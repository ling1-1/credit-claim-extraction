from __future__ import annotations

import argparse
import html
import json
import sqlite3
import threading
import webbrowser
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from jd_scraper_v2 import (
    ASSET_GROUP_LABELS,
    ASSET_TABLES,
    COMMON_FIELDS,
    SPECIAL_FIELDS,
    SPECIAL_NORMALIZED_COLUMNS,
)
from jd_mysql_store import MySQLConfig, ensure_mysql_schema, get_item_detail_mysql, get_items_mysql


STATUS_LABELS = {
    "extracted": "已提取",
    "missing_on_page": "页面未提供",
    "empty_on_page": "页面字段为空",
    "parse_error": "解析失败",
    "conflict": "多来源冲突",
}

VIEWER_SOURCE_LABEL = "SQLite 本地只读查看"


SPECIAL_NORMALIZED_COMMENTS: dict[str, dict[str, tuple[str, str]]] = {
    "land": {
        "land_area_sqm": ("土地面积-平方米", "土地面积换算后的平方米数值，用于数据库筛选、排序和去重。"),
        "assessment_amount": ("评估价值-数值", "土地评估价值的规范化 DECIMAL 数值。"),
        "assessment_date": ("评估日期", "土地评估基准日或评估日期。"),
    },
    "real_estate": {
        "building_area_sqm": ("建筑面积-平方米", "建筑面积换算后的平方米数值，用于数据库筛选、排序和去重。"),
    },
    "debt": {
        "benchmark_date_norm": ("基准日-规范值", "债权基准日转换后的 DATE 值。"),
    },
}


