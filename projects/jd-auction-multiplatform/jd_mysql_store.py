
import argparse
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import pymysql
from pymysql.cursors import DictCursor

from jd_scraper_v2 import (
    ASSET_GROUP_LABELS,
    ASSET_TABLES,
    COMMON_FIELDS,
    COMMON_FIELD_DATA_TYPES,
    DEBT_LEGACY_AGGREGATE_FIELDS,
    DEDUP_FIELDS_CONFIG,
    FieldDef,
    SPECIAL_FIELDS,
    SPECIAL_FIELD_DATA_TYPES,
    SPECIAL_NORMALIZED_COLUMNS,
    area_sqm_to_db,
    compact_text,
    compute_dedup_hash,
    date_to_db,
    decimal_to_db,
    datetime_to_db,
    has_assessment_date_text,
    is_valid_assessment_price_time,
    money_numeric,
    normalize_dedup_part,
    normalized_common_db_values,
    normalized_special_db_values,
    now_text,
    safe_json_dumps,
    typed_field_extraction_values,
)


@dataclass(frozen=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = "root"
    database: str = "auction_data"


def mysql_connection(config: MySQLConfig, *, database: bool = True):
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database if database else None,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )


def qmarks(count: int) -> str:
    return ", ".join(["%s"] * count)


def ensure_mysql_database(config: MySQLConfig) -> None:
    with mysql_connection(config, database=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()


def mysql_table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table_name,),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row.get("cnt") or 0) > 0
    return int(row[0] if row else 0) > 0


def mysql_column_types(cur, table: str) -> dict[str, str]:
    cur.execute(f"SHOW COLUMNS FROM `{table}`")
    return {row["Field"]: row["Type"].lower() for row in cur.fetchall()}



def ensure_mysql_columns(cur, table: str, columns: dict[str, str]) -> None:
    column_types = mysql_column_types(cur, table)
    existing = set(column_types)
    for column, definition in columns.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def ensure_ai_model_profile_columns(cur) -> None:
    """Keep existing ai_model_profiles tables compatible with the current schema."""
    if not mysql_table_exists(cur, "ai_model_profiles"):
        return
    ensure_mysql_columns(
        cur,
        "ai_model_profiles",
        {
            "max_concurrency": "INT NULL COMMENT '该模型配置建议最大并发数；为空则由任务参数决定'",
            "task_types": "JSON NULL COMMENT '适用任务类型数组，如 text/long_text/debt/vision/attachment；空表示通用'",
            "priority": "INT NOT NULL DEFAULT 100 COMMENT '调度优先级，数字越小越优先'",
        },
    )


def ensure_ai_enrichment_queue_columns(cur) -> None:
    """Keep existing ai_enrichment_queue tables compatible with current worker states."""
    if not mysql_table_exists(cur, "ai_enrichment_queue"):
        return
    ensure_mysql_columns(
        cur,
        "ai_enrichment_queue",
        {
            "running_profile_name": "VARCHAR(100) NULL COMMENT '实际处理AI配置名'",
            "running_provider": "VARCHAR(80) NULL COMMENT '实际处理AI供应商'",
            "running_model_name": "VARCHAR(200) NULL COMMENT '实际处理AI模型'",
        },
    )


