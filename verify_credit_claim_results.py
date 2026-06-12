from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from PIL import Image


FIELDS = [
    "债权人",
    "主债务人",
    "借款金额",
    "本金余额",
    "保证人",
    "抵押物",
    "质押物",
    "贷款日",
    "到期日",
    "诉讼状态",
    "受让方",
    "转让方",
]

TRACE_FILE = "债权清洗结果_带来源.csv"
ISSUE_FILE = "清洗结果问题清单.csv"
CHECK_CSV = "逐条检查报告.csv"
MISSING_CSV = "疑似漏洗记录.csv"
REPORT_MD = "逐条检查报告.md"


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
    parser = argparse.ArgumentParser(description="Verify each extracted credit-claim record against its source image.")
    parser.add_argument("--output-dir", default="outputs_image_whole", help="Folder containing extraction CSV outputs.")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "ark_coding"), help="OpenAI-compatible provider.")
    parser.add_argument("--model", default=os.getenv("ARK_CODING_MODEL") or os.getenv("ARK_MODEL") or "", help="Vision model.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ARK_CODING_BASE_URL") or os.getenv("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/coding/v3",
        help="OpenAI-compatible base URL.",
    )
    parser.add_argument("--api-key-env", default="ARK_CODING_API_KEY", help="API key env var.")
    parser.add_argument("--max-tokens", type=int, default=12000, help="Maximum output tokens per source image.")
    parser.add_argument("--request-timeout", type=float, default=180.0, help="Request timeout seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries per verification call.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Pause between calls.")
    parser.add_argument("--no-cache", action="store_true", help="Do not reuse verification cache.")
    parser.add_argument("--limit-images", type=int, default=0, help="Debug only: verify at most N source images.")
    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def write_csv_rows(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def image_to_data_url(path: Path) -> str:
    import base64

    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def parse_model_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def verification_schema() -> dict[str, Any]:
    correction_props = {field: {"type": "string"} for field in FIELDS}
    row_check = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "csv_row_no": {"type": "integer"},
            "row_index": {"type": "string"},
            "verdict": {"type": "string", "enum": ["通过", "需修正", "需复核", "多余记录", "无法判断"]},
            "severity": {"type": "string", "enum": ["通过", "轻微", "中等", "严重"]},
            "issues": {"type": "array", "items": {"type": "string"}},
            "field_corrections": {
                "type": "object",
                "additionalProperties": False,
                "properties": correction_props,
                "required": FIELDS,
            },
            "evidence": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["csv_row_no", "row_index", "verdict", "severity", "issues", "field_corrections", "evidence", "confidence"],
    }
    missing_record = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "description": {"type": "string"},
            "evidence": {"type": "string"},
            "severity": {"type": "string", "enum": ["轻微", "中等", "严重"]},
        },
        "required": ["description", "evidence", "severity"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "image_summary": {"type": "string"},
            "visible_record_count": {"type": "string"},
            "row_checks": {"type": "array", "items": row_check},
            "missing_records": {"type": "array", "items": missing_record},
            "overall_notes": {"type": "string"},
        },
        "required": ["image_summary", "visible_record_count", "row_checks", "missing_records", "overall_notes"],
    }


def build_prompt(source_image: str, rows: list[dict[str, Any]]) -> str:
    payload = []
    for row in rows:
        payload.append(
            {
                "csv_row_no": row["_csv_row_no"],
                "明细序号": normalize(row.get("明细序号")),
                **{field: normalize(row.get(field)) for field in FIELDS},
                "金额口径": normalize(row.get("金额口径")),
                "原文摘录": normalize(row.get("原文摘录")),
            }
        )
    return f"""
你是中文金融公告抽取结果的质检员。请对照图片，逐条检查给定 CSV 抽取记录是否与图片原文一致。

来源图片：{source_image}
待检查记录 JSON：
{json.dumps(payload, ensure_ascii=False, indent=2)}

检查要求：
1. 必须逐条返回 row_checks，csv_row_no 必须对应输入中的 csv_row_no，不要遗漏任何输入记录。
2. 核对字段：{ "、".join(FIELDS) }。
3. 特别检查主债务人、金额、金额单位、转让方/受让方角色、保证人、抵押物/质押物、日期、诉讼状态是否串行或错位。
   如果原表列名是“担保人名称、担保人、担保方”，该列中所有主体都应出现在“保证人”字段；标注“（抵押人）”“（出质人）”的主体也要保留角色标注，不能因为不是普通保证人而删除。
4. 金额应为阿拉伯数字加“元”；原文为“万元”时应换算为“元”。如果原文只有本息合计/债权总额，不应填到本金余额。
5. 如果记录正确，verdict=通过，severity=通过，issues 为空数组，field_corrections 各字段填空字符串。
6. 如果字段有错，verdict=需修正，并在 field_corrections 中只填写建议修正值；无误字段填空字符串。
7. 如果图片不清或原文没有披露导致无法断定，verdict=需复核或无法判断，不要强行修正。
8. 如果图片里存在明显可见但 CSV 没有覆盖的债权明细，请写入 missing_records；没有漏洗则返回空数组。
9. evidence 填支持判断的短原文片段，不要长篇复制。
10. 只输出合法 JSON 对象，不要 Markdown，不要解释。
""".strip()