BASE_TABLE_COMMENTS: dict[str, dict[str, tuple[str, str]]] = {
    "crawl_batches": {
        "batch_id": ("采集批次ID", "一次采集任务的唯一编号。"),
        "started_at": ("开始时间", "该批次开始采集的时间。"),
        "finished_at": ("结束时间", "该批次结束采集的时间。"),
        "parameters_json": ("采集参数", "本批次使用的类目、数量、筛选条件等参数。"),
        "status": ("批次状态", "采集批次的执行状态。"),
        "message": ("批次消息", "错误信息或批次说明。"),
    },
    "raw_payloads": {
        "paimai_id": ("标的ID", "京东拍卖标的唯一 ID。"),
        "batch_id": ("采集批次ID", "该原始数据归属的采集批次。"),
        "source_url": ("来源页面", "京东拍卖详情页地址。"),
        "list_json": ("列表原始JSON", "列表接口返回的该标的原始数据。"),
        "detail_json": ("详情原始JSON", "详情接口返回的原始数据。"),
        "product_basic_json": ("商品基础信息原始JSON", "商品基础信息接口返回的原始数据。"),
        "realtime_json": ("实时原始JSON", "实时价格、状态、出价记录接口返回的原始数据。"),
        "description_html": ("详情HTML", "标的物详情区域的原始 HTML。"),
        "notice_html": ("竞买须知HTML", "竞买须知/公告类正文原始 HTML，常包含联系方式和交易注意事项。"),
        "announcement_html": ("竞买公告HTML", "竞买公告接口返回的原始 HTML；若平台未提供则为空。"),
        "attachments_json": ("附件原始JSON", "附件接口返回的原始数据。"),
        "vendor_json": ("处置方原始JSON", "处置方/机构接口返回的原始数据。"),
        "crawled_at": ("采集时间", "该标的原始数据写入数据库的时间。"),
    },
    "auction_items_common": {
        "paimai_id": ("标的ID", "京东拍卖标的唯一 ID。"),
        "batch_id": ("采集批次ID", "该标的归属的采集批次。"),
        "source_url": ("来源页面", "京东拍卖详情页地址。"),
        "asset_group": ("资产类型编码", "系统内部资产类型编码，用于区分共有字段和特有字段。"),
        "asset_group_label": ("资产类型", "中文资产类型名称。"),
        "jd_category_id": ("京东类目ID", "京东一级类目 ID。"),
        "jd_category_name": ("京东类目", "京东一级类目中文名称。"),
        "common_fields_json": ("共有字段JSON", "共有字段的完整 JSON 快照。"),
        "source_platform": ("来源平台", "该标的来自哪个拍卖平台，用于后续多平台汇总。"),
        "source_item_id": ("平台原始ID", "来源平台上的原始标的 ID。"),
        "signup_start_time_norm": ("报名开始时间-规范值", "把报名/竞价开始时间转换为数据库可筛选的 DATETIME。"),
        "signup_end_time_norm": ("报名截止时间-规范值", "把报名/竞价截止时间转换为数据库可筛选的 DATETIME。"),
        "start_price_amount": ("起拍价-数值", "把起拍价转换为 DECIMAL 数值，便于排序和统计。"),
        "final_price_amount": ("最终价/当前价-数值", "把最终价或当前价转换为 DECIMAL 数值，便于排序和统计。"),
        "assessment_price_amount": ("评估价-数值", "从评估价格及时间中提取出的评估价数值。"),
        "assessment_amount": ("评估价-数值", "评估价或市场价的规范化 DECIMAL 数值，供数据库查询使用。"),
        "assessment_date": ("评估日期", "从评估价格及时间中提取出的评估基准日或评估日期。"),
        "dedup_hash": ("去重指纹", "按资产类型选取核心字段后标准化生成的指纹，用于发现跨平台或重复挂牌记录。"),
        "updated_at": ("更新时间", "共有字段最后写入时间。"),
    },
    "asset_dedup_index": {
        "source_platform": ("来源平台", "数据来自哪个平台，例如 jd。"),
        "source_item_id": ("平台原始ID", "来源平台上的原始标的 ID。"),
        "paimai_id": ("本地标的ID", "当前系统内关联的京东标的 ID。"),
        "dedup_hash": ("去重指纹", "用于识别疑似同一资产的标准化 hash。"),
        "asset_group": ("资产类型", "用于选择不同资产类型的去重字段组合。"),
        "project_name": ("项目名称", "用于人工核对疑似重复资产。"),
        "asset_location": ("标的所在地", "用于人工核对疑似重复资产。"),
        "identity_basis_json": ("去重依据", "生成去重指纹时使用的原始值和标准化值。"),
        "updated_at": ("更新时间", "去重索引最后更新时间。"),
    },
    "field_catalog": {
        "field_namespace": ("字段命名空间", "区分共有字段和各资产类型特有字段，避免同名字段冲突。"),
        "asset_group": ("资产类型编码", "字段适用的资产类型；共有字段为 ALL。"),
        "field_key": ("字段编码", "程序内部使用的英文字段名。"),
        "field_label": ("字段中文名", "页面和导出时展示的中文字段名。"),
        "field_scope": ("字段范围", "共有字段或特有字段。"),
        "data_type": ("数据类型", "字段值的数据类型。"),
        "required_for_display": ("是否必须显示", "为 1 表示页面和导出中必须展示该字段。"),
        "aliases_json": ("字段别名", "解析网页时用于匹配的中文别名。"),
        "source_priority_json": ("来源优先级", "字段提取时各来源的优先顺序。"),
        "export_order": ("导出顺序", "页面和导出文件中的字段排序。"),
    },
    "field_extractions": {
        "paimai_id": ("标的ID", "字段所属的京东拍卖标的 ID。"),
        "field_namespace": ("字段命名空间", "字段所属范围，例如 common 或 special.debt。"),
        "asset_group": ("资产类型编码", "字段所属资产类型。"),
        "field_key": ("字段编码", "程序内部使用的英文字段名。"),
        "field_label": ("字段中文名", "页面展示用中文字段名。"),
        "raw_value": ("原始值", "从接口或网页中提取到的原始字段值。"),
        "normalized_value": ("标准化值", "清洗后的字段值；首版通常与原始值一致。"),
        "status": ("提取状态", "字段是否提取成功、页面是否缺失或是否冲突。"),
        "method": ("提取方式", "字段来自接口、HTML 表格、正文规则或未找到。"),
        "confidence": ("置信度", "该字段提取结果的可信程度。"),
        "source_payload_type": ("来源类型", "字段来自哪个原始数据来源。"),
        "source_path": ("来源路径", "接口 JSON 路径或 HTML 解析路径。"),
        "source_excerpt": ("来源片段", "证明该字段值的网页或接口片段。"),
        "missing_reason": ("缺失原因", "字段为空时记录原因。"),
        "extracted_at": ("提取时间", "字段提取记录写入时间。"),
    },
    "asset_debt_details": {
        "paimai_id": ("标的ID", "京东拍卖标的唯一 ID。"),
        "detail_index": ("明细序号", "系统生成的债权明细行序号。"),
        "sequence_no": ("原表序号", "网页表格中的序号，通常对应债权户数。"),
        "debtor_name": ("债务人名称", "该户债权对应的借款人、主债务人或债务企业名称。"),
        "principal_balance": ("本金余额", "该明细行披露的本金余额。"),
        "principal_balance_amount": ("本金余额-数值", "本金余额的规范化 DECIMAL 数值。"),
        "interest_balance": ("利息余额", "该明细行披露的利息、罚息、复利等余额。"),
        "interest_balance_amount": ("利息余额-数值", "利息、罚息、复利余额的规范化 DECIMAL 数值。"),
        "recovery_fee": ("实现债权费用", "该明细行披露的实现债权费用金额。"),
        "recovery_fee_amount": ("实现债权费用-数值", "实现债权费用的规范化 DECIMAL 数值。"),
        "claim_total": ("债权合计", "该明细行披露的本金、利息、费用等合计。"),
        "claim_total_amount": ("债权合计-数值", "债权合计的规范化 DECIMAL 数值。"),
        "collateral": ("抵质押物", "该户债权对应的抵押物、质押物或担保物。"),
        "guarantor": ("保证人", "该户债权对应的保证人、担保人或相关义务人。"),
        "litigation_status": ("诉讼状态", "该户债权披露的诉讼、执行或案件状态。"),
        "benchmark_date": ("基准日", "该债权包明细金额对应的基准日。"),
        "benchmark_date_norm": ("基准日-规范值", "基准日转换后的 DATE 值。"),
        "amount_unit": ("金额单位", "该债权包表格披露的金额单位。"),
        "source_excerpt": ("来源片段", "该债权明细的来源片段。"),
        "updated_at": ("更新时间", "债权明细最后写入时间。"),
    },
    "asset_ip_details": {
        "paimai_id": ("标的ID", "京东拍卖标的唯一 ID。"),
        "detail_index": ("明细序号", "系统生成的知识产权明细行序号。"),
        "sequence_no": ("原表序号", "网页表格或正文中的序号。"),
        "ip_name": ("单项名称", "单个知识产权标的的名称。"),
        "certificate_no": ("证号/登记号/申请号", "专利号、著作权登记号、商标注册号或申请号等。"),
        "ip_type": ("知产类型", "软件著作权、发明专利、实用新型、外观设计、商标等。"),
        "application_date": ("申请日/登记日期", "该知识产权的申请日、登记日期或授权日期。"),
        "application_date_norm": ("申请日/登记日期-规范值", "申请日、登记日或授权日转换后的 DATE 值。"),
        "patent_type": ("专利类型", "发明、实用新型、外观设计等专利分类。"),
        "status": ("法律状态", "有效、无效、受限、已终止等状态。"),
        "source_excerpt": ("来源片段", "该知识产权明细的来源片段。"),
        "updated_at": ("更新时间", "知识产权明细最后写入时间。"),
    },
}