def upsert_rows(cur, table: str, rows: Iterable[dict[str, Any]], column_types: dict[str, str]) -> int:
    inserted = 0
    dest_columns = set(column_types)
    for row in rows:
        columns = [column for column in row.keys() if column in dest_columns]
        if not columns:
            continue
        values = [coerce_mysql_value(row[column], column_types[column]) for column in columns]
        updates = ", ".join(f"`{column}`=VALUES(`{column}`)" for column in columns)
        sql = (
            f"INSERT INTO `{table}` ({', '.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({qmarks(len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        cur.execute(sql, values)
        inserted += 1
    return inserted


def update_row_columns(
    cur,
    table: str,
    key_column: str,
    key_value: Any,
    values: dict[str, Any],
    column_types: dict[str, str],
) -> int:
    columns = [
        column
        for column, value in values.items()
        if column in column_types and column != key_column and value is not None
    ]
    if not columns:
        return 0
    assignments = ", ".join(f"`{column}`=%s" for column in columns)
    params = [coerce_mysql_value(values[column], column_types[column]) for column in columns]
    params.append(key_value)
    cur.execute(f"UPDATE `{table}` SET {assignments} WHERE `{key_column}`=%s", params)
    return cur.rowcount


# ===== MySQL V2 storage adapter =====

V2_SCHEMA_PATH = Path(__file__).with_name("sql") / "mysql_schema_v2.sql"
V2_DROP_TABLES = [
    "review_queue",
    "data_quality_reports",
    "asset_dedup_index",
    "ai_enrichment_queue",
    "ocr_retry_queue",
    "asset_other",
    "asset_usufruct",
    "asset_goods",
    "asset_ip_details",
    "asset_ip",
    "asset_equity",
    "asset_debt_details",
    "asset_debt",
    "asset_vehicle",
    "asset_equipment",
    "asset_land",
    "asset_real_estate",
    "item_resources",
    "field_extractions",
    "field_catalog",
    "raw_payloads",
    "dead_letter_queue",
    "crawl_queue_events",
    "crawl_checkpoints",
    "crawl_queue",
    "auction_items",
    "crawl_batches",
    "crawl_job_runs",
    "crawl_jobs",
]

LEGACY_DROP_TABLES = [
    "crawl_queue_items",
    "field_comments",
    "auction_items_common",
]

RESET_DROP_TABLES = [*V2_DROP_TABLES, *LEGACY_DROP_TABLES]

V2_SPECIAL_TABLES = {
    "land": "asset_land",
    "real_estate": "asset_real_estate",
    "equipment": "asset_equipment",
    "vehicle": "asset_vehicle",
    "debt": "asset_debt",
    "equity": "asset_equity",
    "ip": "asset_ip",
    "goods": "asset_goods",
    "usufruct": "asset_usufruct",
    "other": "asset_other",
}


def _blank_to_none(value: Any) -> Any:
    text = compact_text(value)
    return text if text else None


def _money_decimal(value: Any) -> Optional[Decimal]:
    amount = money_numeric(value)
    if amount is None:
        return None
    return amount.quantize(Decimal("0.01"))


def _valid_price_display(value: Any) -> bool:
    text = compact_text(value)
    if not text:
        return False
    if _money_decimal(text) is not None:
        return True
    return any(token in text for token in ("面议", "详见", "无底价", "免费", "以公告为准", "另行通知"))


def _debt_detail_unit_multiplier(detail: Mapping[str, Any] | None) -> Decimal:
    if not detail:
        return Decimal("1")
    unit_text = " ".join(
        compact_text(detail.get(key))
        for key in (
            "money_unit",
            "amount_unit",
            "unit",
            "table_unit",
            "source_excerpt",
            "table_header",
            "source_payload",
        )
        if compact_text(detail.get(key))
    )
    if re.search(r"((?:单位|金额单位))\s*[:：]?\s*亿|[（(]亿元[）)]|单位[:：]?\s*人民币亿元", unit_text):
        return Decimal("100000000")
    if re.search(r"((?:单位|金额单位))\s*[:：]?\s*万|[（(]万元[）)]|单位[:：]?\s*人民币万元", unit_text):
        return Decimal("10000")
    return Decimal("1")


def _debt_detail_money_decimal(value: Any, detail: Mapping[str, Any] | None = None) -> Optional[Decimal]:
    amount = money_numeric(value)
    if amount is None:
        return None
    text = compact_text(value)
    has_explicit_unit = bool(re.search(r"(?:元|万)|亿", text))
    if not has_explicit_unit:
        amount *= _debt_detail_unit_multiplier(detail)
    return amount.quantize(Decimal("0.01"))


def _area_decimal(value: Any) -> Optional[Decimal]:
    area = area_sqm_to_db(value)
    if area is None:
        return None
    try:
        return Decimal(str(area)).quantize(Decimal("0.000001"))
    except InvalidOperation:
        return None


def _int_or_none(value: Any) -> Optional[int]:
    text = compact_text(value)
    if not text:
        return None
    match = re.search(r"\d+", text.replace(",", ""))
    if not match:
        return None
    return int(match.group(0))


def _parse_json_maybe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    text = compact_text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return value


def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return safe_json_dumps(value)


def _hash_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = safe_json_dumps(value)
    else:
        text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _payload_tab(payload_type: str) -> str:
    return {
        "list_json": "列表接口",
        "detail_json": "详情接口",
        "product_basic_json": "商品基础信息",
        "realtime_json": "实时价格",
        "description_html": "标的物详情",
        "notice_html": "竞买须知",
        "announcement_html": "竞买公告",
        "attachments_json": "附件材料",
        "vendor_json": "处置方信息",
        "attachment_texts": "附件解析文本",
    }.get(payload_type, payload_type)


def _resource_role(name: str, resource_type: str, asset_group: Optional[str] = None) -> str:
    text = name or ""
    if resource_type == "image":
        if asset_group == "vehicle":
            return "vehicle_image"
        return "site_image"
    if resource_type == "video":
        return "site_video"
    if "评估" in text or "估价" in text:
        return "assessment_report"
    if "清单" in text or "明细" in text or "情况表" in text:
        return "asset_list"
    if "公告" in text:
        return "announcement_file"
    if "须知" in text:
        return "notice_file"
    if "协议" in text or "合同" in text:
        return "agreement_file"
    if "报名" in text or "受让" in text:
        return "registration_file"
    return "attachment_file"


def _normalize_resource_url(url: Any, resource_type: str) -> Optional[str]:
    text = compact_text(url)
    if not text:
        return None
    if text.lower().startswith(("javascript:", "about:", "#")):
        return None
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://")):
        return text
    if resource_type == "image" and text.startswith("jfs/"):
        return "https://img30.360buyimg.com/popWaterMark/" + text
    return text


def _walk_media_urls(value: Any) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in {"imagepath", "imageurl", "imgurl", "imgsrc", "picurl"}:
                url = _normalize_resource_url(item, "image")
                if url:
                    results.append(("image", url))
                continue
            if low in {"videopath", "videourl", "videosrc"}:
                url = _normalize_resource_url(item, "video")
                if url:
                    results.append(("video", url))
                continue
            results.extend(_walk_media_urls(item))
    elif isinstance(value, list):
        for item in value:
            results.extend(_walk_media_urls(item))
    return results


def _valid_assessment_text(
    value: Any,
    *,
    source_excerpt: Any = None,
    source_payload_type: str = "",
    source_path: str = "",
) -> bool:
    text = compact_text(value)
    if not text:
        return False
    structured_field = source_payload_type in {"list_json", "detail_json", "product_basic_json"} and any(
        token in source_path for token in ("assessmentPrice", "marketPrice", "judicatureBasicInfoResult.marketPrice")
    )
    if structured_field:
        return _money_decimal(text) is not None
    if re.fullmatch(r"\s*((?:市场价|评估价)|参考价)\s*\d+(?:\.\d+)?\s*", text):
        return False
    if re.search(r"\d+(?:\.\d+)?\s*((?:倍|折)|成)", text) and not re.search(r"((?:元|万元)|亿元|￥|¥)", text):
        return False
    if not re.search(r"((?:评估价|评估价格)|(?:评估价值|市场价)|(?:市场价格|参考价)|估价)", text):
        return False
    if not re.search(r"((?:元|万元)|亿元|￥|¥)", text):
        return False
    return is_valid_assessment_price_time(
        text,
        compact_text(source_excerpt),
        structured_assessment_field=False,
        require_source_assessment_signal=False,
    )


def _assessment_basis(value: Any) -> Optional[str]:
    text = compact_text(value) or ""
    if "市场价" in text or "市场价格" in text:
        return "market_price"
    if "参考价" in text:
        return "reference_price"
    if "评估报告" in text:
        return "assessment_report"
    if "评估" in text or "估价" in text:
        return "assessment_price"
    return None


def _bid_count(value: Any) -> Optional[int]:
    parsed = _parse_json_maybe(value)
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("bidList", "bids", "data"):
            item = parsed.get(key)
            if isinstance(item, list):
                return len(item)
    return None


def build_v2_common_item_row(
    *,
    paimai_id: str,
    batch_id: Optional[str],
    asset_group: str,
    jd_category_id: Optional[str],
    jd_category_name: Optional[str],
    values: dict[str, Any],
    special_values: dict[str, Any] | None = None,
    field_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    special_values = special_values or {}
    field_results = field_results or {}
    source_platform = _blank_to_none(values.get("source_platform")) or "jd"
    source_item_id = _blank_to_none(values.get("source_item_id")) or str(paimai_id)
    source_url = _blank_to_none(values.get("source_url")) or (
        f"https://paimai.jd.com/{paimai_id}" if source_platform == "jd" else ""
    )
    source_site_name = _blank_to_none(values.get("source_site_name")) or (
        "京东拍卖" if source_platform == "jd" else source_platform
    )
    start_display = _blank_to_none(values.get("start_price_raw"))
    explicit_final_display = _blank_to_none(values.get("final_price_raw"))
    final_display = explicit_final_display or start_display
    assessment_text = _blank_to_none(values.get("assessment_price_time")) or _blank_to_none(
        special_values.get("assessment_time_value")
    )
    assessment_result = field_results.get("assessment_price_time", {})
    assessment_valid = _valid_assessment_text(
        assessment_text,
        source_excerpt=assessment_result.get("source_excerpt"),
        source_payload_type=compact_text(assessment_result.get("source_payload_type")) or "",
        source_path=compact_text(assessment_result.get("source_path")) or "",
    )
    assessment_amount = _money_decimal(assessment_text) if assessment_valid else None
    assessment_date = (
        date_to_db(assessment_text)
        if assessment_valid and has_assessment_date_text(assessment_text)
        else None
    )
    common_for_hash = {field.key: values.get(field.key) for field in COMMON_FIELDS}
    common_for_hash["source_platform"] = source_platform
    common_for_hash["source_item_id"] = source_item_id
    right_holder = None if asset_group == "debt" else _blank_to_none(
        special_values.get("right_holder") or special_values.get("original_right_holder")
    )
    return {
        "source_platform": source_platform,
        "source_item_id": source_item_id,
        "source_url": source_url,
        "source_site_name": source_site_name,
        "batch_id": batch_id,
        "asset_group": asset_group,
        "asset_group_label": ASSET_GROUP_LABELS.get(asset_group, asset_group),
        "source_category_id": _blank_to_none(jd_category_id),
        "source_category_name": _blank_to_none(jd_category_name),
        "asset_type": _blank_to_none(values.get("asset_type")),
        "asset_location": _blank_to_none(values.get("asset_location")),
        "project_status": _blank_to_none(values.get("project_status")),
        "project_status_basis": _blank_to_none(values.get("project_status_basis")),
        "auction_stage": _blank_to_none(values.get("auction_stage")),
        "bid_records_count": _bid_count(values.get("bid_records_json")),
        "bid_records_json": _json_or_none(_parse_json_maybe(values.get("bid_records_json")) or []),
        "data_source": _blank_to_none(values.get("data_source")),
        "project_name": _blank_to_none(values.get("project_name")) or f"JD-{paimai_id}",
        "signup_start_time": datetime_to_db(values.get("signup_start_time")),
        "signup_end_time": datetime_to_db(values.get("signup_end_time")),
        "disposal_party": _blank_to_none(values.get("disposal_party")),
        "disposal_agency": _blank_to_none(values.get("disposal_agency")),
        "right_holder": right_holder,
        "start_price_amount": _money_decimal(start_display),
        "start_price_display": start_display,
        "final_price_amount": _money_decimal(final_display),
        "final_price_display": final_display,
        "price_basis": "effective_price" if explicit_final_display else "start_price_fallback",
        "contact_info": _blank_to_none(values.get("contact_info")),
        "special_notice": _blank_to_none(values.get("special_notice")),
        "disclosed_defects": _blank_to_none(special_values.get("disclosed_defects")),
        "assessment_price_amount": assessment_amount,
        "assessment_price_display": assessment_text if assessment_valid else None,
        "assessment_price_basis": _assessment_basis(assessment_text) if assessment_valid else None,
        "assessment_date": assessment_date,
        "dedup_hash": compute_dedup_hash(asset_group, common_for_hash, special_values),
        "last_seen_at": now_text(),
        "last_crawled_at": now_text(),
    }


def build_v2_resource_rows(
    *,
    item_id: int,
    attachments_json: Any,
    asset_group: Optional[str] = None,
    source_payload_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    parsed = _parse_json_maybe(attachments_json)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_row(resource_type: str, url: Any, name: Any = None, fmt: Any = None, size: Any = None, section: str = "") -> None:
        normalized_url = _normalize_resource_url(url, resource_type)
        if not normalized_url:
            return
        key = (resource_type, normalized_url)
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "item_id": item_id,
                "resource_type": resource_type,
                "resource_role": _resource_role(compact_text(name) or "", resource_type, asset_group),
                "resource_name": _blank_to_none(name) or normalized_url.rsplit("/", 1)[-1],
                "resource_url": normalized_url,
                "resource_format": _blank_to_none(fmt),
                "resource_size_bytes": _int_or_none(size),
                "source_section": section or _payload_tab("attachments_json"),
                "source_payload_id": source_payload_id,
                "url_hash": _hash_text(normalized_url),
                "is_downloaded": 0,
            }
        )

    file_items: list[Any] = []

    def walk_file_items(value: Any) -> None:
        value = _parse_json_maybe(value)
        if isinstance(value, dict):
            has_url = any(
                compact_text(value.get(key))
                for key in (
                    "attachmentAddress",
                    "url",
                    "href",
                    "fileUrl",
                    "downloadUrl",
                    "downloadURL",
                    "attachmentUrl",
                    "filePath",
                    "resourceUrl",
                    "ossUrl",
                )
            )
            if has_url:
                file_items.append(value)
            for item in value.values():
                walk_file_items(item)
        elif isinstance(value, list):
            for item in value:
                walk_file_items(item)

    if isinstance(parsed, dict):
        for key in ("files", "data", "attachments", "attachmentList"):
            item = parsed.get(key)
            if isinstance(item, list):
                file_items.extend(item)
            elif item is not None:
                walk_file_items(item)
        media_source = parsed.get("media", parsed)
    elif isinstance(parsed, list):
        file_items.extend([item for item in parsed if isinstance(item, dict) and item.get("attachmentAddress")])
        walk_file_items(parsed)
        media_source = parsed
    else:
        media_source = None

    for item in file_items:
        if not isinstance(item, dict):
            continue
        add_row(
            "attachment",
            item.get("attachmentAddress")
            or item.get("url")
            or item.get("href")
            or item.get("fileUrl")
            or item.get("downloadUrl")
            or item.get("downloadURL")
            or item.get("attachmentUrl")
            or item.get("filePath")
            or item.get("resourceUrl")
            or item.get("ossUrl"),
            item.get("attachmentName") or item.get("name") or item.get("fileName") or item.get("title"),
            item.get("attachmentFormat") or item.get("format") or item.get("fileType"),
            item.get("attachmentSize") or item.get("size") or item.get("fileSize"),
        )

    for resource_type, url in _walk_media_urls(media_source):
        add_row(resource_type, url, url, None, None, "图片/视频")
    return rows


def build_v2_special_row(*, item_id: int, asset_group: str, values: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"item_id": item_id}
    if asset_group == "real_estate":
        row.update(
            {
                "right_certificate_no": _blank_to_none(values.get("right_certificate_no")),
                "building_area_sqm": _area_decimal(values.get("building_area")),
                "building_area_display": _blank_to_none(values.get("building_area")),
                "property_use": _blank_to_none(values.get("property_use")),
                "use_term": _blank_to_none(values.get("use_term")),
                "property_location": _blank_to_none(values.get("property_location")),
                "property_structure": _blank_to_none(values.get("property_structure")),
                "property_status": _blank_to_none(values.get("property_status")),
                "property_type": _blank_to_none(values.get("property_type")),
                "asset_highlights": _blank_to_none(values.get("asset_highlights")),
            }
        )
    elif asset_group == "land":
        row.update(
            {
                "right_certificate_no": _blank_to_none(values.get("right_certificate_no")),
                "land_area_sqm": _area_decimal(values.get("land_area")),
                "land_area_display": _blank_to_none(values.get("land_area")),
                "land_use": _blank_to_none(values.get("land_use")),
                "use_term": _blank_to_none(values.get("use_term")),
                "land_location": _blank_to_none(values.get("land_location")),
                "land_status": _blank_to_none(values.get("land_status")),
                "land_type": _blank_to_none(values.get("land_type")),
            }
        )
    elif asset_group == "equipment":
        row.update(
            {
                "storage_location": _blank_to_none(values.get("storage_location")),
                "equipment_status": _blank_to_none(values.get("equipment_status")),
                "equipment_type": _blank_to_none(values.get("equipment_type")),
            }
        )
    elif asset_group == "vehicle":
        row.update(
            {
                "storage_location": _blank_to_none(values.get("storage_location")),
                "vehicle_brand_model": _blank_to_none(values.get("vehicle_brand_model")),
                "vehicle_usage": _blank_to_none(values.get("vehicle_usage")),
                "plate_number": _blank_to_none(values.get("plate_number")),
                "vehicle_configuration": _blank_to_none(values.get("vehicle_configuration")),
                "vehicle_status": _blank_to_none(values.get("vehicle_status")),
                "vehicle_type": _blank_to_none(values.get("vehicle_type")),
            }
        )
    elif asset_group == "debt":
        debtor_names = _blank_to_none(values.get("debtor_name"))
        row.update(
            {
                "main_debtor_name": debtor_names.split("、", 1)[0] if debtor_names else None,
                "debtor_names": debtor_names,
                "creditor": _blank_to_none(values.get("creditor")),
                "principal_balance_amount": _money_decimal(values.get("principal_balance")),
                "principal_balance_display": _blank_to_none(values.get("principal_balance")),
                "interest_balance_amount": _money_decimal(values.get("interest_balance")),
                "interest_balance_display": _blank_to_none(values.get("interest_balance")),
                "claim_total_amount": _money_decimal(values.get("claim_total")),
                "claim_total_display": _blank_to_none(values.get("claim_total")),
                "benchmark_date": date_to_db(values.get("benchmark_date")),
                "guarantee_method": _blank_to_none(values.get("guarantee_method")),
                "guarantor": _blank_to_none(values.get("guarantor")),
                "collateral": _blank_to_none(values.get("collateral")),
                "litigation_status": _blank_to_none(values.get("litigation_status")),
                "household_count": _int_or_none(values.get("household_count")),
            }
        )
    elif asset_group == "equity":
        for key in (
            "transferor",
            "target_company",
            "equity_ratio",
            "company_nature",
            "company_industry",
            "business_scope",
            "ownership_structure",
            "financial_metrics",
            "asset_valuation",
            "disclosure_items",
            "attached_assets",
        ):
            row[key] = _blank_to_none(values.get(key))
    elif asset_group == "ip":
        row.update(
            {
                "subject_name": _blank_to_none(values.get("subject_name")),
                "ip_count": _int_or_none(values.get("ip_count")),
                "certificate_no": _blank_to_none(values.get("certificate_no")),
                "ip_type": _blank_to_none(values.get("ip_type")),
                "specific_category": _blank_to_none(values.get("specific_category")),
                "subject_intro": _blank_to_none(values.get("subject_intro")),
                "right_term": _blank_to_none(values.get("right_term")),
            }
        )
    elif asset_group == "goods":
        row.update(
            {
                "goods_category": _blank_to_none(values.get("goods_category")),
                "goods_name": _blank_to_none(values.get("goods_name")),
                "goods_location": _blank_to_none(values.get("goods_location")),
                "goods_details": _blank_to_none(values.get("goods_details")),
                "right_burden": _blank_to_none(values.get("right_burden")),
            }
        )
    elif asset_group == "usufruct":
        row.update(
            {
                "right_category": _blank_to_none(values.get("right_category")),
                "subject_name": _blank_to_none(values.get("subject_name")),
                "subject_location": _blank_to_none(values.get("subject_location")),
                "subject_details": _blank_to_none(values.get("subject_details")),
                "valid_period": _blank_to_none(values.get("valid_period")),
                "original_right_holder": _blank_to_none(values.get("original_right_holder")),
                "right_burden": _blank_to_none(values.get("right_burden")),
            }
        )
    else:
        row.update(
            {
                "raw_detail_text": _blank_to_none(values.get("raw_detail_text")),
                "raw_table_pairs_json": _json_or_none(_parse_json_maybe(values.get("raw_table_pairs_json"))),
                "extracted_summary": _blank_to_none(values.get("extracted_summary")),
            }
        )
    return {key: value for key, value in row.items() if value is not None or key == "item_id"}


def coerce_mysql_value(value: Any, column_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, Decimal):
        number = value
        text = str(value)
    else:
        text = str(value)
    if text == "":
        return None if any(token in column_type for token in ("date", "time", "decimal", "int", "json")) else ""
    if "datetime" in column_type or "timestamp" in column_type:
        return datetime_to_db(text)
    if column_type == "date":
        return date_to_db(text)
    if "json" in column_type:
        parsed = _parse_json_maybe(value)
        return safe_json_dumps(parsed)
    if any(token in column_type for token in ("decimal", "int", "bigint")):
        try:
            number = value if isinstance(value, Decimal) else Decimal(text.replace(",", ""))
        except (InvalidOperation, AttributeError):
            return None
        if "int" in column_type and "decimal" not in column_type:
            return int(number)
        match = re.search(r"decimal\(\d+\s*,\s*(\d+)\)", column_type)
        scale = int(match.group(1)) if match else 2
        quantum = Decimal("1").scaleb(-scale)
        return format(number.quantize(quantum), "f")
    return text


def mysql_table_names() -> list[str]:
    return list(V2_DROP_TABLES)


def mysql_reset_table_names() -> list[str]:
    return list(RESET_DROP_TABLES)


def ensure_mysql_schema(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    sql_text = V2_SCHEMA_PATH.read_text(encoding="utf-8")
    sql_text = re.sub(r"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+auction_data.*?;", "", sql_text, flags=re.I | re.S)
    sql_text = re.sub(r"USE\s+auction_data\s*;", f"USE `{config.database}`;", sql_text, flags=re.I)
    statements = [statement.strip() for statement in sql_text.split(";") if statement.strip()]
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            ensure_ai_model_profile_columns(cur)
            ensure_ai_enrichment_queue_columns(cur)
        conn.commit()


def reset_mysql_tables(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in RESET_DROP_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    ensure_mysql_schema(config)


def _get_item_id(cur, source_item_id: str, *, source_platform: str = "jd", required: bool = True) -> Optional[int]:
    cur.execute(
        "SELECT item_id FROM auction_items WHERE source_platform=%s AND source_item_id=%s",
        (source_platform, str(source_item_id)),
    )
    row = cur.fetchone()
    if row:
        return int(row["item_id"])
    if required:
        raise KeyError(f"item not found: {source_platform}:{source_item_id}")
    return None


def _ensure_item_stub(
    cur,
    paimai_id: str,
    batch_id: Optional[str] = None,
    source_url: Optional[str] = None,
    *,
    source_platform: str = "jd",
    source_site_name: Optional[str] = None,
) -> int:
    source_item_id = str(paimai_id)
    row = {
        "source_platform": source_platform,
        "source_item_id": source_item_id,
        "source_url": source_url or (f"https://paimai.jd.com/{paimai_id}" if source_platform == "jd" else ""),
        "batch_id": batch_id,
        "source_site_name": source_site_name or ("京东拍卖" if source_platform == "jd" else source_platform),
        "asset_group": "other",
        "asset_group_label": ASSET_GROUP_LABELS.get("other", "其他"),
        "project_name": f"{source_platform}-{source_item_id}",
        "last_seen_at": now_text(),
        "last_crawled_at": now_text(),
    }
    upsert_rows(cur, "auction_items", [row], mysql_column_types(cur, "auction_items"))
    return int(_get_item_id(cur, source_item_id, source_platform=source_platform))


def _insert_payload(
    cur,
    *,
    item_id: int,
    batch_id: Optional[str],
    payload_type: str,
    value: Any,
    source_url: Optional[str],
    source_platform: str = "jd",
) -> Optional[int]:
    if value is None:
        return None
    is_json_payload = payload_type.endswith("_json")
    payload_text = None if is_json_payload else str(value or "")
    payload_json = safe_json_dumps(value) if is_json_payload else None
    payload_hash = _hash_text(payload_json if is_json_payload else payload_text)
    row = {
        "item_id": item_id,
        "batch_id": batch_id,
        "source_platform": source_platform,
        "payload_type": payload_type,
        "source_url": source_url,
        "source_tab": _payload_tab(payload_type),
        "payload_text": payload_text,
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "fetched_at": now_text(),
    }
    upsert_rows(cur, "raw_payloads", [row], mysql_column_types(cur, "raw_payloads"))
    cur.execute(
        """
        SELECT payload_id FROM raw_payloads
        WHERE item_id=%s AND payload_type=%s AND payload_hash=%s
        ORDER BY payload_id DESC LIMIT 1
        """,
        (item_id, payload_type, payload_hash),
    )
    found = cur.fetchone()
    return int(found["payload_id"]) if found else None


class MySQLJDScraperDatabase:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config

    def init_schema(self) -> None:
        ensure_mysql_schema(self.config)

    def seed_field_catalog(self) -> None:
        rows: list[dict[str, Any]] = []
        for order, field in enumerate(COMMON_FIELDS, start=1):
            rows.append(
                {
                    "field_namespace": "common",
                    "asset_group": "ALL",
                    "field_key": field.key,
                    "field_label": field.label,
                    "field_comment": f"所有资产类型都应展示的共有字段：{field.label}",
                    "data_type": COMMON_FIELD_DATA_TYPES.get(field.key, "text"),
                    "required_for_display": 1,
                    "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                    "source_priority_json": safe_json_dumps(["api", "html", "ai"]),
                    "export_order": order,
                }
            )
        for group, fields in SPECIAL_FIELDS.items():
            for order, field in enumerate(fields, start=10):
                rows.append(
                    {
                        "field_namespace": "special",
                        "asset_group": group,
                        "field_key": field.key,
                        "field_label": field.label,
                        "field_comment": f"资产类型“{ASSET_GROUP_LABELS.get(group, group)}”的特有字段：{field.label}",
                        "data_type": SPECIAL_FIELD_DATA_TYPES.get(group, {}).get(field.key, "text"),
                        "required_for_display": 1,
                        "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                        "source_priority_json": safe_json_dumps(["api", "html", "ai"]),
                        "export_order": order,
                    }
                )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, "field_catalog", rows, mysql_column_types(cur, "field_catalog"))
            conn.commit()

    def start_batch(self, parameters: dict[str, Any]) -> str:
        import uuid as _uuid

        batch_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _uuid.uuid4().hex[:8]
        row = {
            "batch_id": batch_id,
            "source_platform": compact_text(parameters.get("source_platform")) or "jd",
            "started_at": now_text(),
            "parameters_json": safe_json_dumps(parameters),
            "status": "running",
            "summary_json": safe_json_dumps({"parameters": parameters, "status": "running", "message": ""}),
        }
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, "crawl_batches", [row], mysql_column_types(cur, "crawl_batches"))
            conn.commit()
        return batch_id

    def finish_batch(self, batch_id: str, status: str, message: str = "") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT summary_json, parameters_json FROM crawl_batches WHERE batch_id=%s", (batch_id,))
                stored = cur.fetchone() or {}
                summary: dict[str, Any] = {}
                if stored.get("summary_json"):
                    try:
                        summary = json.loads(stored["summary_json"])
                    except (TypeError, json.JSONDecodeError):
                        summary = {}
                if not summary and stored.get("parameters_json"):
                    try:
                        summary["parameters"] = json.loads(stored["parameters_json"])
                    except (TypeError, json.JSONDecodeError):
                        summary["parameters"] = stored["parameters_json"]
                summary.update({"status": status, "message": message})
                cur.execute(
                    """
                    UPDATE crawl_batches
                    SET finished_at=%s, status=%s, message=%s, summary_json=%s
                    WHERE batch_id=%s
                    """,
                    (now_text(), status, message, safe_json_dumps(summary), batch_id),
                )
            conn.commit()

    def upsert_raw_payloads(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        source_url: str,
        source_platform: str = "jd",
        source_item_id: Optional[str] = None,
        source_site_name: Optional[str] = None,
        list_json: Any,
        detail_json: Any,
        realtime_json: Any,
        description_html: Optional[str],
        product_basic_json: Any = None,
        notice_html: Optional[str] = None,
        announcement_html: Optional[str] = None,
        attachments_json: Any = None,
        vendor_json: Any = None,
    ) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_key = source_item_id or paimai_id
                item_id = _ensure_item_stub(
                    cur,
                    item_key,
                    batch_id=batch_id,
                    source_url=source_url,
                    source_platform=source_platform,
                    source_site_name=source_site_name,
                )
                payload_types = [
                    "list_json",
                    "detail_json",
                    "product_basic_json",
                    "realtime_json",
                    "description_html",
                    "notice_html",
                    "announcement_html",
                    "attachments_json",
                    "vendor_json",
                ]
                cur.execute(
                    f"DELETE FROM raw_payloads WHERE item_id=%s AND batch_id=%s AND payload_type IN ({qmarks(len(payload_types))})",
                    [item_id, batch_id, *payload_types],
                )
                payload_id_by_type: dict[str, Optional[int]] = {}
                for payload_type, value in (
                    ("list_json", list_json),
                    ("detail_json", detail_json),
                    ("product_basic_json", product_basic_json or {}),
                    ("realtime_json", realtime_json),
                    ("description_html", description_html or ""),
                    ("notice_html", notice_html or ""),
                    ("announcement_html", announcement_html or ""),
                    ("attachments_json", attachments_json),
                    ("vendor_json", vendor_json or {}),
                ):
                    payload_id_by_type[payload_type] = _insert_payload(
                        cur,
                        item_id=item_id,
                        batch_id=batch_id,
                        payload_type=payload_type,
                        value=value,
                        source_url=source_url,
                        source_platform=source_platform,
                    )
                resources = build_v2_resource_rows(
                    item_id=item_id,
                    attachments_json=attachments_json,
                    source_payload_id=payload_id_by_type.get("attachments_json"),
                )
                if resources:
                    cur.execute("DELETE FROM item_resources WHERE item_id=%s", (item_id,))
                    upsert_rows(cur, "item_resources", resources, mysql_column_types(cur, "item_resources"))
            conn.commit()

    def update_attachment_texts(self, paimai_id: str, attachment_texts: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = _ensure_item_stub(cur, paimai_id)
                _insert_payload(
                    cur,
                    item_id=item_id,
                    batch_id=None,
                    payload_type="attachment_texts",
                    value=attachment_texts,
                    source_url=f"https://paimai.jd.com/{paimai_id}",
                )
            conn.commit()

    def upsert_common_item(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        asset_group: str,
        jd_category_id: str,
        jd_category_name: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any] | None = None,
    ) -> None:
        full_values = {field.key: compact_text(values.get(field.key)) for field in COMMON_FIELDS}
        for meta_key in ("source_platform", "source_item_id", "source_url", "source_site_name"):
            if meta_key in values:
                full_values[meta_key] = compact_text(values.get(meta_key))
        row = build_v2_common_item_row(
            paimai_id=paimai_id,
            batch_id=batch_id,
            asset_group=asset_group,
            jd_category_id=jd_category_id,
            jd_category_name=jd_category_name,
            values=full_values,
            special_values=special_values or {},
            field_results=field_results,
        )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                # 采集队列: 落库前查询旧记录, 用于判定 success/updated/unchanged
                prev_record = self._fetch_prev_common_for_queue(cur, row.get("source_platform"), row.get("source_item_id"))
                _ensure_item_stub(
                    cur,
                    row["source_item_id"],
                    batch_id=batch_id,
                    source_url=row["source_url"],
                    source_platform=row["source_platform"],
                    source_site_name=row.get("source_site_name"),
                )
                upsert_rows(cur, "auction_items", [row], mysql_column_types(cur, "auction_items"))
                item_id = int(_get_item_id(cur, row["source_item_id"], source_platform=row["source_platform"]))
                self._upsert_dedup_index_cur(cur, item_id=item_id, row=row, common_values=full_values)
                self._upsert_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="common",
                    asset_group=asset_group,
                    fields=COMMON_FIELDS,
                    values=full_values,
                    field_results=field_results,
                )
                # 采集队列: 写入标级采集结果 (失败不影响主采集流程)
                try:
                    self._record_crawl_queue_item(cur, batch_id, row, item_id, prev_record)
                except Exception:
                    pass
            conn.commit()

    # ── 增量采集辅助 ──
    def query_existing_source_item_ids(self, source_platform: str) -> set[str]:
        """返回该平台已写入主表(auction_items)的 source_item_id 集合, 供增量采集过滤。"""
        ids: set[str] = set()
        try:
            with mysql_connection(self.config) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT source_item_id FROM auction_items WHERE source_platform=%s",
                        (source_platform,),
                    )
                    for row in cur.fetchall():
                        sid = row.get("source_item_id") if isinstance(row, dict) else row[0]
                        if sid:
                            ids.add(compact_text(sid))
        except Exception:
            pass
        return ids

    def query_list_fingerprints(self, source_platform: str) -> dict[str, str]:
        """返回该平台已知列表指纹: {source_item_id: fingerprint}。"""
        result: dict[str, str] = {}
        try:
            with mysql_connection(self.config) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT source_item_id, fingerprint FROM crawl_list_fingerprints "
                        "WHERE source_platform=%s",
                        (source_platform,),
                    )
                    for row in cur.fetchall():
                        if isinstance(row, dict):
                            sid, fp = row.get("source_item_id"), row.get("fingerprint")
                        else:
                            sid, fp = row[0], row[1]
                        if sid:
                            result[compact_text(sid)] = compact_text(fp)
        except Exception:
            pass
        return result

    def upsert_list_fingerprints(self, rows: list[dict[str, Any]]) -> None:
        """批量写入/更新列表指纹。rows: [{source_platform, source_item_id, fingerprint, updated_at}]"""
        if not rows:
            return
        normalized = [
            {
                "source_platform": compact_text(r.get("source_platform")),
                "source_item_id": compact_text(r.get("source_item_id")),
                "fingerprint": compact_text(r.get("fingerprint")),
                "updated_at": r.get("updated_at") or now_text(),
            }
            for r in rows
            if compact_text(r.get("source_item_id"))
        ]
        if not normalized:
            return
        try:
            with mysql_connection(self.config) as conn:
                with conn.cursor() as cur:
                    upsert_rows(cur, "crawl_list_fingerprints", normalized,
                                mysql_column_types(cur, "crawl_list_fingerprints"))
                conn.commit()
        except Exception:
            pass

    # ── 采集队列(标级) 辅助与查询 ──
    _CRAWL_QUEUE_COMPARE_KEYS = (
        "project_name", "start_price_display", "final_price_display",
        "project_status", "assessment_price_display", "auction_stage",
        "asset_group", "disposal_party", "disposal_agency",
        "asset_location", "contact_info",
    )

    def _fetch_prev_common_for_queue(self, cur, source_platform, source_item_id):
        if not source_item_id:
            return None
        try:
            cur.execute(
                """
                SELECT project_name, start_price_display, final_price_display,
                       project_status, assessment_price_display, auction_stage,
                       asset_group, disposal_party, disposal_agency,
                       asset_location, contact_info, batch_id
                FROM auction_items
                WHERE source_platform=%s AND source_item_id=%s
                """,
                (source_platform, source_item_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return row
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        except Exception:
            return None

    @staticmethod
    def _queue_source_url(source_platform: Optional[str], source_item_id: Optional[str], source_url: Optional[str] = None) -> str:
        source_url = compact_text(source_url)
        if source_url:
            return source_url
        if source_platform == "jd" and source_item_id:
            return f"https://paimai.jd.com/{source_item_id}"
        return ""

    def _record_crawl_queue_item(self, cur, batch_id, row, item_id, prev):
        source_platform = row.get("source_platform")
        source_item_id = row.get("source_item_id")
        if prev is None:
            status = "success"
            changed = None
            prev_batch = None
        else:
            changed = []
            for key in self._CRAWL_QUEUE_COMPARE_KEYS:
                old_val = ("" if prev.get(key) is None else str(prev.get(key))).strip()
                new_val = ("" if row.get(key) is None else str(row.get(key))).strip()
                if old_val != new_val:
                    changed.append({"field": key, "from": old_val, "to": new_val})
            status = "updated" if changed else "unchanged"
            prev_batch = prev.get("batch_id")
        cur.execute(
            """
            INSERT INTO crawl_queue
              (batch_id, source_platform, source_item_id, source_url, item_id,
               queue_status, last_error, discovered_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
              source_url=VALUES(source_url),
              item_id=VALUES(item_id),
              queue_status=VALUES(queue_status),
              last_error=VALUES(last_error),
              updated_at=NOW()
            """,
            (
                batch_id,
                source_platform,
                source_item_id,
                self._queue_source_url(source_platform, source_item_id, row.get("source_url")),
                item_id,
                status,
                None,
            ),
        )

    def save_checkpoint(self, *, source_platform: str, category_key: str = "default",
                        current_page: int = 1, total_items_seen: int = 0,
                        last_item_id: Optional[str] = None) -> None:
        """保存采集断点"""
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO crawl_checkpoints
                        (source_platform, category_key, current_page, total_items_seen, last_item_id, started_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                        current_page = VALUES(current_page),
                        total_items_seen = VALUES(total_items_seen),
                        last_item_id = COALESCE(VALUES(last_item_id), last_item_id),
                        updated_at = NOW()
                    """,
                    (source_platform, category_key, current_page, total_items_seen, last_item_id),
                )
            conn.commit()

    def load_checkpoint(self, *, source_platform: str, category_key: str = "default") -> dict[str, Any] | None:
        """加载采集断点，返回 None 表示没有断点"""
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM crawl_checkpoints WHERE source_platform=%s AND category_key=%s",
                    (source_platform, category_key),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))

    def clear_checkpoint(self, *, source_platform: str, category_key: str = "default") -> None:
        """清除采集断点（全量采集完成后）"""
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM crawl_checkpoints WHERE source_platform=%s AND category_key=%s",
                    (source_platform, category_key),
                )
            conn.commit()

    def write_crawl_queue_item(self, *, batch_id, source_platform, source_item_id,
                               item_id=None, project_name=None, asset_group=None,
                               asset_group_label=None, status, error_message=None,
                               prev_batch_id=None, changed_fields_json=None,
                               source_url=None):
        """Record one crawl queue item in the formal V2 crawl_queue table."""
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO crawl_queue
                      (batch_id, source_platform, source_item_id, source_url, item_id,
                       queue_status, last_error, discovered_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                      source_url=VALUES(source_url),
                      item_id=VALUES(item_id),
                      queue_status=VALUES(queue_status),
                      last_error=VALUES(last_error),
                      updated_at=NOW()
                    """,
                    (
                        batch_id,
                        source_platform,
                        source_item_id,
                        self._queue_source_url(source_platform, source_item_id, source_url),
                        item_id,
                        status,
                        error_message,
                    ),
                )
            conn.commit()

    def get_crawl_queue_stats(self, batch_id=None):
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                where = ""
                params: list[Any] = []
                if batch_id:
                    where = "WHERE batch_id=%s"
                    params.append(batch_id)
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN queue_status='success' THEN 1 ELSE 0 END),0) AS success,
                        COALESCE(SUM(CASE WHEN queue_status='failed' THEN 1 ELSE 0 END),0) AS failed,
                        COALESCE(SUM(CASE WHEN queue_status='updated' THEN 1 ELSE 0 END),0) AS updated,
                        COALESCE(SUM(CASE WHEN queue_status='unchanged' THEN 1 ELSE 0 END),0) AS unchanged,
                        COALESCE(SUM(CASE WHEN queue_status='skipped' THEN 1 ELSE 0 END),0) AS skipped,
                        COUNT(*) AS total
                    FROM crawl_queue
                    """ + (" " + where if where else ""),
                    params,
                )
                row = cur.fetchone()
                if isinstance(row, dict):
                    return row
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row)) if row else {
                    "success": 0, "failed": 0, "updated": 0, "unchanged": 0, "skipped": 0, "total": 0
                }

    def list_crawl_queue_items(self, batch_id=None, status: str = "", page: int = 1, size: int = 20):
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 20)))
        offset = (page - 1) * size
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                where = []
                params: list[Any] = []
                if batch_id:
                    where.append("q.batch_id=%s")
                    params.append(batch_id)
                if status:
                    where.append("q.queue_status=%s")
                    params.append(status)
                where_clause = ("WHERE " + " AND ".join(where)) if where else ""
                cur.execute(f"SELECT COUNT(*) AS cnt FROM crawl_queue q {where_clause}", params)
                count_row = cur.fetchone()
                if isinstance(count_row, dict):
                    total = int(count_row.get("cnt") or 0)
                else:
                    total = int(count_row[0] if count_row else 0)
                cur.execute(
                    f"""
                    SELECT q.*
                    FROM crawl_queue q
                    {where_clause}
                    ORDER BY q.queue_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    params + [size, offset],
                )
                fetched = cur.fetchall() or []
                if not fetched:
                    rows = []
                elif isinstance(fetched[0], dict):
                    rows = list(fetched)
                else:
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in fetched]
        return {"total": total, "page": page, "size": size, "items": rows}

    def upsert_special_item(
        self,
        *,
        paimai_id: str,
        asset_group: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        source_platform: str = "jd",
    ) -> None:
        fields = SPECIAL_FIELDS[asset_group]
        full_values = {field.key: compact_text(values.get(field.key)) for field in fields}
        table = V2_SPECIAL_TABLES[asset_group]
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                row = build_v2_special_row(item_id=item_id, asset_group=asset_group, values=full_values)
                upsert_rows(cur, table, [row], mysql_column_types(cur, table))
                if asset_group == "debt":
                    self._refresh_debt_main_from_details_cur(cur, item_id)
                self._upsert_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="special",
                    asset_group=asset_group,
                    fields=fields,
                    values=full_values,
                    field_results=field_results,
                )
            conn.commit()

    def upsert_debt_details(self, *, paimai_id: str, details: list[dict[str, Any]], source_platform: str = "jd") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute("DELETE FROM asset_debt_details WHERE item_id=%s", (item_id,))
                rows = []
                for index, detail in enumerate(details, start=1):
                    rows.append(
                        {
                            "item_id": item_id,
                            "detail_index": index,
                            "sequence_no": _blank_to_none(detail.get("sequence_no")),
                            "debtor_name": _blank_to_none(detail.get("debtor_name") or detail.get("debtor_or_asset")),
                            "principal_balance_amount": _debt_detail_money_decimal(detail.get("principal_balance"), detail),
                            "interest_balance_amount": _debt_detail_money_decimal(detail.get("interest_balance"), detail),
                            "claim_total_amount": _debt_detail_money_decimal(detail.get("claim_total"), detail),
                            "benchmark_date": date_to_db(detail.get("benchmark_date")),
                            "collateral": _blank_to_none(detail.get("collateral")),
                            "guarantor": _blank_to_none(detail.get("guarantor") or detail.get("guarantor_or_related_party")),
                            "litigation_status": _blank_to_none(detail.get("litigation_status")),
                            "source_excerpt": (_blank_to_none(detail.get("source_excerpt")) or "")[:1000],
                        }
                    )
                upsert_rows(cur, "asset_debt_details", rows, mysql_column_types(cur, "asset_debt_details"))
                self._refresh_debt_main_from_details_cur(cur, item_id)
            conn.commit()

    def upsert_ip_details(self, *, paimai_id: str, details: list[dict[str, Any]], source_platform: str = "jd") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute("DELETE FROM asset_ip_details WHERE item_id=%s", (item_id,))
                rows = []
                for index, detail in enumerate(details, start=1):
                    rows.append(
                        {
                            "item_id": item_id,
                            "detail_index": index,
                            "sequence_no": _blank_to_none(detail.get("sequence_no")) or str(index),
                            "ip_name": _blank_to_none(detail.get("ip_name")),
                            "certificate_no": _blank_to_none(detail.get("certificate_no")),
                            "registration_no": _blank_to_none(detail.get("registration_no")),
                            "acquire_method": _blank_to_none(detail.get("acquire_method")),
                            "application_date": date_to_db(detail.get("application_date")),
                            "approval_date": date_to_db(detail.get("approval_date") or detail.get("application_date")),
                            "ip_type": _blank_to_none(detail.get("ip_type")),
                            "patent_type": _blank_to_none(detail.get("patent_type")),
                            "right_holder": _blank_to_none(detail.get("right_holder")),
                            "right_status": _blank_to_none(detail.get("right_status") or detail.get("status")),
                            "source_excerpt": (_blank_to_none(detail.get("source_excerpt")) or "")[:1000],
                        }
                    )
                upsert_rows(cur, "asset_ip_details", rows, mysql_column_types(cur, "asset_ip_details"))
                if rows:
                    cur.execute(
                        "UPDATE asset_ip SET ip_count=%s WHERE item_id=%s AND (ip_count IS NULL OR ip_count=0)",
                        (len(rows), item_id),
                    )
            conn.commit()

    def enqueue_ocr_retry_task(self, *, paimai_id: str, task: dict[str, Any], source_platform: str = "jd") -> None:
        image_urls = task.get("image_urls") or task.get("resource_urls") or []
        if not image_urls:
            return
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                row = {
                    "item_id": item_id,
                    "source_platform": source_platform,
                    "source_item_id": str(paimai_id),
                    "task_type": compact_text(task.get("task_type")) or "ip_image_details",
                    "resource_urls_json": safe_json_dumps(image_urls),
                    "queue_status": "pending",
                    "priority": int(task.get("priority") or 100),
                    "retry_count": 0,
                    "max_retries": int(task.get("max_retries") or 3),
                    "last_error": _blank_to_none(task.get("reason") or task.get("last_error")),
                    "result_json": None,
                }
                upsert_rows(cur, "ocr_retry_queue", [row], mysql_column_types(cur, "ocr_retry_queue"))
            conn.commit()

    def enqueue_ai_enrichment_task(
        self,
        *,
        paimai_id: str,
        source_platform: str = "jd",
        source_item_id: Optional[str] = None,
        asset_group: str,
        context: dict[str, Any],
        task_type: str = "field_enrichment",
        field_keys: list[str] | None = None,
        priority: int = 100,
        max_retries: int = 3,
        reason: str = "",
    ) -> None:
        item_ref = compact_text(source_item_id) or compact_text(paimai_id)
        if not item_ref:
            raise ValueError("source_item_id is required for AI enrichment queue")
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, item_ref, source_platform=source_platform))
                row = {
                    "item_id": item_id,
                    "source_platform": source_platform,
                    "source_item_id": item_ref,
                    "asset_group": asset_group,
                    "task_type": task_type,
                    "context_json": context,
                    "field_keys_json": field_keys or None,
                    "queue_status": "pending",
                    "priority": priority,
                    "retry_count": 0,
                    "max_retries": max_retries,
                    "last_error": _blank_to_none(reason),
                    "result_json": None,
                }
                upsert_rows(cur, "ai_enrichment_queue", [row], mysql_column_types(cur, "ai_enrichment_queue"))
                # AI 入队后，将采集队列状态改为 pending_ai，表示主采完成、等待 AI。
                cur.execute(
                    """
                    UPDATE crawl_queue
                    SET queue_status='pending_ai', updated_at=NOW()
                    WHERE source_platform=%s AND source_item_id=%s
                      AND queue_status IN ('success', 'updated', 'unchanged')
                    """,
                    (source_platform, item_ref),
                )
            conn.commit()

    def fetch_ai_enrichment_tasks(
        self,
        *,
        limit: int = 20,
        worker_id: str = "ai-worker",
        stale_minutes: int = 30,
        task_types: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, int(limit or 1))
        normalized_task_types: list[str] = []
        for item in task_types or []:
            task_type = compact_text(item)
            if task_type and task_type not in normalized_task_types:
                normalized_task_types.append(task_type)

        task_filter = ""
        filter_params: list[Any] = []
        if normalized_task_types:
            clauses: list[str] = []

            def add_clause(sql: str, *values: Any) -> None:
                clauses.append(sql)
                filter_params.extend(values)

            for task_type in normalized_task_types:
                add_clause("task_type=%s", task_type)
                if task_type == "debt":
                    add_clause("(task_type='field_enrichment' AND asset_group='debt')")
                elif task_type == "vision":
                    add_clause("(task_type IN ('vision','ip_image_details') OR (task_type='field_enrichment' AND asset_group='ip'))")
                elif task_type == "attachment":
                    add_clause("task_type IN ('attachment','attachment_parse')")
                elif task_type == "text":
                    add_clause("(task_type='field_enrichment' AND COALESCE(asset_group,'') NOT IN ('debt','ip'))")
                elif task_type == "long_text":
                    add_clause("task_type='long_text'")
            if clauses:
                task_filter = " AND (" + " OR ".join(clauses) + ")"

        with mysql_connection(self.config) as conn:
            try:
                conn.begin()
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT *
                        FROM ai_enrichment_queue
                        WHERE (
                            queue_status='pending'
                            OR (queue_status='failed' AND retry_count < max_retries)
                            OR (
                                queue_status IN ('running', 'parsing')
                                AND locked_at IS NOT NULL
                                AND locked_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
                            )
                        )
                        {task_filter}
                        ORDER BY priority ASC, ai_task_id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        [stale_minutes, *filter_params, limit],
                    )
                    rows = list(cur.fetchall())
                    if not rows:
                        conn.commit()
                        return []
                    ids = [int(row["ai_task_id"]) for row in rows]
                    cur.execute(
                        f"""
                        UPDATE ai_enrichment_queue
                        SET queue_status='running',
                            locked_by=%s,
                            locked_at=NOW(),
                            running_profile_name=NULL,
                            running_provider=NULL,
                            running_model_name=NULL,
                            updated_at=NOW()
                        WHERE ai_task_id IN ({qmarks(len(ids))})
                        """,
                        [worker_id, *ids],
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return rows

    def mark_ai_enrichment_task_parsing(
        self,
        ai_task_id: int,
        *,
        worker_id: str = "",
        profile_name: str = "",
        provider: str = "",
        model_name: str = "",
    ) -> bool:
        """Move a claimed AI task from running to parsing before the model call.

        Returns False if the task was paused or otherwise changed after claim.
        """
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai_enrichment_queue
                    SET queue_status='parsing',
                        locked_by=COALESCE(NULLIF(%s, ''), locked_by),
                        locked_at=NOW(),
                        running_profile_name=NULLIF(%s, ''),
                        running_provider=NULLIF(%s, ''),
                        running_model_name=NULLIF(%s, ''),
                        last_error=NULL,
                        updated_at=NOW()
                    WHERE ai_task_id=%s
                      AND queue_status='running'
                    """,
                    (worker_id, profile_name, provider, model_name, ai_task_id),
                )
                affected = cur.rowcount
            conn.commit()
        return affected == 1

    def mark_ai_enrichment_task_success(self, ai_task_id: int, result_json: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai_enrichment_queue
                    SET queue_status='success',
                        result_json=%s,
                        locked_by=NULL,
                        locked_at=NULL,
                        last_error=NULL,
                        updated_at=NOW()
                    WHERE ai_task_id=%s
                    """,
                    (safe_json_dumps(result_json), ai_task_id),
                )
                # AI 完成后，更新采集队列状态。
                cur.execute(
                    """
                    UPDATE crawl_queue cq
                    JOIN ai_enrichment_queue aq ON aq.source_platform = cq.source_platform
                      AND aq.source_item_id = cq.source_item_id
                    SET cq.queue_status = 'success',
                        cq.updated_at = NOW()
                    WHERE aq.ai_task_id = %s
                      AND cq.queue_status = 'pending_ai'
                    """,
                    (ai_task_id,),
                )
            conn.commit()

    def mark_ai_enrichment_task_failed(self, ai_task_id: int, error: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai_enrichment_queue
                    SET queue_status=IF(retry_count + 1 >= max_retries, 'failed', 'pending'),
                        retry_count=retry_count + 1,
                        locked_by=NULL,
                        locked_at=NULL,
                        running_profile_name=NULL,
                        running_provider=NULL,
                        running_model_name=NULL,
                        last_error=%s,
                        updated_at=NOW()
                    WHERE ai_task_id=%s
                    """,
                    (compact_text(error)[:4000], ai_task_id),
                )
                # 如果 AI 已耗尽重试次数（永久失败），将 crawl_queue 从 pending_ai 恢复为 success
                if mysql_table_exists(cur, "crawl_queue"):
                    cur.execute(
                        """
                        UPDATE crawl_queue cq
                        JOIN ai_enrichment_queue aq ON aq.source_platform = cq.source_platform
                          AND aq.source_item_id = cq.source_item_id
                        SET cq.queue_status = 'success',
                            cq.last_error = %s,
                            cq.updated_at = NOW()
                        WHERE aq.ai_task_id = %s
                          AND cq.queue_status = 'pending_ai'
                          AND aq.retry_count >= aq.max_retries
                        """,
                        (compact_text(error)[:2000], ai_task_id),
                    )
            conn.commit()

    def apply_ai_enrichment_results(
        self,
        *,
        paimai_id: str,
        source_platform: str,
        asset_group: str,
        common_values: dict[str, Any],
        common_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any],
        special_results: dict[str, dict[str, Any]],
        debt_details: list[dict[str, Any]] | None = None,
        ip_details: list[dict[str, Any]] | None = None,
    ) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute(
                    """
                    SELECT asset_type, project_name, data_source, start_price_display, final_price_display
                    FROM auction_items
                    WHERE item_id=%s
                    """,
                    (item_id,),
                )
                existing_item = cur.fetchone() or {}
                common_update = self._ai_common_update_row(
                    common_values,
                    common_results,
                    special_values,
                    asset_group,
                    existing_item=existing_item,
                )
                update_row_columns(
                    cur,
                    "auction_items",
                    "item_id",
                    item_id,
                    common_update,
                    mysql_column_types(cur, "auction_items"),
                )
                self._upsert_selected_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="common",
                    asset_group=asset_group,
                    fields=tuple(
                        field
                        for field in COMMON_FIELDS
                        if field.key in common_values and field.key not in {"attachments_json"}
                    ),
                    values=common_values,
                    field_results=common_results,
                )
                if asset_group in V2_SPECIAL_TABLES and special_values:
                    table = V2_SPECIAL_TABLES[asset_group]
                    special_row = build_v2_special_row(item_id=item_id, asset_group=asset_group, values=special_values)
                    special_row = {key: value for key, value in special_row.items() if key == "item_id" or value is not None}
                    if len(special_row) > 1:
                        upsert_rows(cur, table, [special_row], mysql_column_types(cur, table))
                    self._upsert_selected_field_extractions_cur(
                        cur,
                        item_id=item_id,
                        namespace="special",
                        asset_group=asset_group,
                        fields=tuple(field for field in SPECIAL_FIELDS.get(asset_group, ()) if field.key in special_values),
                        values=special_values,
                        field_results=special_results,
                    )
            conn.commit()
        if debt_details:
            self.upsert_debt_details(paimai_id=paimai_id, source_platform=source_platform, details=debt_details)
        if ip_details:
            self.upsert_ip_details(paimai_id=paimai_id, source_platform=source_platform, details=ip_details)

    def export_csvs(self, output_dir: Path) -> dict[str, Path]:
        return {}

    def _ai_common_update_row(
        self,
        common_values: dict[str, Any],
        common_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any],
        asset_group: str,
        existing_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        existing_item = existing_item or {}
        direct_columns = {
            "asset_type": "asset_type",
            "asset_location": "asset_location",
            "project_status": "project_status",
            "project_status_basis": "project_status_basis",
            "auction_stage": "auction_stage",
            "data_source": "data_source",
            "project_name": "project_name",
            "signup_start_time": "signup_start_time",
            "signup_end_time": "signup_end_time",
            "disposal_party": "disposal_party",
            "disposal_agency": "disposal_agency",
            "contact_info": "contact_info",
            "special_notice": "special_notice",
        }
        for field_key, column in direct_columns.items():
            value = common_values.get(field_key)
            if not compact_text(value):
                continue
            if field_key in {"asset_type", "project_name", "data_source"} and compact_text(existing_item.get(column)):
                continue
            if field_key in {"signup_start_time", "signup_end_time"}:
                row[column] = datetime_to_db(value)
            else:
                row[column] = _blank_to_none(value)
        if compact_text(common_values.get("bid_records_json")):
            row["bid_records_json"] = _json_or_none(_parse_json_maybe(common_values.get("bid_records_json")) or [])
            row["bid_records_count"] = _bid_count(common_values.get("bid_records_json"))
        start_display = _blank_to_none(common_values.get("start_price_raw"))
        if start_display:
            row["start_price_display"] = start_display
            row["start_price_amount"] = _money_decimal(start_display)
            if not _blank_to_none(common_values.get("final_price_raw")) and not _blank_to_none(
                existing_item.get("final_price_display")
            ):
                row["final_price_display"] = start_display
                row["final_price_amount"] = _money_decimal(start_display)
                row["price_basis"] = "start_price_fallback_after_ai"
        final_display = _blank_to_none(common_values.get("final_price_raw"))
        if final_display and _valid_price_display(final_display):
            row["final_price_display"] = final_display
            row["final_price_amount"] = _money_decimal(final_display)
            row["price_basis"] = "ai_extracted_effective_price"
        defects = _blank_to_none(special_values.get("disclosed_defects"))
        if defects:
            row["disclosed_defects"] = defects
        if asset_group != "debt":
            right_holder = _blank_to_none(
                special_values.get("right_holder")
                or special_values.get("original_right_holder")
                or common_values.get("right_holder")
            )
            if right_holder:
                row["right_holder"] = right_holder
        assessment_text = _blank_to_none(common_values.get("assessment_price_time"))
        if assessment_text:
            result = common_results.get("assessment_price_time", {})
            if _valid_assessment_text(
                assessment_text,
                source_excerpt=result.get("source_excerpt"),
                source_payload_type=compact_text(result.get("source_payload_type")) or "",
                source_path=compact_text(result.get("source_path")) or "",
            ):
                row["assessment_price_amount"] = _money_decimal(assessment_text)
                row["assessment_price_display"] = assessment_text
                row["assessment_price_basis"] = _assessment_basis(assessment_text)
                row["assessment_date"] = (
                    date_to_db(assessment_text) if has_assessment_date_text(assessment_text) else None
                )
        return row

    def _upsert_selected_field_extractions_cur(
        self,
        cur,
        *,
        item_id: int,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        rows: list[dict[str, Any]] = []
        for field in fields:
            value = values.get(field.key)
            value_text = compact_text(value)
            if not value_text:
                continue
            result = field_results.get(field.key, {})
            typed_values = typed_field_extraction_values(field.key, value)
            rows.append(
                {
                    "item_id": item_id,
                    "field_namespace": namespace,
                    "asset_group": asset_group,
                    "field_key": field.key,
                    "field_label": field.label,
                    "display_value": value_text,
                    "normalized_text": value_text,
                    "numeric_value": typed_values.get("numeric_value"),
                    "date_value": typed_values.get("date_value"),
                    "datetime_value": typed_values.get("datetime_value"),
                    "value_unit": "元" if typed_values.get("value_type") == "money" else None,
                    "method": result.get("method") or "ai",
                    "source_payload_type": result.get("source_payload_type", "ai_extraction"),
                    "source_tab": _payload_tab(result.get("source_payload_type", "ai_extraction")),
                    "source_path": result.get("source_path", "llm_batch"),
                    "source_excerpt": compact_text(result.get("source_excerpt")),
                    "confidence": float(result.get("confidence", 0.75)),
                    "status": "extracted",
                    "is_selected": 1,
                    "missing_reason": "",
                }
            )
        if not rows:
            return
        keys = [row["field_key"] for row in rows]
        cur.execute(
            f"""
            UPDATE field_extractions
            SET is_selected=0
            WHERE item_id=%s AND field_namespace=%s AND asset_group=%s
              AND field_key IN ({qmarks(len(keys))})
            """,
            [item_id, namespace, asset_group, *keys],
        )
        cur.execute(
            f"""
            DELETE FROM field_extractions
            WHERE item_id=%s AND field_namespace=%s AND asset_group=%s
              AND method IN ('ai', 'ai_derived', 'ai_async')
              AND field_key IN ({qmarks(len(keys))})
            """,
            [item_id, namespace, asset_group, *keys],
        )
        upsert_rows(cur, "field_extractions", rows, mysql_column_types(cur, "field_extractions"))

    def _upsert_dedup_index_cur(
        self,
        cur,
        *,
        item_id: int,
        row: dict[str, Any],
        common_values: dict[str, Any],
    ) -> None:
        dedup_hash = compact_text(row.get("dedup_hash"))
        if not dedup_hash:
            return
        identity_basis = {
            "project_name": common_values.get("project_name"),
            "asset_location": common_values.get("asset_location"),
            "source_item_id": row.get("source_item_id"),
        }
        upsert_rows(
            cur,
            "asset_dedup_index",
            [
                {
                    "item_id": item_id,
                    "source_platform": row.get("source_platform") or "jd",
                    "source_item_id": row.get("source_item_id"),
                    "dedup_hash": dedup_hash,
                    "asset_group": row.get("asset_group"),
                    "project_name": row.get("project_name"),
                    "asset_location": row.get("asset_location"),
                    "identity_basis_json": safe_json_dumps(identity_basis),
                    "duplicate_status": "unique",
                }
            ],
            mysql_column_types(cur, "asset_dedup_index"),
        )

    def _refresh_debt_main_from_details_cur(self, cur, item_id: int) -> None:
        cur.execute(
            """
            SELECT
              GROUP_CONCAT(DISTINCT debtor_name ORDER BY detail_index SEPARATOR '、') AS debtor_names,
              SUM(principal_balance_amount) AS principal_balance_amount,
              SUM(interest_balance_amount) AS interest_balance_amount,
              SUM(claim_total_amount) AS claim_total_amount,
              COUNT(*) AS household_count
            FROM asset_debt_details
            WHERE item_id=%s
            """,
            (item_id,),
        )
        summary = cur.fetchone() or {}
        if not summary.get("household_count"):
            return
        updates = {
            "item_id": item_id,
            "debtor_names": summary.get("debtor_names"),
            "main_debtor_name": (summary.get("debtor_names") or "").split("、", 1)[0] or None,
            "principal_balance_amount": summary.get("principal_balance_amount"),
            "interest_balance_amount": summary.get("interest_balance_amount"),
            "claim_total_amount": summary.get("claim_total_amount"),
            "household_count": summary.get("household_count"),
        }
        upsert_rows(cur, "asset_debt", [updates], mysql_column_types(cur, "asset_debt"))

    def _upsert_field_extractions_cur(
        self,
        cur,
        *,
        item_id: int,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        cur.execute(
            "DELETE FROM field_extractions WHERE item_id=%s AND field_namespace=%s AND asset_group=%s",
            (item_id, namespace, asset_group),
        )
        rows: list[dict[str, Any]] = []
        for field in fields:
            value = values.get(field.key)
            result = field_results.get(field.key, {})
            value_text = compact_text(value)
            status = result.get("status") or ("extracted" if value_text else "missing")
            if status == "missing_on_page":
                status = "missing"
            typed_values = typed_field_extraction_values(field.key, value)
            rows.append(
                {
                    "item_id": item_id,
                    "field_namespace": namespace,
                    "asset_group": asset_group,
                    "field_key": field.key,
                    "field_label": field.label,
                    "display_value": value_text,
                    "normalized_text": value_text,
                    "numeric_value": typed_values.get("numeric_value"),
                    "date_value": typed_values.get("date_value"),
                    "datetime_value": typed_values.get("datetime_value"),
                    "value_unit": "元" if typed_values.get("value_type") == "money" else None,
                    "method": result.get("method") or ("not_found" if not value_text else "api_or_html"),
                    "source_payload_type": result.get("source_payload_type", ""),
                    "source_tab": _payload_tab(result.get("source_payload_type", "")),
                    "source_path": result.get("source_path", ""),
                    "source_excerpt": compact_text(result.get("source_excerpt")),
                    "confidence": float(result.get("confidence", 0.95 if value_text else 0.0)),
                    "status": status,
                    "is_selected": 1 if value_text else 0,
                    "missing_reason": "" if value_text else result.get("missing_reason", "页面或接口未提供该字段"),
                }
            )
        upsert_rows(cur, "field_extractions", rows, mysql_column_types(cur, "field_extractions"))


def table_comments_mysql(cur, table: str) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT COLUMN_NAME AS column_name, COLUMN_COMMENT AS comment
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table,),
    )
    return {
        row["column_name"]: {
            "column_name": row["column_name"],
            "comment": row.get("comment") or "",
        }
        for row in cur.fetchall()
    }


def _raw_payload_map(cur, item_id: int) -> dict[str, Any]:
    cur.execute(
        "SELECT * FROM raw_payloads WHERE item_id=%s ORDER BY payload_id",
        (item_id,),
    )
    raw: dict[str, Any] = {}
    for row in cur.fetchall():
        payload_type = row["payload_type"]
        if payload_type.endswith("_json"):
            raw[payload_type] = row.get("payload_json")
        else:
            raw[payload_type] = row.get("payload_text")
    return raw


def parse_source_item_ref(value: Any) -> tuple[Optional[str], str]:
    text = str(value or "").strip()
    if ":" in text:
        platform, source_item_id = text.split(":", 1)
        platform = platform.strip()
        source_item_id = source_item_id.strip()
        if platform and source_item_id:
            return platform, source_item_id
    return None, text


def get_items_mysql(config: MySQLConfig, filters: dict[str, str] | None = None) -> dict[str, Any]:
    filters = filters or {}
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("asset_group"):
        clauses.append("c.asset_group = %s")
        params.append(filters["asset_group"])
    if filters.get("source_platform"):
        clauses.append("c.source_platform = %s")
        params.append(filters["source_platform"])
    if filters.get("project_status"):
        clauses.append("c.project_status = %s")
        params.append(filters["project_status"])
    if filters.get("q"):
        like = f"%{filters['q']}%"
        clauses.append(
            """
            (
              c.project_name LIKE %s
              OR c.asset_location LIKE %s
              OR c.disposal_party LIKE %s
              OR EXISTS (
                SELECT 1 FROM field_extractions fx
                WHERE fx.item_id = c.item_id
                  AND (fx.display_value LIKE %s OR fx.source_excerpt LIKE %s)
              )
            )
            """
        )
        params.extend([like, like, like, like, like])
    if filters.get("issue") == "missing":
        clauses.append("EXISTS (SELECT 1 FROM field_extractions fm WHERE fm.item_id=c.item_id AND fm.status!='extracted')")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  CONCAT(c.source_platform, ':', c.source_item_id) AS paimai_id,
                  c.source_platform,
                  c.source_site_name,
                  c.source_item_id,
                  c.asset_group,
                  c.asset_group_label,
                  c.source_category_id AS jd_category_id,
                  c.source_category_name AS jd_category_name,
                  c.project_name,
                  c.asset_location,
                  c.project_status,
                  c.start_price_display AS start_price_raw,
                  COALESCE(NULLIF(c.final_price_display, ''), NULLIF(c.start_price_display, '')) AS final_price_raw,
                  c.disposal_party,
                  COUNT(fe.field_key) AS total_fields,
                  SUM(CASE WHEN fe.status='extracted' THEN 1 ELSE 0 END) AS extracted_fields,
                  SUM(CASE WHEN fe.status!='extracted' THEN 1 ELSE 0 END) AS issue_fields
                FROM auction_items c
                LEFT JOIN field_extractions fe ON fe.item_id = c.item_id
                {where}
                GROUP BY c.item_id
                ORDER BY c.source_platform, CAST(c.source_category_id AS UNSIGNED), c.source_item_id
                """,
                params,
            )
            items = cur.fetchall()
            cur.execute(
                "SELECT asset_group, asset_group_label, COUNT(*) AS count "
                "FROM auction_items GROUP BY asset_group, asset_group_label ORDER BY asset_group"
            )
            asset_groups = cur.fetchall()
            cur.execute(
                """
                SELECT
                  source_platform,
                  COALESCE(NULLIF(MAX(source_site_name), ''), source_platform) AS source_site_name,
                  COUNT(*) AS count
                FROM auction_items
                GROUP BY source_platform
                ORDER BY source_platform
                """
            )
            source_platforms = cur.fetchall()
            cur.execute(
                "SELECT DISTINCT project_status FROM auction_items "
                "WHERE project_status IS NOT NULL AND project_status!='' ORDER BY project_status"
            )
            statuses = [row["project_status"] for row in cur.fetchall()]
    return {"items": items, "asset_groups": asset_groups, "source_platforms": source_platforms, "statuses": statuses}