def init_client(args: argparse.Namespace) -> Any:
    api_key = os.getenv(args.api_key_env)
    if not api_key:
        raise RuntimeError(f"{args.api_key_env} is missing")
    if not args.model:
        raise RuntimeError("model is missing")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed. Run pip install -r requirements.txt first.") from exc
    return OpenAI(api_key=api_key, base_url=args.base_url, timeout=args.request_timeout)


def verify_group(client: Any, args: argparse.Namespace, crop_path: Path, source_image: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = build_prompt(source_image, rows)
    data_url = image_to_data_url(crop_path)
    last_error: Exception | None = None
    for attempt in range(args.retries + 1):
        try:
            response = client.chat.completions.create(
                model=args.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "credit_claim_verification",
                        "strict": True,
                        "schema": verification_schema(),
                    },
                },
                max_tokens=args.max_tokens,
            )
            return parse_model_json(response.choices[0].message.content)
        except Exception as exc:
            last_error = exc
            if re.search(r"response_format|json_schema|schema|structured", str(exc), re.I):
                response = client.chat.completions.create(
                    model=args.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt + "\n\n不要输出 Markdown，只输出合法 JSON。"},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                    max_tokens=args.max_tokens,
                )
                return parse_model_json(response.choices[0].message.content)
            if attempt < args.retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"verification failed for {source_image}: {last_error}") from last_error


def deterministic_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in FIELDS:
        if row.get(field) is None:
            issues.append(f"{field}列缺失")
    if not normalize(row.get("主债务人")):
        issues.append("主债务人为空")

    for field in ["借款金额", "本金余额"]:
        value = normalize(row.get(field))
        if not value:
            continue
        if any(token in value for token in ["人民币", "￥", "¥", "元整", "万元"]):
            issues.append(f"{field}金额格式未统一:{value}")
        if not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?元|\d+(?:\.\d+)?元", value):
            issues.append(f"{field}金额格式异常:{value}")

    for field in ["贷款日", "到期日"]:
        value = normalize(row.get(field))
        if value and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            issues.append(f"{field}日期格式非 YYYY-MM-DD:{value}")

    creditor = normalize(row.get("债权人"))
    assignee = normalize(row.get("受让方"))
    transferor = normalize(row.get("转让方"))
    if creditor and assignee and creditor == assignee:
        issues.append("债权人与受让方相同，需确认角色")
    if transferor and assignee and transferor == assignee:
        issues.append("转让方与受让方相同，需确认角色")

    crop_path = Path(normalize(row.get("切块图片")))
    if not crop_path.exists():
        issues.append("切块图片路径不存在")
    bbox = normalize(row.get("bbox"))
    if not re.fullmatch(r"\[\d+, \d+, \d+, \d+\]", bbox):
        issues.append(f"bbox格式异常:{bbox}")
    return issues


def combined_verdict(model_verdict: str, model_severity: str, local_issues: list[str]) -> tuple[str, str]:
    if local_issues and model_verdict == "通过":
        return "需复核", "轻微"
    if model_verdict:
        return model_verdict, model_severity or "中等"
    if local_issues:
        return "需复核", "轻微"
    return "通过", "通过"


def normalize_corrections(value: Any) -> str:
    if isinstance(value, dict):
        meaningful = {k: normalize(v) for k, v in value.items() if normalize(v)}
        return json.dumps(meaningful, ensure_ascii=False) if meaningful else ""
    return normalize(value)