@contextmanager
def db_connect(db_path: Path | str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def pretty_json(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
    else:
        parsed = value
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def short_text(value: Any, limit: int = 140) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def field_comment_rows() -> list[tuple[str, str, str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, str, str, int]] = []

    for table, columns in BASE_TABLE_COMMENTS.items():
        for order, (column, (label, comment)) in enumerate(columns.items(), start=1):
            rows.append((table, column, label, comment, "system", "", order))

    for order, field in enumerate(COMMON_FIELDS, start=100):
        rows.append(
            (
                "auction_items_common",
                field.key,
                field.label,
                f"所有资产类型都必须展示的共有字段：{field.label}。",
                "common",
                "ALL",
                order,
            )
        )

    for group, table in ASSET_TABLES.items():
        rows.append((table, "paimai_id", "标的ID", "京东拍卖标的唯一 ID。", "system", group, 1))
        rows.append((table, "special_fields_json", "特有字段JSON", "该类型特有字段的完整 JSON 快照。", "system", group, 998))
        rows.append((table, "updated_at", "更新时间", "该类型特有字段最后写入时间。", "system", group, 999))
        for offset, column in enumerate(SPECIAL_NORMALIZED_COLUMNS.get(group, {}), start=900):
            label, comment = SPECIAL_NORMALIZED_COMMENTS.get(group, {}).get(
                column,
                (column, "从该类型特有字段中生成的数据库规范化伴生列。"),
            )
            rows.append((table, column, label, comment, "normalized", group, offset))
        for order, field in enumerate(SPECIAL_FIELDS[group], start=10):
            label = ASSET_GROUP_LABELS[group]
            rows.append(
                (
                    table,
                    field.key,
                    field.label,
                    f"资产类型“{label}”的特有字段：{field.label}。",
                    "special",
                    group,
                    order,
                )
            )
    return rows


def ensure_field_comments(db_path: Path | str) -> None:
    with db_connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS db_field_comments (
              table_name TEXT,
              column_name TEXT,
              field_label TEXT,
              comment TEXT,
              field_scope TEXT,
              asset_group TEXT,
              display_order INTEGER,
              PRIMARY KEY (table_name, column_name)
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO db_field_comments
            (table_name, column_name, field_label, comment, field_scope, asset_group, display_order)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            field_comment_rows(),
        )


def table_comments(conn: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    return {
        row["column_name"]: row
        for row in conn.execute(
            """
            SELECT * FROM db_field_comments
            WHERE table_name=?
            """,
            (table_name,),
        )
    }


def get_items(db_path: Path | str, filters: dict[str, str] | None = None) -> dict[str, Any]:
    ensure_field_comments(db_path)
    filters = filters or {}
    clauses = []
    params: list[Any] = []

    if filters.get("asset_group"):
        clauses.append("c.asset_group = ?")
        params.append(filters["asset_group"])
    if filters.get("project_status"):
        clauses.append("c.project_status = ?")
        params.append(filters["project_status"])
    if filters.get("q"):
        clauses.append(
            """
            (
              c.project_name LIKE ?
              OR c.asset_location LIKE ?
              OR c.disposal_party LIKE ?
              OR EXISTS (
                SELECT 1 FROM field_extractions fx
                WHERE fx.paimai_id = c.paimai_id
                  AND fx.raw_value LIKE ?
              )
            )
            """
        )
        like = f"%{filters['q']}%"
        params.extend([like, like, like, like])
    if filters.get("issue") == "missing":
        clauses.append("EXISTS (SELECT 1 FROM field_extractions fm WHERE fm.paimai_id=c.paimai_id AND fm.status!='extracted')")

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    query = f"""
        SELECT
          c.paimai_id,
          c.asset_group,
          c.asset_group_label,
          c.jd_category_id,
          c.jd_category_name,
          c.project_name,
          c.asset_location,
          c.project_status,
          c.start_price_raw,
          c.final_price_raw,
          c.disposal_party,
          COUNT(fe.field_key) AS total_fields,
          SUM(CASE WHEN fe.status='extracted' THEN 1 ELSE 0 END) AS extracted_fields,
          SUM(CASE WHEN fe.status!='extracted' THEN 1 ELSE 0 END) AS issue_fields
        FROM auction_items_common c
        LEFT JOIN field_extractions fe ON fe.paimai_id = c.paimai_id
        {where}
        GROUP BY c.paimai_id
        ORDER BY CAST(c.jd_category_id AS INTEGER), c.paimai_id
    """

    with db_connect(db_path) as conn:
        items = [dict(row) for row in conn.execute(query, params)]
        asset_groups = [dict(row) for row in conn.execute("SELECT asset_group, asset_group_label, COUNT(*) AS count FROM auction_items_common GROUP BY asset_group, asset_group_label ORDER BY asset_group")]
        statuses = [row["project_status"] for row in conn.execute("SELECT DISTINCT project_status FROM auction_items_common WHERE project_status IS NOT NULL AND project_status!='' ORDER BY project_status")]
    return {"items": items, "asset_groups": asset_groups, "statuses": statuses}


def get_item_detail(db_path: Path | str, paimai_id: str) -> dict[str, Any]:
    ensure_field_comments(db_path)
    with db_connect(db_path) as conn:
        item = conn.execute("SELECT * FROM auction_items_common WHERE paimai_id=?", (paimai_id,)).fetchone()
        if item is None:
            raise KeyError(f"找不到标的：{paimai_id}")
        item_dict = dict(item)
        group = item_dict["asset_group"]
        special_table = ASSET_TABLES[group]
        special_row = conn.execute(f"SELECT * FROM {special_table} WHERE paimai_id=?", (paimai_id,)).fetchone()
        debt_details = []
        if group == "debt":
            debt_details = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM asset_debt_details WHERE paimai_id=? ORDER BY detail_index",
                    (paimai_id,),
                )
            ]
        ip_details = []
        if group == "ip":
            ip_details = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM asset_ip_details WHERE paimai_id=? ORDER BY detail_index",
                    (paimai_id,),
                )
            ]
        raw = conn.execute("SELECT * FROM raw_payloads WHERE paimai_id=?", (paimai_id,)).fetchone()
        duplicates: list[dict[str, Any]] = []
        dedup_hash = item_dict.get("dedup_hash")
        if dedup_hash:
            duplicates = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT source_platform, source_item_id, paimai_id, asset_group,
                           project_name, asset_location, updated_at
                    FROM asset_dedup_index
                    WHERE dedup_hash = ?
                      AND paimai_id != ?
                    ORDER BY updated_at DESC
                    """,
                    (dedup_hash, paimai_id),
                )
            ]
        common_comments = table_comments(conn, "auction_items_common")
        special_comments = table_comments(conn, special_table)

        def load_fields(namespace: str, comments: dict[str, sqlite3.Row]) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT fe.*, fc.export_order
                FROM field_extractions fe
                LEFT JOIN field_catalog fc
                  ON fc.field_namespace = fe.field_namespace
                 AND fc.field_key = fe.field_key
                WHERE fe.paimai_id = ?
                  AND fe.field_namespace = ?
                ORDER BY COALESCE(fc.export_order, 999), fe.field_key
                """,
                (paimai_id, namespace),
            ).fetchall()
            fields = []
            for row in rows:
                comment = comments.get(row["field_key"])
                fields.append(
                    {
                        "key": row["field_key"],
                        "label": row["field_label"],
                        "comment": comment["comment"] if comment else "字段说明未配置。",
                        "value": row["normalized_value"],
                        "raw_value": row["raw_value"],
                        "status": row["status"],
                        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
                        "method": row["method"],
                        "confidence": row["confidence"],
                        "source_payload_type": row["source_payload_type"],
                        "source_path": row["source_path"],
                        "source_excerpt": row["source_excerpt"],
                        "missing_reason": row["missing_reason"],
                    }
                )
            return fields

        return {
            "item": item_dict,
            "special_row": dict(special_row) if special_row else {},
            "raw": dict(raw) if raw else {},
            "common_fields": load_fields("common", common_comments),
            "special_fields": load_fields(f"special.{group}", special_comments),
            "debt_details": debt_details,
            "ip_details": ip_details,
            "duplicates": duplicates,
            "asset_group_label": ASSET_GROUP_LABELS[group],
        }


