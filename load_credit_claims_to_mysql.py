from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


BUSINESS_FIELD_MAP = {
    "债权人": "creditor",
    "主债务人": "primary_debtor",
    "借款金额": "loan_amount",
    "本金余额": "principal_balance",
    "保证人": "guarantor",
    "抵押物": "mortgage",
    "质押物": "pledge",
    "贷款日": "loan_date",
    "到期日": "due_date",
    "诉讼状态": "litigation_status",
    "受让方": "assignee",
    "转让方": "transferor",
}

TRACE_FILE = "债权清洗结果_带来源.csv"
ISSUE_FILE = "清洗结果问题清单.csv"


def load_env_file(path: Path, override: bool = True) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_env_file(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="Load credit-claim CSV results into MySQL staging tables.")
    parser.add_argument("--output-dir", default="outputs_image_whole", help="Folder containing extraction CSV outputs.")
    parser.add_argument("--run-uid", default="", help="Stable run id. Defaults to a hash of output-dir.")
    parser.add_argument("--run-name", default="", help="Human-readable run name. Defaults to output-dir name.")
    parser.add_argument("--input-dir", default="", help="Original input image folder for run metadata.")
    parser.add_argument("--provider", default="", help="Provider name for run metadata.")
    parser.add_argument("--model", default="", help="Model name for run metadata.")
    parser.add_argument("--status", default="STAGED", help="Run status stored in credit_claim_runs.")
    parser.add_argument("--dry-run", action="store_true", help="Read CSVs and print counts without connecting or writing.")
    parser.add_argument("--check", action="store_true", help="Only verify database connection with SELECT 1.")
    parser.add_argument("--no-create-tables", action="store_true", help="Do not run CREATE TABLE IF NOT EXISTS.")
    return parser.parse_args()