def write_markdown_report(path: Path, check_rows: list[dict[str, Any]], missing_rows: list[dict[str, Any]], group_notes: list[dict[str, str]]) -> None:
    verdict_counts = Counter(row["最终结论"] for row in check_rows)
    severity_counts = Counter(row["严重程度"] for row in check_rows)
    program_attention = [row for row in check_rows if row.get("程序质检问题")]
    lines = [
        "# 债权清洗结果逐条检查报告",
        "",
        "## 总览",
        f"- 检查记录数：{len(check_rows)}",
        f"- 通过：{verdict_counts.get('通过', 0)}",
        f"- 需修正：{verdict_counts.get('需修正', 0)}",
        f"- 需复核：{verdict_counts.get('需复核', 0)}",
        f"- 多余记录：{verdict_counts.get('多余记录', 0)}",
        f"- 无法判断：{verdict_counts.get('无法判断', 0)}",
        f"- 疑似漏洗记录：{len(missing_rows)}",
        f"- 程序质检关注项：{len(program_attention)}",
        "",
        "## 严重程度",
    ]
    for severity, count in severity_counts.most_common():
        lines.append(f"- {severity}: {count}")
    lines.extend(["", "## 需要关注的记录"])

    attention = [row for row in check_rows if row["最终结论"] != "通过"]
    if not attention:
        lines.append("未发现需要修正或复核的记录。")
    else:
        for row in attention:
            lines.append(
                f"- CSV行{row['CSV行号']} | {row['来源图片']} | 序号{row['明细序号']} | "
                f"{row['主债务人']} | {row['最终结论']} | {row['严重程度']} | {row['问题说明']}"
            )
            if row["建议修正"]:
                lines.append(f"  建议修正：{row['建议修正']}")
            if row["证据"]:
                lines.append(f"  证据：{row['证据']}")

    lines.extend(["", "## 疑似漏洗"])
    if not missing_rows:
        lines.append("未发现模型指出的明显漏洗记录。")
    else:
        for row in missing_rows:
            lines.append(f"- {row['来源图片']} | {row['严重程度']} | {row['描述']} | 证据：{row['证据']}")

    lines.extend(["", "## 程序质检关注项"])
    if not program_attention:
        lines.append("未发现额外程序质检关注项。")
    else:
        for row in program_attention:
            lines.append(
                f"- CSV行{row['CSV行号']} | {row['来源图片']} | 序号{row['明细序号']} | "
                f"{row['主债务人']} | {row['程序质检问题']}"
            )
            if row["最终结论"] == "通过":
                lines.append("  视觉逐条核对结论：该行字段可在图片中找到依据，暂不建议删除或改写。")

    lines.extend(["", "## 分图片检查摘要"])
    for note in group_notes:
        lines.append(
            f"- {note['来源图片']}：抽取行数 {note['记录数']}，图片可见记录数判断 {note['可见记录数']}，"
            f"说明：{note['图片摘要']} {note['总体备注']}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    trace_path = output_dir / TRACE_FILE
    if not trace_path.exists():
        print(f"Missing trace CSV: {trace_path}", file=sys.stderr)
        return 2
    rows = read_csv_rows(trace_path)
    if not rows:
        print("No rows to verify.", file=sys.stderr)
        return 2
    issue_path = output_dir / ISSUE_FILE
    issue_rows = read_csv_rows(issue_path) if issue_path.exists() else []
    program_issues_by_row: dict[int, list[str]] = defaultdict(list)
    for issue in issue_rows:
        try:
            row_no = int(normalize(issue.get("行号")))
        except ValueError:
            continue
        issue_text = f"{normalize(issue.get('问题类型'))}:{normalize(issue.get('问题详情'))}"
        if issue_text != ":":
            program_issues_by_row[row_no].append(issue_text)

    for idx, row in enumerate(rows, start=2):
        row["_csv_row_no"] = idx

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (normalize(row.get("来源图片")), normalize(row.get("公告块编号")), normalize(row.get("切块图片")))
        groups[key].append(row)

    group_items = list(groups.items())
    if args.limit_images:
        group_items = group_items[: args.limit_images]

    client = init_client(args)
    cache_dir = output_dir / "verification_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    all_model_checks: dict[int, dict[str, Any]] = {}
    missing_rows: list[dict[str, Any]] = []
    group_notes: list[dict[str, str]] = []

    for index, ((source_image, block_no, crop_path_text), group_rows) in enumerate(group_items, start=1):
        crop_path = Path(crop_path_text)
        cache_path = cache_dir / f"{Path(source_image).stem}_block_{block_no}.json"
        print(f"[{index}/{len(group_items)}] Verifying {source_image} rows={len(group_rows)}")
        if not crop_path.exists():
            payload = {
                "image_summary": "切块图片不存在，无法视觉核对。",
                "visible_record_count": "无法判断",
                "row_checks": [
                    {
                        "csv_row_no": int(row["_csv_row_no"]),
                        "row_index": normalize(row.get("明细序号")),
                        "verdict": "无法判断",
                        "severity": "严重",
                        "issues": ["切块图片不存在"],
                        "field_corrections": {field: "" for field in FIELDS},
                        "evidence": "",
                        "confidence": 0,
                    }
                    for row in group_rows
                ],
                "missing_records": [],
                "overall_notes": "",
            }
        elif cache_path.exists() and not args.no_cache:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            payload = verify_group(client, args, crop_path, source_image, group_rows)
            cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(args.sleep)

        group_notes.append(
            {
                "来源图片": source_image,
                "记录数": str(len(group_rows)),
                "可见记录数": normalize(payload.get("visible_record_count")),
                "图片摘要": normalize(payload.get("image_summary")),
                "总体备注": normalize(payload.get("overall_notes")),
            }
        )
        for item in payload.get("row_checks", []):
            try:
                all_model_checks[int(item.get("csv_row_no"))] = item
            except (TypeError, ValueError):
                continue
        for missing in payload.get("missing_records", []):
            missing_rows.append(
                {
                    "来源图片": source_image,
                    "公告块编号": block_no,
                    "描述": normalize(missing.get("description")),
                    "证据": normalize(missing.get("evidence")),
                    "严重程度": normalize(missing.get("severity")),
                }
            )

    check_rows: list[dict[str, Any]] = []
    for row in rows:
        csv_row_no = int(row["_csv_row_no"])
        model_check = all_model_checks.get(csv_row_no, {})
        local_issues = deterministic_issues(row)
        model_issues = [normalize(issue) for issue in model_check.get("issues", []) if normalize(issue)]
        all_issues = model_issues + local_issues
        if not model_check:
            all_issues.append("视觉核对结果缺失")
            verdict, severity = "无法判断", "严重"
        else:
            verdict, severity = combined_verdict(
                normalize(model_check.get("verdict")),
                normalize(model_check.get("severity")),
                local_issues,
            )
        if verdict == "通过" and all_issues:
            verdict, severity = "需复核", "轻微"
        check_rows.append(
            {
                "CSV行号": csv_row_no,
                "来源图片": normalize(row.get("来源图片")),
                "公告块编号": normalize(row.get("公告块编号")),
                "明细序号": normalize(row.get("明细序号")),
                "债权人": normalize(row.get("债权人")),
                "主债务人": normalize(row.get("主债务人")),
                "借款金额": normalize(row.get("借款金额")),
                "本金余额": normalize(row.get("本金余额")),
                "保证人": normalize(row.get("保证人")),
                "受让方": normalize(row.get("受让方")),
                "转让方": normalize(row.get("转让方")),
                "最终结论": verdict,
                "严重程度": severity,
                "问题说明": "；".join(all_issues),
                "程序质检问题": "；".join(program_issues_by_row.get(csv_row_no, [])),
                "建议修正": normalize_corrections(model_check.get("field_corrections")),
                "证据": normalize(model_check.get("evidence")),
                "视觉核对置信度": normalize(model_check.get("confidence")),
                "切块图片": normalize(row.get("切块图片")),
            }
        )

    check_columns = [
        "CSV行号",
        "来源图片",
        "公告块编号",
        "明细序号",
        "债权人",
        "主债务人",
        "借款金额",
        "本金余额",
        "保证人",
        "受让方",
        "转让方",
        "最终结论",
        "严重程度",
        "问题说明",
        "程序质检问题",
        "建议修正",
        "证据",
        "视觉核对置信度",
        "切块图片",
    ]
    write_csv_rows(output_dir / CHECK_CSV, check_rows, check_columns)
    write_csv_rows(output_dir / MISSING_CSV, missing_rows, ["来源图片", "公告块编号", "描述", "证据", "严重程度"])
    write_markdown_report(output_dir / REPORT_MD, check_rows, missing_rows, group_notes)

    verdict_counts = Counter(row["最终结论"] for row in check_rows)
    print(f"Done. verified={len(check_rows)}; verdicts={dict(verdict_counts)}; missing={len(missing_rows)}")
    print(f"Report: {output_dir / REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