DataSource = Path | MySQLConfig


def get_items_for_source(source: DataSource, filters: dict[str, str] | None = None) -> dict[str, Any]:
    if isinstance(source, MySQLConfig):
        return get_items_mysql(source, filters)
    return get_items(source, filters)


def get_item_detail_for_source(source: DataSource, paimai_id: str) -> dict[str, Any]:
    if isinstance(source, MySQLConfig):
        return get_item_detail_mysql(source, paimai_id)
    return get_item_detail(source, paimai_id)


def render_layout(title: str, body: str) -> bytes:
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #202124;
      --muted: #69707a;
      --line: #d8dde5;
      --accent: #176b5b;
      --accent-soft: #e7f4ef;
      --warn: #9a5b00;
      --bad: #a33333;
      --ok: #1f7a4f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      padding: 12px 24px;
      display: flex;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
    }}
    .brand {{ font-size: 18px; font-weight: 700; }}
    .wrap {{ max-width: 1500px; margin: 0 auto; padding: 18px 24px 32px; }}
    .toolbar {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      grid-template-columns: minmax(220px, 2fr) repeat(3, minmax(150px, 1fr)) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }}
    label {{ color: var(--muted); font-size: 12px; display: block; margin-bottom: 4px; }}
    input, select {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--text);
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 7px 12px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      white-space: nowrap;
    }}
    .button.secondary {{ background: #fff; color: var(--accent); }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 14px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .metric strong {{ display: block; font-size: 20px; margin-top: 2px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
    }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 9px; vertical-align: top; text-align: left; }}
    th {{ background: #eef1f4; font-weight: 700; color: #363a40; position: sticky; top: 57px; z-index: 2; }}
    tr:hover td {{ background: #fbfcfd; }}
    .muted {{ color: var(--muted); }}
    .status {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #eef1f4; }}
    .status.ok {{ background: #e8f5ee; color: var(--ok); }}
    .status.warn {{ background: #fff4dc; color: var(--warn); }}
    .status.bad {{ background: #fbe7e7; color: var(--bad); }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 14px;
      overflow: hidden;
    }}
    .section h2 {{ margin: 0; padding: 12px 14px; font-size: 16px; border-bottom: 1px solid var(--line); background: #f0f3f5; }}
    .kv {{
      display: grid;
      grid-template-columns: 180px 1fr 110px 170px;
      border-bottom: 1px solid var(--line);
      min-height: 46px;
    }}
    .kv > div {{ padding: 9px 12px; border-right: 1px solid var(--line); overflow-wrap: anywhere; }}
    .kv > div:last-child {{ border-right: 0; }}
    .field-name strong {{ display: block; }}
    .field-name span {{ color: var(--muted); font-size: 12px; }}
    .value-empty {{ color: var(--muted); font-style: italic; }}
    details {{ padding: 10px 14px; border-top: 1px solid var(--line); }}
    summary {{ cursor: pointer; font-weight: 700; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f7f8fa;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      max-height: 420px;
      overflow: auto;
    }}
    .title-row {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }}
    .title-row h1 {{ margin: 0; font-size: 22px; }}
    @media (max-width: 900px) {{
      .toolbar, .summary {{ grid-template-columns: 1fr; }}
      .kv {{ grid-template-columns: 1fr; }}
      .kv > div {{ border-right: 0; }}
      th {{ position: static; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">京东资产拍卖数据查看器</div>
    <div class="muted">{VIEWER_SOURCE_LABEL}</div>
  </div>
  <main class="wrap">{body}</main>
</body>
</html>"""
    return html_text.encode("utf-8")


def render_index(source: DataSource, query: dict[str, str]) -> bytes:
    data = get_items_for_source(source, query)
    items = data["items"]
    total = len(items)
    issue_total = sum(int(item["issue_fields"] or 0) for item in items)
    extracted_total = sum(int(item["extracted_fields"] or 0) for item in items)
    fields_total = sum(int(item["total_fields"] or 0) for item in items)
    completion = f"{(extracted_total / fields_total * 100):.1f}%" if fields_total else "0%"

    asset_options = ['<option value="">全部类型</option>']
    for group in data["asset_groups"]:
        selected = " selected" if query.get("asset_group") == group["asset_group"] else ""
        asset_options.append(
            f'<option value="{esc(group["asset_group"])}"{selected}>{esc(group["asset_group_label"])} ({group["count"]})</option>'
        )
    status_options = ['<option value="">全部状态</option>']
    for status in data["statuses"]:
        selected = " selected" if query.get("project_status") == status else ""
        status_options.append(f'<option value="{esc(status)}"{selected}>{esc(status)}</option>')
    issue_selected = " selected" if query.get("issue") == "missing" else ""

    rows = []
    for item in items:
        issue = int(item["issue_fields"] or 0)
        total_fields = int(item["total_fields"] or 0)
        extracted = int(item["extracted_fields"] or 0)
        row_status = "ok" if issue == 0 else "warn"
        rows.append(
            f"""
            <tr>
              <td><a href="/item/{quote(str(item['paimai_id']))}">{esc(item['paimai_id'])}</a></td>
              <td><strong>{esc(short_text(item['project_name'], 80))}</strong><div class="muted">{esc(short_text(item['asset_location'], 80))}</div></td>
              <td>{esc(item['asset_group_label'])}<div class="muted">{esc(item['jd_category_id'])} / {esc(item['jd_category_name'])}</div></td>
              <td>{esc(item['project_status'])}</td>
              <td>{esc(item['start_price_raw'])}</td>
              <td>{esc(item['final_price_raw'])}</td>
              <td><span class="status {row_status}">{extracted}/{total_fields}</span><div class="muted">缺失/异常 {issue}</div></td>
              <td><a class="button secondary" href="/item/{quote(str(item['paimai_id']))}">查看详情</a></td>
            </tr>
            """
        )

    body = f"""
    <form class="toolbar" method="get" action="/">
      <div>
        <label>关键词</label>
        <input name="q" value="{esc(query.get('q', ''))}" placeholder="项目名称、所在地、处置方、字段值">
      </div>
      <div>
        <label>资产类型</label>
        <select name="asset_group">{''.join(asset_options)}</select>
      </div>
      <div>
        <label>项目状态</label>
        <select name="project_status">{''.join(status_options)}</select>
      </div>
      <div>
        <label>字段状态</label>
        <select name="issue">
          <option value="">全部字段</option>
          <option value="missing"{issue_selected}>有缺失/异常</option>
        </select>
      </div>
      <button type="submit">筛选</button>
    </form>
    <div class="summary">
      <div class="metric"><span class="muted">当前列表</span><strong>{total}</strong></div>
      <div class="metric"><span class="muted">字段完整度</span><strong>{completion}</strong></div>
      <div class="metric"><span class="muted">字段总数</span><strong>{fields_total}</strong></div>
      <div class="metric"><span class="muted">缺失/异常字段</span><strong>{issue_total}</strong></div>
    </div>
    <table>
      <thead>
        <tr>
          <th>标的ID</th>
          <th>项目</th>
          <th>类型/类目</th>
          <th>状态</th>
          <th>起拍价</th>
          <th>最终价/当前价</th>
          <th>字段</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>{''.join(rows) if rows else '<tr><td colspan="8" class="muted">没有符合条件的数据</td></tr>'}</tbody>
    </table>
    """
    return render_layout("京东资产拍卖数据查看器", body)


def render_fields(fields: list[dict[str, Any]]) -> str:
    rows = []
    for field in fields:
        value = field.get("value")
        value_html = esc(value) if value not in (None, "") else '<span class="value-empty">空</span>'
        status = field.get("status")
        status_label = STATUS_LABELS.get(status, field.get("status_label") or status)
        status_class = "ok" if status == "extracted" else "warn"
        source = " / ".join(filter(None, [field.get("source_payload_type"), field.get("source_path")]))
        rows.append(
            f"""
            <div class="kv">
              <div class="field-name">
                <strong>{esc(field.get('label'))}</strong>
                <span>{esc(field.get('comment'))}</span>
              </div>
              <div>{value_html}</div>
              <div><span class="status {status_class}">{esc(status_label)}</span></div>
              <div>
                <div>{esc(source)}</div>
                <div class="muted">{esc(field.get('missing_reason') or field.get('source_excerpt') or '')}</div>
              </div>
            </div>
            """
        )
    return "".join(rows)


def summarize_resources(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return ""
    labels = {
        "attachment": "附件",
        "image": "图片",
        "video": "视频",
    }
    counts: dict[str, int] = {}
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "other")
        counts[resource_type] = counts.get(resource_type, 0) + 1
    parts = [
        f"{labels.get(resource_type, resource_type)} {count} 个"
        for resource_type, count in sorted(counts.items())
    ]
    if not parts:
        return ""
    return "；".join(parts) + "；详见下方“附件/图片/视频”"


def apply_resource_summary_to_fields(fields: list[dict[str, Any]], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = summarize_resources(resources)
    if not summary:
        return fields
    updated: list[dict[str, Any]] = []
    for field in fields:
        if field.get("key") != "attachments_json":
            updated.append(field)
            continue
        new_field = dict(field)
        new_field["value"] = summary
        new_field["source_excerpt"] = summary
        updated.append(new_field)
    return updated


def render_debt_details(details: list[dict[str, Any]], global_benchmark_date: Any = None) -> str:
    if not details:
        return ""
    global_benchmark = "" if global_benchmark_date is None else str(global_benchmark_date).strip()
    detail_benchmark_values = {
        str(detail.get("benchmark_date") or "").strip()
        for detail in details
        if str(detail.get("benchmark_date") or "").strip()
    }
    show_detail_benchmark = bool(
        detail_benchmark_values
        and not (global_benchmark and detail_benchmark_values == {global_benchmark})
    )
    rows = []
    for detail in details:
        debtor_name = detail.get("debtor_name") or detail.get("debtor_or_asset")
        guarantor = detail.get("guarantor") or detail.get("guarantor_or_related_party")
        benchmark_cell = f"<td>{esc(detail.get('benchmark_date'))}</td>" if show_detail_benchmark else ""
        rows.append(
            f"""
            <tr>
              <td>{esc(detail.get('sequence_no'))}</td>
              <td>{esc(debtor_name)}</td>
              <td>{esc(detail.get('principal_balance'))}</td>
              <td>{esc(detail.get('interest_balance'))}</td>
              <td>{esc(detail.get('recovery_fee'))}</td>
              <td>{esc(detail.get('claim_total'))}</td>
              <td>{esc(detail.get('collateral'))}</td>
              <td>{esc(guarantor)}</td>
              <td>{esc(detail.get('litigation_status'))}</td>
              {benchmark_cell}
            </tr>
            """
        )
    benchmark_header = "<th>基准日</th>" if show_detail_benchmark else ""
    return f"""
    <section class="section">
      <h2>债权包明细</h2>
      <table>
        <thead>
          <tr>
            <th>原表序号</th>
            <th>债务人名称</th>
            <th>本金余额</th>
            <th>利息余额</th>
            <th>实现债权费用</th>
            <th>债权合计</th>
            <th>抵质押物</th>
            <th>保证人</th>
            <th>诉讼状态</th>
            {benchmark_header}
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_ip_details(details: list[dict[str, Any]]) -> str:
    if not details:
        return ""
    rows = []
    for detail in details:
        rows.append(
            f"""
            <tr>
              <td>{esc(detail.get('sequence_no'))}</td>
              <td>{esc(detail.get('ip_name'))}</td>
              <td>{esc(detail.get('certificate_no'))}</td>
              <td>{esc(detail.get('ip_type'))}</td>
              <td>{esc(detail.get('application_date'))}</td>
              <td>{esc(detail.get('patent_type'))}</td>
              <td>{esc(detail.get('status'))}</td>
            </tr>
            """
        )
    return f"""
    <section class="section">
      <h2>知识产权明细</h2>
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>单项名称</th>
            <th>证号/登记号/申请号</th>
            <th>知产类型</th>
            <th>申请日/登记日期</th>
            <th>专利类型</th>
            <th>法律状态</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_resources(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return """
        <section class="section">
          <h2>附件/图片/视频</h2>
          <p class="muted">无结构化资源</p>
        </section>
        """

    type_labels = {
        "attachment": "附件",
        "image": "图片",
        "video": "视频",
    }
    role_labels = {
        "assessment_report": "评估报告",
        "announcement": "公告",
        "notice": "须知",
        "inventory": "清单",
        "contract": "合同/协议",
        "site_image": "现场图片",
        "site_video": "现场视频",
        "other": "其他",
    }
    rows = []
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "")
        resource_role = str(resource.get("resource_role") or "")
        resource_name = resource.get("resource_name") or resource.get("file_name") or resource_role or resource_type
        resource_url = str(resource.get("resource_url") or "").strip()
        if resource_url:
            link_html = (
                f'<a href="{esc(resource_url)}" target="_blank" rel="noreferrer">打开</a>'
                f'<div class="muted">{esc(short_text(resource_url, 120))}</div>'
            )
        else:
            link_html = '<span class="value-empty">空</span>'
        rows.append(
            f"""
            <tr>
              <td>{esc(type_labels.get(resource_type, resource_type))}</td>
              <td>{esc(role_labels.get(resource_role, resource_role))}</td>
              <td>{esc(resource_name)}</td>
              <td>{link_html}</td>
              <td>{esc(resource.get('source_section'))}</td>
            </tr>
            """
        )

    return f"""
    <section class="section">
      <h2>附件/图片/视频</h2>
      <table>
        <thead>
          <tr>
            <th>类型</th>
            <th>用途</th>
            <th>名称</th>
            <th>链接</th>
            <th>来源区块</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_duplicates(duplicates: list[dict[str, Any]]) -> str:
    if not duplicates:
        return ""
    rows = []
    for duplicate in duplicates:
        rows.append(
            f"""
            <tr>
              <td>{esc(duplicate.get('source_platform'))}</td>
              <td><a href="/item/{quote(str(duplicate.get('paimai_id')))}">{esc(duplicate.get('source_item_id'))}</a></td>
              <td>{esc(short_text(duplicate.get('project_name'), 90))}</td>
              <td>{esc(short_text(duplicate.get('asset_location'), 90))}</td>
              <td>{esc(duplicate.get('updated_at'))}</td>
            </tr>
            """
        )
    return f"""
    <section class="section">
      <h2>疑似重复资产</h2>
      <table>
        <thead>
          <tr>
            <th>来源平台</th>
            <th>平台ID</th>
            <th>项目名称</th>
            <th>所在地</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_detail(source: DataSource, paimai_id: str) -> bytes:
    try:
        detail = get_item_detail_for_source(source, paimai_id)
    except KeyError as exc:
        return render_layout("找不到标的", f'<div class="section"><h2>找不到标的</h2><p>{esc(exc)}</p><p><a href="/">返回列表</a></p></div>')

    item = detail["item"]
    raw = detail["raw"]
    resources = detail.get("resources") or []
    common_fields = apply_resource_summary_to_fields(detail["common_fields"], resources)
    source_url = item.get("source_url") or f"https://paimai.jd.com/{paimai_id}"
    body = f"""
    <div class="title-row">
      <div>
        <h1>{esc(item.get('project_name') or paimai_id)}</h1>
        <div class="muted">标的ID：{esc(paimai_id)}　资产类型：{esc(item.get('asset_group_label'))}　京东类目：{esc(item.get('jd_category_name'))}</div>
      </div>
      <div><a class="button secondary" href="/">返回列表</a></div>
    </div>

    <section class="section">
      <h2>快速概览</h2>
      <div class="kv"><div>来源页面</div><div><a href="{esc(source_url)}" target="_blank">{esc(source_url)}</a></div><div>项目状态</div><div>{esc(item.get('project_status'))}</div></div>
      <div class="kv"><div>所在地</div><div>{esc(item.get('asset_location'))}</div><div>处置方</div><div>{esc(item.get('disposal_party'))}</div></div>
      <div class="kv"><div>起拍价</div><div>{esc(item.get('start_price_raw'))}</div><div>最终价/当前价</div><div>{esc(item.get('final_price_raw'))}</div></div>
    </section>

    <section class="section">
      <h2>共有字段</h2>
      {render_fields(common_fields)}
    </section>

    <section class="section">
      <h2>{esc(detail['asset_group_label'])}特有字段</h2>
      {render_fields(detail['special_fields'])}
    </section>

    {render_debt_details(detail.get('debt_details') or [], (detail.get('special_row') or {}).get('benchmark_date'))}
    {render_ip_details(detail.get('ip_details') or [])}
    {render_resources(resources)}
    {render_duplicates(detail.get('duplicates') or [])}

    <section class="section">
      <h2>原始证据</h2>
      <details><summary>列表原始 JSON</summary><pre>{esc(pretty_json(raw.get('list_json')))}</pre></details>
      <details><summary>详情原始 JSON</summary><pre>{esc(pretty_json(raw.get('detail_json')))}</pre></details>
      <details><summary>商品基础信息原始 JSON</summary><pre>{esc(pretty_json(raw.get('product_basic_json')))}</pre></details>
      <details><summary>实时原始 JSON</summary><pre>{esc(pretty_json(raw.get('realtime_json')))}</pre></details>
      <details><summary>标的详情 HTML</summary><pre>{esc(raw.get('description_html'))}</pre></details>
      <details><summary>竞买须知 HTML</summary><pre>{esc(raw.get('notice_html'))}</pre></details>
      <details><summary>竞买公告 HTML</summary><pre>{esc(raw.get('announcement_html'))}</pre></details>
      <details><summary>附件原始 JSON</summary><pre>{esc(pretty_json(raw.get('attachments_json')))}</pre></details>
    </section>
    """
    return render_layout(str(item.get("project_name") or paimai_id), body)


class ViewerHandler(BaseHTTPRequestHandler):
    db_path: Path
    data_source: DataSource

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values and values[0]}
        if parsed.path == "/":
            self._send(render_index(self.data_source, query))
            return
        if parsed.path.startswith("/item/"):
            paimai_id = unquote(parsed.path.removeprefix("/item/"))
            self._send(render_detail(self.data_source, paimai_id))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(source: DataSource, host: str, port: int, open_browser: bool) -> None:
    global VIEWER_SOURCE_LABEL
    if isinstance(source, MySQLConfig):
        ensure_mysql_schema(source)
        source_label = f"MySQL {source.host}:{source.port}/{source.database}"
    else:
        ensure_field_comments(source)
        source_label = str(source)
    VIEWER_SOURCE_LABEL = source_label
    ViewerHandler.data_source = source
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    url = f"http://{host}:{port}/"
    print(f"京东资产拍卖数据查看器已启动：{url}")
    print(f"数据库：{source_label}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="京东资产拍卖 SQLite 本地查看器")
    parser.add_argument("--backend", choices=("sqlite", "mysql"), default="sqlite", help="读取 SQLite 或 MySQL")
    parser.add_argument("--db", type=Path, default=Path("outputs") / "sample_2_per_category_utf8" / "jd_auction.sqlite", help="SQLite 数据库路径")
    parser.add_argument("--mysql-host", default="127.0.0.1", help="MySQL 主机")
    parser.add_argument("--mysql-port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--mysql-user", default="", help="MySQL 用户")
    parser.add_argument("--mysql-password", default="", help="MySQL 密码")
    parser.add_argument("--mysql-database", default="auction_data", help="MySQL 数据库")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.backend == "mysql":
        source: DataSource = MySQLConfig(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
        )
    else:
        if not args.db.exists():
            raise SystemExit(f"数据库不存在：{args.db}")
        source = args.db
    if not isinstance(source, MySQLConfig) and not source.exists():
        raise SystemExit(f"数据库不存在：{args.db}")
    run_server(source, args.host, args.port, args.open)


if __name__ == "__main__":
    main()