def require_env(names: list[str]) -> dict[str, str]:
    values = {name: os.getenv(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return values


def connect_from_env(require_database: bool = True) -> Any:
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("PyMySQL is not installed. Run: pip install -r requirements.txt") from exc

    required = ["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD"]
    if require_database:
        required.append("MYSQL_DATABASE")
    env = require_env(required)
    port = int(os.getenv("MYSQL_PORT", "3306"))
    kwargs = {
        "host": env["MYSQL_HOST"],
        "port": port,
        "user": env["MYSQL_USER"],
        "password": env["MYSQL_PASSWORD"],
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
    }
    if os.getenv("MYSQL_DATABASE"):
        kwargs["database"] = os.getenv("MYSQL_DATABASE")
    return pymysql.connect(
        **kwargs,
    )


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def read_optional_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return read_csv(path)


def stable_run_uid(output_dir: Path, explicit: str) -> str:
    if explicit:
        return explicit
    digest = hashlib.sha1(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"csv_{digest}"


def create_tables(conn: Any) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS credit_claim_runs (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '批次自增主键',
            run_uid VARCHAR(64) NOT NULL COMMENT '批次唯一标识，用于幂等写入',
            run_name VARCHAR(255) NOT NULL DEFAULT '' COMMENT '批次名称',
            input_dir VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '输入图片目录',
            output_dir VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '结果输出目录',
            provider VARCHAR(64) NOT NULL DEFAULT '' COMMENT '模型供应商',
            model VARCHAR(255) NOT NULL DEFAULT '' COMMENT '模型名称',
            status VARCHAR(32) NOT NULL DEFAULT 'STAGED' COMMENT '批次状态，默认 STAGED 表示待审核暂存',
            record_count INT NOT NULL DEFAULT 0 COMMENT '本批次主记录数量',
            issue_count INT NOT NULL DEFAULT 0 COMMENT '本批次问题记录数量',
            finished_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '批次完成时间',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_credit_claim_runs_uid (run_uid)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权图片清洗抽取批次表'
        """,
        """
        CREATE TABLE IF NOT EXISTS credit_claim_records_staging (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '暂存记录自增主键',
            run_id BIGINT UNSIGNED NOT NULL COMMENT '所属抽取批次 ID',
            dedupe_key CHAR(64) NOT NULL COMMENT '同批次幂等去重键',
            source_image VARCHAR(512) NOT NULL DEFAULT '' COMMENT '来源图片文件名',
            block_no VARCHAR(32) NOT NULL DEFAULT '' COMMENT '公告块编号，整图模式通常为001',
            crop_path TEXT COMMENT '来源裁剪图路径',
            bbox TEXT COMMENT '来源区域坐标，格式[x,y,w,h]',
            provider VARCHAR(64) NOT NULL DEFAULT '' COMMENT '模型供应商',
            model VARCHAR(255) NOT NULL DEFAULT '' COMMENT '模型名称',
            detail_seq VARCHAR(64) NOT NULL DEFAULT '' COMMENT '原表序号或明细序号',
            creditor TEXT COMMENT '债权人：持有或公告主张债权的主体',
            primary_debtor TEXT COMMENT '主债务人：借款人、客户名称或单户债权对应债务人',
            loan_amount VARCHAR(128) NOT NULL DEFAULT '' COMMENT '借款金额：原始借款/贷款/发放金额，统一为“数字+元”',
            principal_balance VARCHAR(128) NOT NULL DEFAULT '' COMMENT '本金余额：剩余本金/接收时本金/结欠本金，统一为“数字+元”',
            guarantor TEXT COMMENT '保证人：担保人名称/担保方；若含抵押人或出质人，以括号保留角色标注',
            mortgage TEXT COMMENT '抵押物：具体抵押财产或权利描述',
            pledge TEXT COMMENT '质押物：具体质押财产或权利描述',
            loan_date VARCHAR(32) NOT NULL DEFAULT '' COMMENT '贷款日：贷款发放日/借款日，标准化为YYYY-MM-DD',
            due_date VARCHAR(32) NOT NULL DEFAULT '' COMMENT '到期日：最后还款日/到期日，标准化为YYYY-MM-DD',
            litigation_status VARCHAR(255) NOT NULL DEFAULT '' COMMENT '诉讼状态：案件状态/执行状态/债权状态',
            assignee TEXT COMMENT '受让方：接收方、买受人、购买方或债权受让主体',
            transferor TEXT COMMENT '转让方：出让方、委托方、委托人、出包方或原债权转让主体',
            amount_basis VARCHAR(255) NOT NULL DEFAULT '' COMMENT '金额口径：本金、借款金额、本息合计等来源口径',
            confidence DECIMAL(6,4) NULL COMMENT '模型对该行抽取的置信度，0到1',
            source_excerpt TEXT COMMENT '支撑抽取结果的原文摘录',
            row_warnings TEXT COMMENT '行内警告，如字段缺失、金额口径不确定等',
            issue_flags TEXT COMMENT '关联的问题类型汇总',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_credit_claim_record (run_id, dedupe_key),
            KEY idx_credit_claim_record_run (run_id),
            KEY idx_credit_claim_record_source (source_image),
            CONSTRAINT fk_credit_claim_record_run FOREIGN KEY (run_id) REFERENCES credit_claim_runs(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权清洗结果暂存表，审核后再进入正式业务表'
        """,
        """
        CREATE TABLE IF NOT EXISTS credit_claim_issues (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '问题记录自增主键',
            run_id BIGINT UNSIGNED NOT NULL COMMENT '所属抽取批次 ID',
            row_no VARCHAR(64) NOT NULL DEFAULT '' COMMENT '对应 CSV 行号',
            issue_type VARCHAR(255) NOT NULL DEFAULT '' COMMENT '问题类型',
            issue_detail TEXT COMMENT '问题详情',
            source_image VARCHAR(512) NOT NULL DEFAULT '' COMMENT '来源图片文件名',
            block_no VARCHAR(32) NOT NULL DEFAULT '' COMMENT '公告块编号',
            creditor TEXT COMMENT '债权人快照',
            primary_debtor TEXT COMMENT '主债务人快照',
            loan_amount VARCHAR(128) NOT NULL DEFAULT '' COMMENT '借款金额快照',
            principal_balance VARCHAR(128) NOT NULL DEFAULT '' COMMENT '本金余额快照',
            assignee TEXT COMMENT '受让方快照',
            transferor TEXT COMMENT '转让方快照',
            confidence DECIMAL(6,4) NULL COMMENT '模型置信度',
            source_excerpt TEXT COMMENT '原文摘录',
            crop_path TEXT COMMENT '来源裁剪图路径',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            KEY idx_credit_claim_issue_run (run_id),
            KEY idx_credit_claim_issue_source (source_image),
            CONSTRAINT fk_credit_claim_issue_run FOREIGN KEY (run_id) REFERENCES credit_claim_runs(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权清洗质检问题表'
        """,
    ]
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def confidence_value(value: Any) -> float | None:
    text = normalize(value)
    if not text:
        return None
    try:
        return max(0.0, min(1.0, float(text)))
    except ValueError:
        return None


def record_dedupe_key(row: dict[str, Any]) -> str:
    pieces = [
        normalize(row.get("来源图片")),
        normalize(row.get("公告块编号")),
        normalize(row.get("明细序号")),
        normalize(row.get("主债务人")),
        normalize(row.get("借款金额")),
        normalize(row.get("本金余额")),
        normalize(row.get("债权人")),
        normalize(row.get("受让方")),
        normalize(row.get("转让方")),
    ]
    return hashlib.sha256("\x1f".join(pieces).encode("utf-8")).hexdigest()


def build_issue_flags(issue_df: pd.DataFrame) -> dict[tuple[str, str], str]:
    flags: dict[tuple[str, str], set[str]] = defaultdict(set)
    if issue_df.empty:
        return {}
    for _, issue in issue_df.iterrows():
        key = (normalize(issue.get("来源图片")), normalize(issue.get("公告块编号")))
        issue_type = normalize(issue.get("问题类型"))
        if issue_type:
            flags[key].add(issue_type)
    return {key: "；".join(sorted(values)) for key, values in flags.items()}


def upsert_run(conn: Any, args: argparse.Namespace, run_uid: str, record_count: int, issue_count: int) -> int:
    output_dir = Path(args.output_dir).resolve()
    run_name = args.run_name or output_dir.name
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO credit_claim_runs
                (run_uid, run_name, input_dir, output_dir, provider, model, status, record_count, issue_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                run_name = VALUES(run_name),
                input_dir = VALUES(input_dir),
                output_dir = VALUES(output_dir),
                provider = VALUES(provider),
                model = VALUES(model),
                status = VALUES(status),
                record_count = VALUES(record_count),
                issue_count = VALUES(issue_count),
                finished_at = CURRENT_TIMESTAMP
            """,
            (
                run_uid,
                run_name,
                args.input_dir,
                str(output_dir),
                args.provider,
                args.model,
                args.status,
                record_count,
                issue_count,
            ),
        )
        cursor.execute("SELECT id FROM credit_claim_runs WHERE run_uid = %s", (run_uid,))
        row = cursor.fetchone()
    conn.commit()
    return int(row[0])


def upsert_records(conn: Any, run_id: int, trace_df: pd.DataFrame, issue_flags: dict[tuple[str, str], str]) -> int:
    columns = [
        "run_id",
        "dedupe_key",
        "source_image",
        "block_no",
        "crop_path",
        "bbox",
        "provider",
        "model",
        "detail_seq",
        "creditor",
        "primary_debtor",
        "loan_amount",
        "principal_balance",
        "guarantor",
        "mortgage",
        "pledge",
        "loan_date",
        "due_date",
        "litigation_status",
        "assignee",
        "transferor",
        "amount_basis",
        "confidence",
        "source_excerpt",
        "row_warnings",
        "issue_flags",
    ]
    rows = []
    for _, series in trace_df.iterrows():
        row = series.to_dict()
        flag_key = (normalize(row.get("来源图片")), normalize(row.get("公告块编号")))
        rows.append(
            [
                run_id,
                record_dedupe_key(row),
                normalize(row.get("来源图片")),
                normalize(row.get("公告块编号")),
                normalize(row.get("切块图片")),
                normalize(row.get("bbox")),
                normalize(row.get("provider")),
                normalize(row.get("model")),
                normalize(row.get("明细序号")),
                *[normalize(row.get(chinese_name)) for chinese_name in BUSINESS_FIELD_MAP],
                normalize(row.get("金额口径")),
                confidence_value(row.get("置信度")),
                normalize(row.get("原文摘录")),
                normalize(row.get("行内警告")),
                issue_flags.get(flag_key, ""),
            ]
        )
    if not rows:
        return 0

    placeholders = ", ".join(["%s"] * len(columns))
    update_cols = [col for col in columns if col not in {"run_id", "dedupe_key"}]
    sql = f"""
        INSERT INTO credit_claim_records_staging ({", ".join(columns)})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{col} = VALUES({col})" for col in update_cols)}
    """
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)
    conn.commit()
    return len(rows)


def replace_issues(conn: Any, run_id: int, issue_df: pd.DataFrame) -> int:
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM credit_claim_issues WHERE run_id = %s", (run_id,))
    if issue_df.empty:
        conn.commit()
        return 0

    columns = [
        "run_id",
        "row_no",
        "issue_type",
        "issue_detail",
        "source_image",
        "block_no",
        "creditor",
        "primary_debtor",
        "loan_amount",
        "principal_balance",
        "assignee",
        "transferor",
        "confidence",
        "source_excerpt",
        "crop_path",
    ]
    rows = []
    for _, series in issue_df.iterrows():
        issue = series.to_dict()
        rows.append(
            [
                run_id,
                normalize(issue.get("行号")),
                normalize(issue.get("问题类型")),
                normalize(issue.get("问题详情")),
                normalize(issue.get("来源图片")),
                normalize(issue.get("公告块编号")),
                normalize(issue.get("债权人")),
                normalize(issue.get("主债务人")),
                normalize(issue.get("借款金额")),
                normalize(issue.get("本金余额")),
                normalize(issue.get("受让方")),
                normalize(issue.get("转让方")),
                confidence_value(issue.get("置信度")),
                normalize(issue.get("原文摘录")),
                normalize(issue.get("切块图片")),
            ]
        )
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cursor:
        cursor.executemany(
            f"INSERT INTO credit_claim_issues ({', '.join(columns)}) VALUES ({placeholders})",
            rows,
        )
    conn.commit()
    return len(rows)


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    if args.check:
        conn = connect_from_env(require_database=False)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
            print(f"Database connection OK: SELECT 1 -> {result[0]}")
        finally:
            conn.close()
        return 0

    trace_df = read_csv(output_dir / TRACE_FILE)
    issue_df = read_optional_csv(output_dir / ISSUE_FILE)
    run_uid = stable_run_uid(output_dir, args.run_uid)
    print(f"CSV rows: records={len(trace_df)}, issues={len(issue_df)}, run_uid={run_uid}")

    if args.dry_run:
        print("Dry run only; no database connection or write was performed.")
        return 0

    conn = connect_from_env()
    try:
        if not args.no_create_tables:
            create_tables(conn)
        run_id = upsert_run(conn, args, run_uid, len(trace_df), len(issue_df))
        issue_flags = build_issue_flags(issue_df)
        written_records = upsert_records(conn, run_id, trace_df, issue_flags)
        written_issues = replace_issues(conn, run_id, issue_df)
        print(f"Loaded staging run_id={run_id}; records={written_records}; issues={written_issues}")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