def get_item_detail_mysql(config: MySQLConfig, paimai_id: str) -> dict[str, Any]:
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            platform_hint, source_item_id = parse_source_item_ref(paimai_id)
            if platform_hint:
                item_where = "c.source_platform=%s AND c.source_item_id=%s"
                item_params: tuple[Any, ...] = (platform_hint, source_item_id)
                item_order = ""
            else:
                item_where = "c.source_item_id=%s"
                item_params = (source_item_id,)
                if source_item_id.isdigit():
                    item_order = "ORDER BY CASE WHEN c.source_platform='jd' THEN 0 ELSE 1 END, c.item_id DESC"
                else:
                    item_order = "ORDER BY c.item_id DESC"
            cur.execute(
                f"""
                SELECT
                  c.*,
                  CONCAT(c.source_platform, ':', c.source_item_id) AS paimai_id,
                  c.source_category_id AS jd_category_id,
                  c.source_category_name AS jd_category_name,
                  c.start_price_display AS start_price_raw,
                  COALESCE(NULLIF(c.final_price_display, ''), NULLIF(c.start_price_display, '')) AS final_price_raw
                FROM auction_items c
                WHERE {item_where}
                {item_order}
                LIMIT 1
                """,
                item_params,
            )
            item = cur.fetchone()
            if item is None:
                raise KeyError(f"找不到标的：{paimai_id}")
            item_id = int(item["item_id"])
            group = item["asset_group"]
            special_table = V2_SPECIAL_TABLES[group]
            cur.execute(f"SELECT * FROM `{special_table}` WHERE item_id=%s", (item_id,))
            special_row = cur.fetchone() or {}
            cur.execute(
                """
                SELECT *
                FROM item_resources
                WHERE item_id=%s
                ORDER BY resource_type, resource_role, resource_id
                """,
                (item_id,),
            )
            resources = cur.fetchall()
            debt_details: list[dict[str, Any]] = []
            if group == "debt":
                cur.execute(
                    """
                    SELECT
                      *,
                      CAST(principal_balance_amount AS CHAR) AS principal_balance,
                      CAST(interest_balance_amount AS CHAR) AS interest_balance,
                      CAST(claim_total_amount AS CHAR) AS claim_total,
                      NULL AS recovery_fee
                    FROM asset_debt_details
                    WHERE item_id=%s
                    ORDER BY detail_index
                    """,
                    (item_id,),
                )
                debt_details = cur.fetchall()
            ip_details: list[dict[str, Any]] = []
            if group == "ip":
                cur.execute(
                    """
                    SELECT *, right_status AS status
                    FROM asset_ip_details
                    WHERE item_id=%s
                    ORDER BY detail_index
                    """,
                    (item_id,),
                )
                ip_details = cur.fetchall()
            raw = _raw_payload_map(cur, item_id)
            duplicates: list[dict[str, Any]] = []
            if item.get("dedup_hash"):
                cur.execute(
                    """
                    SELECT d.source_platform, d.source_item_id,
                           CONCAT(d.source_platform, ':', d.source_item_id) AS paimai_id,
                           d.asset_group, d.project_name, d.asset_location, d.updated_at
                    FROM asset_dedup_index d
                    JOIN auction_items i ON i.item_id = d.item_id
                    WHERE d.dedup_hash = %s
                      AND d.item_id != %s
                    ORDER BY d.updated_at DESC
                    """,
                    (item["dedup_hash"], item_id),
                )
                duplicates = cur.fetchall()
            common_comments = table_comments_mysql(cur, "auction_items")
            special_comments = table_comments_mysql(cur, special_table)

            def load_fields(namespace: str, asset_group_filter: str, comments: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
                cur.execute(
                    """
                    SELECT fe.*, fc.export_order
                    FROM (
                        SELECT *,
                            ROW_NUMBER() OVER (
                                PARTITION BY field_key
                                ORDER BY is_selected DESC, extraction_id DESC
                            ) AS rn
                        FROM field_extractions
                        WHERE item_id = %s AND field_namespace = %s AND asset_group = %s
                    ) fe
                    LEFT JOIN field_catalog fc
                      ON fc.field_namespace = fe.field_namespace
                     AND fc.asset_group = CASE WHEN fe.field_namespace='common' THEN 'ALL' ELSE fe.asset_group END
                     AND fc.field_key = fe.field_key
                    WHERE fe.rn = 1
                    ORDER BY COALESCE(fc.export_order, 999), fe.field_key
                    """,
                    (item_id, namespace, asset_group_filter),
                )
                fields = []
                for row in cur.fetchall():
                    comment = comments.get(row["field_key"])
                    fields.append(
                        {
                            "key": row["field_key"],
                            "label": row["field_label"],
                            "comment": comment["comment"] if comment and comment.get("comment") else "字段说明未配置。",
                            "value": row["display_value"],
                            "raw_value": row["display_value"],
                            "status": row["status"],
                            "status_label": row["status"],
                            "method": row["method"],
                            "confidence": row["confidence"],
                            "source_payload_type": row["source_payload_type"],
                            "source_path": row["source_path"],
                            "source_excerpt": row["source_excerpt"],
                            "missing_reason": row["missing_reason"],
                        }
                    )
                return fields

            common_fields = load_fields("common", group, common_comments)
            special_fields = load_fields("special", group, special_comments)

    return {
        "item": item,
        "special_row": special_row,
        "raw": raw,
        "common_fields": common_fields,
        "special_fields": special_fields,
        "resources": resources,
        "debt_details": debt_details,
        "ip_details": ip_details,
        "duplicates": duplicates,
        "asset_group_label": ASSET_GROUP_LABELS.get(group, group),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="初始化或重建拍卖采集 V2 MySQL 正式表")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument("--database", default="auction_data")
    parser.add_argument("--reset", action="store_true", help="删除并重建 V2 MySQL 正式表，同时清理已废弃旧表")
    parser.add_argument("--confirm-reset", action="store_true", help="与 --reset 同时使用，确认执行删除重建")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = MySQLConfig(args.host, args.port, args.user, args.password, args.database)
    if args.reset:
        if not args.confirm_reset:
            raise SystemExit("--reset 会删除并重建 V2 MySQL 表，请同时传入 --confirm-reset")
        reset_mysql_tables(config)
        action = "reset"
    else:
        ensure_mysql_schema(config)
        action = "ensure"
    print(json.dumps({"action": action, "database": config.database, "tables": mysql_table_names()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
