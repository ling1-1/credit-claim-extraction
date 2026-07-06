from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urljoin

import requests

from jd.ai_config import load_default_dotenv

load_default_dotenv()

import jd_scraper_v2 as jd_v2
from jd.ai_extractor import AIExtractionContext
from jd_mysql_store import MySQLConfig, MySQLJDScraperDatabase, mysql_connection, reset_mysql_tables
from platform_adapters.ali_adapter import (
    ALI_DEFAULT_DETAIL_URL,
    ALI_SOURCE_PLATFORM,
    AliAuctionAdapter,
    AliDetailBundle,
    AliListItem,
)
from platform_adapters.cquae_adapter import (
    CQUAE_BASE_URL,
    CQUAE_DATA_SOURCE,
    CQUAE_PLATFORM,
    SDCQJY_DATA_SOURCE,
    SDCQJY_LIST_ENDPOINT,
    CquaeAdapter,
    CquaeBrowserFetcher,
    CquaeDetailBundle,
    CquaeListItem,
)
from platform_adapters.ejy365_adapter import (
    DEFAULT_BASE_URL as EJY365_BASE_URL,
    Ejy365Adapter,
    Ejy365DetailBundle,
    Ejy365ListItem,
)
from platform_adapters.tpre_adapter import (
    TPRE_BASE_URL,
    TPRE_DATA_SOURCE,
    TPRE_PLATFORM,
    TpreAdapter,
    TpreDetailBundle,
    TpreListItem,
)
from platform_adapters.prechina_adapter import (
    PRECHINA_BASE_URL,
    PRECHINA_DATA_SOURCE,
    PRECHINA_PLATFORM,
    PrechinaAdapter,
    PrechinaDetailBundle,
    PrechinaListItem,
)
from platform_adapters.gxcq_adapter import (
    GXCQ_BASE_URL,
    GXCQ_DATA_SOURCE,
    GXCQ_PLATFORM,
    GxcqAdapter,
    GxcqDetailBundle,
    GxcqListItem,
)
from platform_adapters.jd_adapter import (
    JD_DATA_SOURCE,
    JD_SOURCE_PLATFORM,
    JDPlatformAdapter,
)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

AI_UNVERIFIED_COMMON_FIELDS = {
    "attachments_json",
}

AI_FILL_ONLY_COMMON_FIELDS = {
    "asset_type",
    "project_name",
    "data_source",
    "source_platform",
    "source_item_id",
    "source_url",
    "source_site_name",
    "start_price_raw",
    "final_price_raw",
    "bid_records_json",
}


EJY365_PROJECT_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "ZQ": ("debt", "债权"),
    "FC": ("real_estate", "房地产"),
    "FCZZ": ("real_estate", "房产租赁"),
    "TD": ("land", "土地"),
    "CL": ("vehicle", "车辆"),
    "GQ": ("equity", "股权"),
    "ZSCQ": ("ip", "知识产权"),
    "WZ": ("goods", "物资产品"),
    "ZYSYQ": ("usufruct", "用益物权"),
    "KQ": ("usufruct", "矿权"),
    "LQ": ("usufruct", "林权"),
    "HKQ": ("usufruct", "海域使用权"),
    "QT": ("other", "其他"),
}
DEFAULT_EJY365_PROJECT_TYPES = ("ZQ", "FC", "CL", "GQ", "TD", "ZSCQ", "WZ", "ZYSYQ", "QT")


def ejy365_asset_for_project_type(project_type: Any) -> tuple[str, str]:
    code = compact_text(project_type).upper()
    return EJY365_PROJECT_TYPE_LABELS.get(code, ("other", "其他"))


@dataclass
class PlatformRecord:
    source_platform: str
    source_site_name: str
    source_item_id: str
    source_url: str
    asset_group: str
    category_id: str = ""
    category_name: str = ""
    common_values: dict[str, Any] = field(default_factory=dict)
    field_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    special_values: dict[str, Any] = field(default_factory=dict)
    special_field_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_payloads: dict[str, Any] = field(default_factory=dict)
    attachments_json: Any = None
    debt_details: list[dict[str, Any]] = field(default_factory=list)
    ip_details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlatformCrawlResult:
    platform: str
    batch_id: str
    scanned_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


class PlatformHandler(Protocol):
    source_platform: str
    source_site_name: str

    def fetch_list(self, limit: int) -> list[Any]:
        ...

    def fetch_detail(self, list_item: Any) -> Any:
        ...

    def build_record(self, detail_bundle: Any) -> PlatformRecord:
        ...


def request_timeout_value(timeout: int | float | None) -> int | float | None:
    if timeout is None:
        return None
    try:
        numeric = float(timeout)
    except (TypeError, ValueError):
        return timeout
    return None if numeric <= 0 else timeout


def compact_text(value: Any) -> str:
    return jd_v2.compact_text(value)


def safe_json(value: Any) -> str:
    return jd_v2.safe_json_dumps(value)


def make_field_result(
    value: Any,
    source_payload_type: str,
    source_path: str,
    excerpt: Any = None,
    *,
    method: str = "html_rule",
    confidence: float | None = None,
) -> dict[str, Any]:
    return jd_v2.field_result_value(
        value,
        source_payload_type,
        source_path,
        compact_text(excerpt) if excerpt is not None else None,
        method=method,
        confidence=confidence,
    )


def normalize_attachments_payload(files: Any = None, media: Any = None) -> dict[str, Any]:
    def parse_payload(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def iter_values(value: Any) -> Iterable[Any]:
        value = parse_payload(value)
        if isinstance(value, Mapping):
            yield value
            for item in value.values():
                yield from iter_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from iter_values(item)

    def clean_url(value: Any) -> str:
        url = compact_text(value)
        if not url:
            return ""
        if url.lower().startswith(("javascript:", "about:", "#")):
            return ""
        return url

    def file_url(entry: Mapping[str, Any]) -> str:
        for key in (
            "url",
            "href",
            "attachmentAddress",
            "fileUrl",
            "downloadUrl",
            "downloadURL",
            "attachmentUrl",
            "filePath",
            "path",
            "src",
            "resourceUrl",
            "resourceURL",
            "ossUrl",
            "previewUrl",
        ):
            url = clean_url(entry.get(key))
            if url:
                return url
        return ""

    def normalize_file(entry: Any) -> dict[str, Any] | None:
        if isinstance(entry, str):
            url = clean_url(entry)
            if not url.startswith(("http://", "https://", "//")):
                return None
            return {"name": url.rsplit("/", 1)[-1], "url": url}
        if not isinstance(entry, Mapping):
            return None
        url = file_url(entry)
        if not url:
            return None
        normalized = dict(entry)
        normalized["url"] = url
        if not compact_text(normalized.get("name")):
            normalized["name"] = jd_v2.first_non_blank(
                normalized.get("attachmentName"),
                normalized.get("fileName"),
                normalized.get("file_name"),
                normalized.get("title"),
                normalized.get("label"),
                normalized.get("attachmentCode"),
            ) or url.rsplit("/", 1)[-1]
        return normalized

    def contains_media_marker(value: Any) -> bool:
        value = parse_payload(value)
        if isinstance(value, Mapping):
            for key, item in value.items():
                low = str(key).lower()
                if low in {
                    "media",
                    "imagevideoarea",
                    "imagelist",
                    "videolist",
                    "imagepath",
                    "imageurl",
                    "imgurl",
                    "picurl",
                    "videopath",
                    "videourl",
                }:
                    return True
                if contains_media_marker(item):
                    return True
        elif isinstance(value, list):
            return any(contains_media_marker(item) for item in value)
        return False

    def contains_media_reference(value: Any) -> bool:
        value = parse_payload(value)
        if isinstance(value, Mapping):
            for key, item in value.items():
                low = str(key).lower()
                if low in {
                    "imagepath",
                    "imageurl",
                    "imgurl",
                    "picurl",
                    "src",
                    "videopath",
                    "videourl",
                    "video",
                } and compact_text(item):
                    return True
                if contains_media_reference(item):
                    return True
        elif isinstance(value, list):
            return any(contains_media_reference(item) for item in value)
        return False

    files_value = parse_payload(files)
    file_candidates: list[Any] = []
    if isinstance(files_value, Mapping):
        for key in ("files", "data", "attachments", "attachmentList", "attachList", "attachFiles", "fileList", "docs", "documents"):
            item = files_value.get(key)
            if item is not None:
                file_candidates.append(item)
        file_candidates.append(files_value)
    elif isinstance(files_value, list):
        file_candidates.append(files_value)
    elif files_value:
        file_candidates.append(files_value)

    normalized_files: list[dict[str, Any]] = []
    seen_file_urls: set[str] = set()
    for candidate in file_candidates:
        for entry in iter_values(candidate):
            normalized = normalize_file(entry)
            if not normalized:
                continue
            url = normalized["url"]
            if url in seen_file_urls:
                continue
            seen_file_urls.add(url)
            normalized_files.append(normalized)
    payload: dict[str, Any] = {"files": normalized_files}

    media_values: list[Any] = []
    if isinstance(files_value, Mapping) and files_value.get("media") is not None and contains_media_reference(files_value.get("media")):
        media_values.append(files_value.get("media"))
    if files_value is not None and contains_media_marker(files_value) and contains_media_reference(files_value):
        media_values.append(files_value)
    media_value = parse_payload(media)
    if media_value and contains_media_reference(media_value):
        media_values.append(media_value)
    payload["media"] = media_values
    return payload


def common_results_from_values(values: Mapping[str, Any], source_type: str, source_path: str) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    common_keys = {field.key for field in jd_v2.COMMON_FIELDS}
    for key, value in values.items():
        if key not in common_keys:
            continue
        results[key] = make_field_result(value, source_type, f"{source_path}.{key}", value)
    return results


def special_results_from_values(
    asset_group: str,
    values: Mapping[str, Any],
    source_type: str,
    source_path: str,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    special_keys = {field.key for field in jd_v2.SPECIAL_FIELDS.get(asset_group, ())}
    for key, value in values.items():
        if key not in special_keys:
            continue
        results[key] = make_field_result(value, source_type, f"{source_path}.{key}", value)
    return results


def split_ai_results(
    ai_results: Mapping[str, Any],
    asset_group: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    common_keys = {field.key for field in jd_v2.COMMON_FIELDS}
    special_keys = {field.key for field in jd_v2.SPECIAL_FIELDS.get(asset_group, ())}
    if asset_group == "other":
        special_keys.add("extracted_summary")

    common_values: dict[str, Any] = {}
    common_results: dict[str, dict[str, Any]] = {}
    special_values: dict[str, Any] = {}
    special_results: dict[str, dict[str, Any]] = {}
    for key, result in ai_results.items():
        if key in AI_UNVERIFIED_COMMON_FIELDS:
            continue
        value = getattr(result, "value", None)
        if jd_v2.is_blank(value):
            continue
        field_result = make_field_result(
            value,
            "ai_extraction",
            "llm_batch",
            getattr(result, "original_text", "") or getattr(result, "reasoning", "") or value,
            method="ai",
            confidence=getattr(result, "confidence", 0.75),
        )
        if key in common_keys:
            common_values[key] = value
            common_results[key] = field_result
        elif key in special_keys:
            special_values[key] = value
            special_results[key] = field_result
    return common_values, common_results, special_values, special_results


def ai_result_to_field_result(result: Any, *, source_path: str, method: str = "ai") -> dict[str, Any]:
    value = getattr(result, "value", None)
    excerpt = getattr(result, "original_text", "") or getattr(result, "reasoning", "") or value
    return make_field_result(
        value,
        "ai_extraction",
        source_path,
        excerpt,
        method=method,
        confidence=getattr(result, "confidence", 0.75),
    )


def ai_context_to_payload(context: AIExtractionContext) -> dict[str, Any]:
    return asdict(context)


def ai_context_from_payload(payload: Any) -> AIExtractionContext:
    if isinstance(payload, AIExtractionContext):
        return payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return AIExtractionContext(
        html_key_values=payload.get("html_key_values") or {},
        detail_text=payload.get("detail_text") or "",
        notice_text=payload.get("notice_text") or "",
        image_urls=payload.get("image_urls") or [],
        asset_group=payload.get("asset_group") or "",
        paimai_id=payload.get("paimai_id") or "",
    )


def ai_fields_for_asset_group(asset_group: str) -> list[tuple[str, str, str]]:
    fields = [jd_v2.ai_field_tuple(field) for field in jd_v2.COMMON_FIELDS]
    fields.extend(jd_v2.special_ai_field_tuples(asset_group))
    return fields


def ai_results_to_payload(ai_results: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, result in ai_results.items():
        if hasattr(result, "__dataclass_fields__"):
            payload[key] = asdict(result)
        else:
            payload[key] = result
    return payload


class MultiPlatformRunner:
    def __init__(
        self,
        db: Any,
        handlers: Mapping[str, PlatformHandler],
        *,
        ai_enabled: bool = True,
        ai_mode: str = "sync",
        item_concurrency: int = 1,
    ) -> None:
        self.db = db
        self.handlers = dict(handlers)
        normalized_mode = (ai_mode or "sync").strip().lower()
        if normalized_mode not in {"sync", "async", "off"}:
            raise ValueError("ai_mode must be one of: sync, async, off")
        self.ai_mode = "off" if not ai_enabled else normalized_mode
        self.ai_enabled = self.ai_mode != "off"
        self.item_concurrency = max(1, int(item_concurrency or 1))

    def crawl_platform(self, platform: str, limit: int = 10) -> PlatformCrawlResult:
        if platform not in self.handlers:
            raise KeyError(f"unknown platform: {platform}")
        handler = self.handlers[platform]
        crawl_with_db = getattr(handler, "crawl_with_db", None)
        if callable(crawl_with_db):
            return crawl_with_db(self.db, limit)
        batch_id = self.db.start_batch(
            {
                "source_platform": platform,
                "source_site_name": getattr(handler, "source_site_name", platform),
                "limit": limit,
                "runner": "multi_platform_runner",
            }
        )
        result = PlatformCrawlResult(platform=platform, batch_id=batch_id)
        try:
            list_items = handler.fetch_list(limit)
            result.scanned_count = len(list_items)
            if self.item_concurrency <= 1 or len(list_items) <= 1:
                for list_item in list_items:
                    self._crawl_list_item(handler, batch_id, list_item, result)
            else:
                with ThreadPoolExecutor(max_workers=min(self.item_concurrency, len(list_items))) as executor:
                    futures = {
                        executor.submit(self._crawl_list_item, handler, batch_id, list_item, None): list_item
                        for list_item in list_items
                    }
                    for future in as_completed(futures):
                        list_item = futures[future]
                        try:
                            future.result()
                            result.success_count += 1
                        except Exception as exc:
                            result.failed_count += 1
                            result.errors.append(
                                {
                                    "item": compact_text(getattr(list_item, "source_item_id", "")) or compact_text(list_item),
                                    "error": str(exc),
                                }
                            )
            status = "success" if result.failed_count == 0 else ("partial_success" if result.success_count else "failed")
            self.db.finish_batch(batch_id, status, safe_json(result.__dict__))
            return result
        except Exception as exc:
            result.failed_count = max(result.failed_count, limit)
            result.errors.append({"item": "list", "error": str(exc)})
            self.db.finish_batch(batch_id, "failed", safe_json(result.__dict__))
            return result

    def _crawl_list_item(
        self,
        handler: PlatformHandler,
        batch_id: str,
        list_item: Any,
        result: PlatformCrawlResult | None = None,
    ) -> None:
        try:
            detail_bundle = handler.fetch_detail(list_item)
            record = handler.build_record(detail_bundle)
            if self.ai_enabled and self.ai_mode == "sync":
                self._apply_ai(record, handler, detail_bundle)
            self._write_record(batch_id, record)
            if self.ai_enabled and self.ai_mode == "async":
                self._enqueue_ai(record, handler, detail_bundle)
            if result is not None:
                result.success_count += 1
        except Exception as exc:
            if result is None:
                raise
            result.failed_count += 1
            result.errors.append(
                {
                    "item": compact_text(getattr(list_item, "source_item_id", "")) or compact_text(list_item),
                    "error": str(exc),
                }
            )

    def _apply_ai(self, record: PlatformRecord, handler: PlatformHandler, detail_bundle: Any) -> None:
        context_builder = getattr(getattr(handler, "adapter", None), "build_ai_context", None)
        if context_builder is None:
            return
        context = context_builder(detail_bundle)
        if not isinstance(context, AIExtractionContext):
            return
        context.asset_group = record.asset_group
        if not context.paimai_id:
            context.paimai_id = f"{record.source_platform}:{record.source_item_id}"
        ai_results = self._batch_extract_ai(record.asset_group, context)
        if not ai_results:
            return
        common_values, common_results, special_values, special_results = split_ai_results(ai_results, record.asset_group)
        for key, value in common_values.items():
            if not jd_v2.is_blank(value):
                if key in AI_FILL_ONLY_COMMON_FIELDS and not jd_v2.is_blank(record.common_values.get(key)):
                    continue
                record.common_values[key] = value
                record.field_results[key] = common_results[key]
        for key, value in special_values.items():
            if not jd_v2.is_blank(value):
                record.special_values[key] = value
                record.special_field_results[key] = special_results[key]
        self._apply_ai_detail_rows(record, ai_results)

    def _batch_extract_ai(self, asset_group: str, context: AIExtractionContext) -> Mapping[str, Any]:
        extractor = getattr(jd_v2, "ai_extractor", None)
        if extractor is None or not getattr(extractor, "is_available", lambda: False)():
            return {}
        return extractor.batch_extract(ai_fields_for_asset_group(asset_group), context)

    def _enqueue_ai(self, record: PlatformRecord, handler: PlatformHandler, detail_bundle: Any) -> None:
        context_builder = getattr(getattr(handler, "adapter", None), "build_ai_context", None)
        if context_builder is None or not hasattr(self.db, "enqueue_ai_enrichment_task"):
            return
        context = context_builder(detail_bundle)
        if not isinstance(context, AIExtractionContext):
            return
        context.asset_group = record.asset_group
        if not context.paimai_id:
            context.paimai_id = f"{record.source_platform}:{record.source_item_id}"
        self.db.enqueue_ai_enrichment_task(
            paimai_id=record.source_item_id,
            source_platform=record.source_platform,
            source_item_id=record.source_item_id,
            asset_group=record.asset_group,
            context=ai_context_to_payload(context),
            task_type="field_enrichment",
            priority=100,
            reason="main crawl wrote item first; queued AI enrichment",
        )

    def process_ai_enrichment_queue(self, *, limit: int = 20, worker_id: str = "ai-worker") -> dict[str, Any]:
        if not hasattr(self.db, "fetch_ai_enrichment_tasks"):
            raise AttributeError("db does not support ai enrichment queue")
        tasks = self.db.fetch_ai_enrichment_tasks(limit=limit, worker_id=worker_id)
        summary = {"picked": len(tasks), "success": 0, "failed": 0, "errors": []}
        for task in tasks:
            task_id = int(task["ai_task_id"])
            source_platform = compact_text(task.get("source_platform")) or "jd"
            source_item_id = compact_text(task.get("source_item_id"))
            asset_group = compact_text(task.get("asset_group")) or "other"
            try:
                context = ai_context_from_payload(task.get("context_json"))
                context.asset_group = asset_group
                if not context.paimai_id:
                    context.paimai_id = f"{source_platform}:{source_item_id}"
                ai_results = self._batch_extract_ai(asset_group, context)
                if not ai_results:
                    raise RuntimeError("AI extractor unavailable or returned no enrichment result")
                common_values, common_results, special_values, special_results = split_ai_results(ai_results, asset_group)
                record = PlatformRecord(
                    source_platform=source_platform,
                    source_site_name=source_platform,
                    source_item_id=source_item_id,
                    source_url="",
                    asset_group=asset_group,
                    common_values=common_values,
                    field_results=common_results,
                    special_values=special_values,
                    special_field_results=special_results,
                )
                self._apply_ai_detail_rows(record, ai_results)
                if not record.common_values and not record.special_values and not record.debt_details and not record.ip_details:
                    raise RuntimeError("AI extractor returned only empty/error enrichment values")
                self.db.apply_ai_enrichment_results(
                    paimai_id=source_item_id,
                    source_platform=source_platform,
                    asset_group=asset_group,
                    common_values=record.common_values,
                    common_results=record.field_results,
                    special_values=record.special_values,
                    special_results=record.special_field_results,
                    debt_details=record.debt_details,
                    ip_details=record.ip_details,
                )
                self.db.mark_ai_enrichment_task_success(task_id, ai_results_to_payload(ai_results))
                summary["success"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append({"ai_task_id": task_id, "error": str(exc)})
                if hasattr(self.db, "mark_ai_enrichment_task_failed"):
                    self.db.mark_ai_enrichment_task_failed(task_id, exc)
        return summary

    def _apply_ai_detail_rows(self, record: PlatformRecord, ai_results: Mapping[str, Any]) -> None:
        if record.asset_group == "debt":
            result = ai_results.get("debt_package_details_json")
            details = jd_v2.normalize_ai_debt_details(getattr(result, "value", None) if result else None)
            if not details:
                return
            record.debt_details = details
            derived_values = {
                "household_count": jd_v2.debt_detail_household_count(details),
                "benchmark_date": jd_v2.debt_detail_first_benchmark_date(details),
                "debtor_name": jd_v2.debt_detail_primary_debtor_names(details),
            }
            for key, value in derived_values.items():
                if jd_v2.is_blank(value) or not jd_v2.is_blank(record.special_values.get(key)):
                    continue
                record.special_values[key] = value
                record.special_field_results[key] = make_field_result(
                    value,
                    "ai_extraction",
                    f"llm_batch.{key}_from_debt_details",
                    getattr(result, "original_text", "") if result else value,
                    method="ai_derived",
                    confidence=getattr(result, "confidence", 0.75) if result else 0.75,
                )
        elif record.asset_group == "ip":
            result = ai_results.get("ip_details")
            details = jd_v2.normalize_ai_ip_details(getattr(result, "value", None) if result else None)
            if not details or jd_v2.ip_details_look_aggregated(details):
                details = self._single_ip_detail_from_special_values(record)
                if not details:
                    return
            record.ip_details = details
            if jd_v2.is_blank(record.special_values.get("ip_count")):
                value = str(len(details))
                record.special_values["ip_count"] = value
                record.special_field_results["ip_count"] = make_field_result(
                    value,
                    "ai_extraction",
                    "llm_batch.ip_count_from_ip_details",
                    getattr(result, "original_text", "") if result else value,
                    method="ai_derived",
                    confidence=getattr(result, "confidence", 0.75) if result else 0.75,
                )

    def _single_ip_detail_from_special_values(self, record: PlatformRecord) -> list[dict[str, Any]]:
        values = record.special_values or {}
        ip_name = compact_text(values.get("subject_name")) or ""
        certificate_no = compact_text(values.get("certificate_no")) or ""
        ip_type = compact_text(values.get("ip_type") or values.get("specific_category")) or ""
        if not any((ip_name, certificate_no, ip_type)):
            return []
        if re.search(r"\d+\s*项", ip_name) or re.search(r"\d+\s*项", certificate_no):
            return []
        excerpt_parts: list[str] = []
        for key in ("subject_name", "certificate_no", "ip_type", "specific_category"):
            result = record.special_field_results.get(key) or {}
            excerpt = compact_text(result.get("source_excerpt") or result.get("source_value")) or ""
            if excerpt and excerpt not in excerpt_parts:
                excerpt_parts.append(excerpt)
        return [
            {
                "sequence_no": "1",
                "ip_name": ip_name or None,
                "certificate_no": certificate_no or None,
                "ip_type": ip_type or None,
                "right_holder": compact_text(values.get("right_holder")) or None,
                "right_status": compact_text(values.get("right_status")) or None,
                "source_excerpt": "；".join(excerpt_parts[:4]) or None,
            }
        ]

    def _write_record(self, batch_id: str, record: PlatformRecord) -> None:
        source_item_id = compact_text(record.source_item_id)
        if not source_item_id:
            raise ValueError("source_item_id is required")
        common_values = dict(record.common_values)
        common_values.update(
            {
                "source_platform": record.source_platform,
                "source_item_id": source_item_id,
                "source_url": record.source_url,
                "source_site_name": record.source_site_name,
            }
        )
        if "data_source" not in common_values or not compact_text(common_values.get("data_source")):
            common_values["data_source"] = record.source_site_name
        if "project_name" not in common_values or not compact_text(common_values.get("project_name")):
            common_values["project_name"] = source_item_id
        if "attachments_json" not in common_values and record.attachments_json is not None:
            common_values["attachments_json"] = safe_json(record.attachments_json)

        raw = record.raw_payloads or {}
        self.db.upsert_raw_payloads(
            paimai_id=source_item_id,
            batch_id=batch_id,
            source_url=record.source_url,
            source_platform=record.source_platform,
            source_item_id=source_item_id,
            source_site_name=record.source_site_name,
            list_json=raw.get("list_json") or raw.get("list_html") or {},
            detail_json=raw.get("detail_json") or {},
            product_basic_json=raw.get("product_basic_json") or raw.get("auxiliary_json") or {},
            realtime_json=raw.get("realtime_json") or raw.get("status_json") or {},
            description_html=raw.get("description_html") or raw.get("detail_html") or "",
            notice_html=raw.get("notice_html") or "",
            announcement_html=raw.get("announcement_html") or "",
            attachments_json=record.attachments_json,
            vendor_json=raw.get("vendor_json") or {},
        )
        self.db.upsert_common_item(
            paimai_id=source_item_id,
            batch_id=batch_id,
            asset_group=record.asset_group,
            jd_category_id=record.category_id,
            jd_category_name=record.category_name,
            values=common_values,
            field_results=record.field_results,
            special_values=record.special_values,
        )
        if record.asset_group in jd_v2.SPECIAL_FIELDS:
            self.db.upsert_special_item(
                paimai_id=source_item_id,
                source_platform=record.source_platform,
                asset_group=record.asset_group,
                values=record.special_values,
                field_results=record.special_field_results,
            )
        if record.debt_details:
            self.db.upsert_debt_details(
                paimai_id=source_item_id,
                source_platform=record.source_platform,
                details=record.debt_details,
            )
        if record.ip_details:
            self.db.upsert_ip_details(
                paimai_id=source_item_id,
                source_platform=record.source_platform,
                details=record.ip_details,
            )


class RequestsHTMLClient:
    def __init__(self, *, timeout: int | float | None = 0, headers: Mapping[str, str] | None = None) -> None:
        self.session = requests.Session()
        self.session.headers.update(dict(DEFAULT_HEADERS))
        if headers:
            self.session.headers.update(dict(headers))
        self.timeout = request_timeout_value(timeout)

    def get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def post_text(self, url: str, data: Mapping[str, Any] | None = None) -> str:
        response = self.session.post(url, data=data, timeout=self.timeout)
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text


class Ejy365LiveHandler:
    source_platform = "ejy365"
    source_site_name = "e交易"

    def __init__(self, *, request_timeout: int | float | None = 0, project_types: Iterable[str] | None = None) -> None:
        self.adapter = Ejy365Adapter()
        self.client = RequestsHTMLClient(timeout=request_timeout)
        self.project_types = tuple(project_types or DEFAULT_EJY365_PROJECT_TYPES)

    def fetch_list(self, limit: int) -> list[Ejy365ListItem]:
        items: list[Ejy365ListItem] = []
        seen: set[str] = set()
        per_type_count: dict[str, int] = {}
        per_type_limit = max(1, (max(1, limit) + len(self.project_types) - 1) // max(1, len(self.project_types)))
        for page in range(1, 20):
            page_added = 0
            for project_type in self.project_types:
                if per_type_count.get(project_type, 0) >= per_type_limit:
                    continue
                url = f"{EJY365_BASE_URL}/jygg_more?project_type={project_type}&page={page}"
                html = self.client.get_text(url)
                added_for_type_this_page = 0
                for item in self.adapter.parse_list_html(html, base_url=EJY365_BASE_URL):
                    key = item.slug or item.detail_url
                    if key in seen:
                        continue
                    seen.add(key)
                    setattr(item, "project_type_code", project_type)
                    items.append(item)
                    per_type_count[project_type] = per_type_count.get(project_type, 0) + 1
                    page_added += 1
                    added_for_type_this_page += 1
                    if len(items) >= limit:
                        return items
                    if per_type_count[project_type] >= per_type_limit or added_for_type_this_page >= 1:
                        break
            if page == 1 and page_added == 0:
                break
        return items

    def fetch_detail(self, list_item: Ejy365ListItem) -> Ejy365DetailBundle:
        html = self.client.get_text(list_item.detail_url)
        return self.adapter.parse_detail_html(html, url=list_item.detail_url, list_item=list_item)

    def build_record(self, detail_bundle: Ejy365DetailBundle) -> PlatformRecord:
        common = self.adapter.map_common_candidates(detail_bundle)
        field_results = common.pop("field_results", {})
        source_item_id = compact_text(common.get("source_item_id") or detail_bundle.source_item_id or detail_bundle.url)
        attachments = normalize_attachments_payload(
            detail_bundle.attachments,
            [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}],
        )
        project_type = compact_text(getattr(detail_bundle.list_item, "project_type_code", "")) if detail_bundle.list_item else "ZQ"
        asset_group, asset_label = ejy365_asset_for_project_type(project_type or "ZQ")
        common["source_site_name"] = self.source_site_name
        common["asset_group"] = asset_group
        common["asset_type"] = asset_label
        field_results["asset_group"] = make_field_result(
            asset_group,
            "list_html",
            "project_type",
            f"project_type={project_type or 'ZQ'}",
            method="category_mapping",
            confidence=0.9,
        )
        field_results["asset_type"] = make_field_result(
            asset_label,
            "list_html",
            "project_type",
            f"project_type={project_type or 'ZQ'}",
            method="category_mapping",
            confidence=0.9,
        )
        common["attachments_json"] = safe_json(attachments)
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.url,
            asset_group=asset_group,
            category_id=project_type or "ZQ",
            category_name=asset_label,
            common_values=common,
            field_results=field_results or common_results_from_values(common, "detail_html", "ejy365"),
            special_values={},
            special_field_results={},
            raw_payloads={
                "list_json": {"raw_html": detail_bundle.list_item.raw_html if detail_bundle.list_item else ""},
                "detail_html": detail_bundle.html,
                "auxiliary_json": detail_bundle.auxiliary_json or {},
                "status_json": detail_bundle.status_json or {},
            },
            attachments_json=attachments,
        )


class CquaeLiveHandler:
    source_platform = CQUAE_PLATFORM
    source_site_name = "重庆产权交易网"

    def __init__(
        self,
        *,
        request_timeout: int | float | None = 0,
        use_browser: bool = True,
        browser_headless: bool = True,
        browser_profile_path: str | None = None,
    ) -> None:
        self.adapter = CquaeAdapter()
        self.client = RequestsHTMLClient(timeout=request_timeout)
        self.use_browser = use_browser
        self.browser = (
            CquaeBrowserFetcher(
                headless=browser_headless,
                timeout_ms=0,
                profile_path=browser_profile_path,
            )
            if use_browser
            else None
        )

    def _fetch_html(self, url: str) -> str:
        try:
            html = self.client.get_text(url)
            if not self.adapter.is_waf_challenge(html):
                return html
        except Exception:
            if not self.use_browser:
                raise
        if not self.browser:
            raise RuntimeError("CQUAE returned WAF challenge and browser fallback is disabled")
        rendered_html = self.browser.fetch_html(url)
        if self.adapter.is_waf_challenge(rendered_html):
            raise RuntimeError(
                "CQUAE returned a WAF challenge after browser fallback; "
                "try --cquae-headed with a trusted desktop browser session or switch to an official/API data source"
            )
        return rendered_html

    def fetch_list(self, limit: int) -> list[CquaeListItem]:
        items: list[CquaeListItem] = []
        seen: set[str] = set()
        first_error: Exception | None = None
        try:
            for page in range(1, 20):
                url = self.adapter.build_list_url(page=page, project_id=1, nt=1, price_id=32)
                html = self._fetch_html(url)
                page_items = self.adapter.parse_list_html(html, base_url=CQUAE_BASE_URL)
                if not page_items and page == 1:
                    break
                for item in page_items:
                    if item.source_item_id in seen:
                        continue
                    seen.add(item.source_item_id)
                    items.append(item)
                    if len(items) >= limit:
                        return items
        except Exception as exc:
            first_error = exc
        if items:
            return items
        fallback_items = self._fetch_sdcqjy_list(limit)
        if fallback_items:
            return fallback_items
        if first_error:
            raise first_error
        return items

    def _fetch_sdcqjy_list(self, limit: int) -> list[CquaeListItem]:
        html = self.client.post_text(SDCQJY_LIST_ENDPOINT, data={"assetsType": ""})
        items = self.adapter.parse_sdcqjy_list_html(html)
        return items[:limit]

    def fetch_detail(self, list_item: CquaeListItem) -> CquaeDetailBundle:
        html = self._fetch_html(list_item.source_url)
        return self.adapter.parse_detail_html(html, url=list_item.source_url, list_item=list_item)

    def build_record(self, detail_bundle: CquaeDetailBundle) -> PlatformRecord:
        common = self.adapter.map_common_candidates(detail_bundle)
        adapter_field_results = common.pop("field_results", {})
        asset_group = common.get("asset_group") or self.adapter.classify_bundle(detail_bundle) or "other"
        special = self.adapter.map_special_candidates(detail_bundle, asset_group)
        is_sdcqjy = "sdcqjy.com" in (detail_bundle.source_url or "")
        common["source_site_name"] = SDCQJY_DATA_SOURCE if is_sdcqjy else self.source_site_name
        if is_sdcqjy:
            common["data_source"] = SDCQJY_DATA_SOURCE
        attachments = normalize_attachments_payload(
            detail_bundle.attachments,
            [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}],
        )
        common["attachments_json"] = safe_json(attachments)
        field_results = common_results_from_values(common, "detail_html", "cquae")
        for key, fr in adapter_field_results.items():
            field_results[key] = fr
        special_results = special_results_from_values(asset_group, special, "detail_html", "cquae")
        source_item_id = compact_text(detail_bundle.source_item_id)
        record_source_site_name = compact_text(common.get("source_site_name")) or self.source_site_name
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=record_source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.source_url,
            asset_group=asset_group,
            category_id="cquae",
            category_name=compact_text(common.get("asset_type")) or "产权交易",
            common_values=common,
            field_results=field_results,
            special_values=special,
            special_field_results=special_results,
            raw_payloads={
                "list_json": detail_bundle.list_item.raw_fields if detail_bundle.list_item else {},
                "detail_html": detail_bundle.raw_html,
            },
            attachments_json=attachments,
        )


class AliLiveHandler:
    source_platform = ALI_SOURCE_PLATFORM
    source_site_name = "阿里拍卖"

    def __init__(
        self,
        *,
        profile_path: str | None = None,
        item_urls: Iterable[str] | None = None,
        headless: bool = False,
        timeout_ms: int = 0,
    ) -> None:
        self.adapter = AliAuctionAdapter()
        self.profile_path = profile_path
        self.item_urls = list(item_urls or [])
        self.headless = headless
        self.timeout_ms = timeout_ms

    def fetch_list(self, limit: int) -> list[AliListItem]:
        if self.item_urls:
            return [self._list_item_from_url(url) for url in self.item_urls[:limit]]
        try:
            items = self.adapter.fetch_mtop_list(limit=limit)
            if items:
                return items
        except Exception:
            pass
        return self._fetch_list_with_browser(limit)

    def _list_item_from_url(self, url: str) -> AliListItem:
        item_id = self._extract_item_id(url)
        return AliListItem(item_id=item_id, source_url=url, title=item_id, raw={"url": url})

    def _extract_item_id(self, url: str) -> str:
        match = re.search(r"(?:itemId=|id=)(\d+)", url)
        if match:
            return match.group(1)
        digits = re.findall(r"\d{6,}", url)
        return digits[-1] if digits else url

    def _fetch_list_with_browser(self, limit: int) -> list[AliListItem]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._fetch_list_with_selenium(limit)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_path,
                headless=self.headless,
            )
            try:
                page = context.new_page()
                page.goto("https://zc-paimai.taobao.com/", wait_until="domcontentloaded", timeout=self.timeout_ms)
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
            finally:
                context.close()
        return self._list_items_from_page(hrefs, "", limit)

    def _fetch_list_with_selenium(self, limit: int) -> list[AliListItem]:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import TimeoutException, WebDriverException
        except ImportError as exc:
            raise RuntimeError("Ali live listing requires Playwright or Selenium.") from exc

        last_error: Exception | None = None
        for browser_name, driver_factory, options_factory in (
            ("chrome", webdriver.Chrome, webdriver.ChromeOptions),
            ("edge", webdriver.Edge, webdriver.EdgeOptions),
        ):
            options = options_factory()
            options.page_load_strategy = "eager"
            if self.headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1365,900")
            options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
            if self.profile_path and browser_name == "chrome":
                options.add_argument(f"--user-data-dir={self.profile_path}")
            driver = None
            try:
                driver = driver_factory(options=options)
                if self.timeout_ms and self.timeout_ms > 0:
                    driver.set_page_load_timeout(max(1, self.timeout_ms / 1000))
                try:
                    driver.get("https://zc-paimai.taobao.com/")
                except TimeoutException:
                    driver.execute_script("window.stop()")
                time.sleep(5)
                hrefs = driver.execute_script(
                    "return Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
                )
                return self._list_items_from_page(list(hrefs or []), driver.page_source or "", limit)
            except WebDriverException as exc:
                last_error = exc
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        raise RuntimeError(f"Ali live listing failed with browser fallback: {last_error}")

    def _list_items_from_page(self, hrefs: Iterable[str], html: str, limit: int) -> list[AliListItem]:
        candidates: list[str] = []
        candidates.extend(str(href) for href in hrefs if href)
        candidates.extend(re.findall(r"https?://[^\"'\\s<>]+auction\\.htm[^\"'\\s<>]+", html or "", flags=re.I))
        for match in re.findall(r"(?:itemId|item_id)[\"'=:\\s]+(\\d{5,})", html or "", flags=re.I):
            candidates.append(ALI_DEFAULT_DETAIL_URL.format(item_id=match))

        items: list[AliListItem] = []
        seen: set[str] = set()
        for href in candidates:
            if "itemId=" not in href and "auction.htm" not in href:
                continue
            item_id = self._extract_item_id(href)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            source_url = href if href.startswith("http") else ALI_DEFAULT_DETAIL_URL.format(item_id=item_id)
            items.append(AliListItem(item_id=item_id, source_url=source_url, title=item_id, raw={"url": source_url}))
            if len(items) >= limit:
                break
        if not items:
            raise RuntimeError(
                "Ali listing rendered but no auction item links were found; "
                "the page likely requires login/session cookies or an mtop list API."
            )
        return items

    def fetch_detail(self, list_item: AliListItem) -> AliDetailBundle:
        url = list_item.source_url or ALI_DEFAULT_DETAIL_URL.format(item_id=list_item.item_id)
        try:
            bundle = self.adapter.fetch_mtop_detail(list_item)
        except Exception:
            bundle = self.adapter.browser_fetcher.fetch_detail(
                url,
                profile_path=self.profile_path,
                timeout_ms=self.timeout_ms,
            )
        bundle.list_item = list_item
        if not bundle.source_item_id:
            bundle.source_item_id = list_item.item_id
        if not bundle.source_url:
            bundle.source_url = url
        return bundle

    def build_record(self, detail_bundle: AliDetailBundle) -> PlatformRecord:
        if detail_bundle.status != "ok":
            raise RuntimeError(f"Ali detail blocked: {detail_bundle.status} {detail_bundle.block_reason}")
        common = self.adapter.map_common_candidates(detail_bundle)
        adapter_field_results = common.pop("field_results", {})
        asset_group = detail_bundle.asset_group or common.get("asset_group") or "other"
        special = self.adapter.map_special_candidates(detail_bundle, asset_group)
        common["source_site_name"] = self.source_site_name
        attachments = normalize_attachments_payload(detail_bundle.attachments, [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}])
        common["attachments_json"] = safe_json(attachments)
        field_results = common_results_from_values(common, "detail_html", "ali")
        for key, fr in adapter_field_results.items():
            field_results[key] = fr
        special_results = special_results_from_values(asset_group, special, "detail_html", "ali")
        source_item_id = compact_text(detail_bundle.source_item_id)
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.source_url,
            asset_group=asset_group,
            category_id="ali",
            category_name=detail_bundle.category or compact_text(common.get("asset_type")),
            common_values=common,
            field_results=field_results,
            special_values=special,
            special_field_results=special_results,
            raw_payloads={
                "list_json": detail_bundle.list_item.raw if detail_bundle.list_item else {},
                "detail_html": detail_bundle.rendered_html,
                "notice_html": detail_bundle.notice_html,
                "announcement_html": detail_bundle.notice_html,
                "detail_json": detail_bundle.top_json or {},
            },
            attachments_json=attachments,
        )


class TpreLiveHandler:
    source_platform = TPRE_PLATFORM
    source_site_name = TPRE_DATA_SOURCE

    def __init__(self, *, request_timeout: int | float | None = 0) -> None:
        self.adapter = TpreAdapter(timeout=request_timeout if request_timeout else 15)

    def fetch_list(self, limit: int) -> list[TpreListItem]:
        items: list[TpreListItem] = []
        seen: set[str] = set()
        for page in range(1, 20):
            api_data = self.adapter.fetch_list_api(page=page, size=min(limit, 20))
            page_items = self.adapter.parse_list_response(api_data)
            if not page_items:
                break
            for item in page_items:
                if item.source_item_id in seen:
                    continue
                seen.add(item.source_item_id)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def fetch_detail(self, list_item: TpreListItem) -> TpreDetailBundle:
        api_data = self.adapter.fetch_detail_api(list_item)
        return self.adapter.parse_detail_response(api_data, list_item=list_item)

    def build_record(self, detail_bundle: TpreDetailBundle) -> PlatformRecord:
        common = self.adapter.map_common_candidates(detail_bundle)
        field_results = common.pop("field_results", {})
        asset_group_common = common.get("asset_group") or self.adapter.classify_bundle(detail_bundle) or "other"
        special = self.adapter.map_special_candidates(detail_bundle, asset_group_common)
        common["source_site_name"] = self.source_site_name
        attachments = normalize_attachments_payload(
            detail_bundle.attachments,
            [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}],
        )
        common["attachments_json"] = safe_json(attachments)
        common_results = common_results_from_values(common, "detail_api", TPRE_PLATFORM)
        special_results = special_results_from_values(asset_group_common, special, "detail_api", TPRE_PLATFORM)
        for key, fr in field_results.items():
            common_results.setdefault(key, fr)
        asset_type = compact_text(common.get("asset_type")) or "产权转让"
        source_item_id = compact_text(detail_bundle.source_item_id)
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.source_url,
            asset_group=asset_group_common,
            category_id=TPRE_PLATFORM,
            category_name=asset_type,
            common_values=common,
            field_results=common_results,
            special_values=special,
            special_field_results=special_results,
            raw_payloads={
                "list_json": asdict(detail_bundle.list_item) if detail_bundle.list_item else {},
                "detail_json": detail_bundle.detail_json,
                "detail_html": detail_bundle.raw_html,
            },
            attachments_json=attachments,
        )


class PrechinaLiveHandler:
    source_platform = PRECHINA_PLATFORM
    source_site_name = PRECHINA_DATA_SOURCE

    def __init__(self, *, request_timeout: int | float | None = 0) -> None:
        self.adapter = PrechinaAdapter(timeout=request_timeout if request_timeout else 20)
        self.client = RequestsHTMLClient(timeout=request_timeout)

    def fetch_list(self, limit: int) -> list[PrechinaListItem]:
        html = self.client.get_text(PRECHINA_BASE_URL)
        items = self.adapter.parse_list_from_homepage(html)
        return items[:limit]

    def fetch_detail(self, list_item: PrechinaListItem) -> PrechinaDetailBundle:
        if list_item.source_url and list_item.source_url != f"{PRECHINA_BASE_URL}/ejygg/index.jhtml":
            html = self.client.get_text(list_item.source_url)
        else:
            html = ""
        return self.adapter.parse_detail_html(html, url=list_item.source_url, list_item=list_item)

    def build_record(self, detail_bundle: PrechinaDetailBundle) -> PlatformRecord:
        common = self.adapter.map_common_candidates(detail_bundle)
        field_results = common.pop("field_results", {})
        asset_group_common = common.get("asset_group") or self.adapter.classify_bundle(detail_bundle) or "other"
        special = self.adapter.map_special_candidates(detail_bundle, asset_group_common)
        common["source_site_name"] = self.source_site_name
        attachments = normalize_attachments_payload(
            detail_bundle.attachments,
            [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}],
        )
        common["attachments_json"] = safe_json(attachments)
        common_results = common_results_from_values(common, "detail_html", PRECHINA_PLATFORM)
        special_results = special_results_from_values(asset_group_common, special, "detail_html", PRECHINA_PLATFORM)
        for key, fr in field_results.items():
            common_results.setdefault(key, fr)
        asset_type = compact_text(common.get("asset_type")) or "产权转让"
        source_item_id = compact_text(detail_bundle.source_item_id)
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.source_url,
            asset_group=asset_group_common,
            category_id=PRECHINA_PLATFORM,
            category_name=asset_type,
            common_values=common,
            field_results=common_results,
            special_values=special,
            special_field_results=special_results,
            raw_payloads={
                "list_json": detail_bundle.list_item.raw_fields if detail_bundle.list_item else {},
                "detail_html": detail_bundle.raw_html,
            },
            attachments_json=attachments,
        )


class GxcqLiveHandler:
    source_platform = GXCQ_PLATFORM
    source_site_name = GXCQ_DATA_SOURCE

    def __init__(self, *, request_timeout: int | float | None = 0) -> None:
        self.adapter = GxcqAdapter(timeout=request_timeout if request_timeout else 15)
        self.client = RequestsHTMLClient(timeout=request_timeout)

    def fetch_list(self, limit: int) -> list[GxcqListItem]:
        api_data = self.adapter.fetch_list_api(page=1, size=limit)
        return self.adapter.parse_list_response(api_data)[:limit]

    def fetch_detail(self, list_item: GxcqListItem) -> GxcqDetailBundle:
        detail_data = self.adapter.fetch_detail_api(list_item)
        return self.adapter.parse_detail_response(detail_data, list_item=list_item)

    def build_record(self, detail_bundle: GxcqDetailBundle) -> PlatformRecord:
        common = self.adapter.map_common_candidates(detail_bundle)
        field_results = common.pop("field_results", {})
        asset_group_common = common.get("asset_group") or self.adapter.classify_bundle(detail_bundle) or "other"
        special = self.adapter.map_special_candidates(detail_bundle, asset_group_common)
        common["source_site_name"] = self.source_site_name
        attachments = normalize_attachments_payload(
            detail_bundle.attachments,
            [{"imageVideoArea": {"imageList": [{"imagePath": url} for url in detail_bundle.image_urls]}}],
        )
        common["attachments_json"] = safe_json(attachments)
        common_results = common_results_from_values(common, "detail_api", GXCQ_PLATFORM)
        special_results = special_results_from_values(asset_group_common, special, "detail_api", GXCQ_PLATFORM)
        for key, fr in field_results.items():
            common_results.setdefault(key, fr)
        source_item_id = compact_text(detail_bundle.source_item_id)
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=detail_bundle.source_url,
            asset_group=asset_group_common,
            category_id=GXCQ_PLATFORM,
            category_name=compact_text(common.get("asset_type")) or "产权转让",
            common_values=common,
            field_results=common_results,
            special_values=special,
            special_field_results=special_results,
            raw_payloads={
                "list_json": detail_bundle.list_item.raw_json if detail_bundle.list_item else {},
                "detail_html": detail_bundle.raw_html,
            },
            attachments_json=attachments,
        )


class JdLiveHandler:
    source_platform = JD_SOURCE_PLATFORM
    source_site_name = JD_DATA_SOURCE

    def __init__(
        self,
        *,
        request_timeout: float | None = None,
        output_dir: str | Path = Path("outputs") / "multi_platform_jd",
        categories: set[str] | None = None,
    ) -> None:
        timeout = None if request_timeout is None or request_timeout <= 0 else int(request_timeout)
        self.adapter = JDPlatformAdapter(timeout=timeout)
        self.output_dir = Path(output_dir)
        self.categories = categories

    def crawl_with_db(self, db: Any, limit: int) -> PlatformCrawlResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        summary = self.adapter.crawl_sample(
            db=db,
            per_category_limit=limit,
            output_dir=self.output_dir,
            categories=self.categories,
            total_limit=limit,
        )
        errors = summary.get("errors") or []
        scanned = int(summary.get("items_seen") or 0)
        return PlatformCrawlResult(
            platform=self.source_platform,
            batch_id=compact_text(summary.get("batch_id")) or "",
            scanned_count=scanned,
            success_count=max(0, scanned - len(errors)),
            failed_count=len(errors),
            errors=[{"item": compact_text(err.get("paimai_id")), "error": compact_text(err.get("error"))} for err in errors],
        )


def build_handlers(args: argparse.Namespace) -> dict[str, PlatformHandler]:
    ali_urls: list[str] = []
    for url in args.ali_item_url or []:
        ali_urls.extend([part.strip() for part in str(url).split(",") if part.strip()])
    jd_categories = {part.strip() for part in (args.jd_categories or "").split(",") if part.strip()} or None
    return {
        "jd": JdLiveHandler(
            request_timeout=args.request_timeout,
            output_dir=args.output_dir,
            categories=jd_categories,
        ),
        "ejy365": Ejy365LiveHandler(request_timeout=args.request_timeout),
        "cquae": CquaeLiveHandler(
            request_timeout=args.request_timeout,
            use_browser=not args.no_browser,
            browser_headless=not args.cquae_headed,
            browser_profile_path=args.cquae_profile_path or None,
        ),
        "ali": AliLiveHandler(
            profile_path=args.ali_profile_path,
            item_urls=ali_urls,
            headless=args.ali_headless,
            timeout_ms=args.browser_timeout_ms,
        ),
        "tpre": TpreLiveHandler(request_timeout=args.request_timeout),
        "prechina": PrechinaLiveHandler(request_timeout=args.request_timeout),
        "gxcq": GxcqLiveHandler(request_timeout=args.request_timeout),
    }


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_无数据_\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(compact_text(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(lines) + "\n"


def generate_model_quality_report(
    config: MySQLConfig,
    *,
    output_dir: Path,
    run_results: list[dict[str, Any]] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"model_data_quality_report_{time.strftime('%Y%m%d_%H%M%S')}.md"
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM auction_items")
            total_items = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute(
                """
                SELECT
                  source_platform,
                  COALESCE(source_site_name, source_platform) AS source_site_name,
                  COUNT(*) AS item_count,
                  SUM(CASE WHEN final_price_display IS NULL OR final_price_display='' THEN 1 ELSE 0 END) AS missing_final_price,
                  SUM(CASE WHEN start_price_display IS NULL OR start_price_display='' THEN 1 ELSE 0 END) AS missing_start_price,
                  SUM(CASE WHEN contact_info IS NULL OR contact_info='' THEN 1 ELSE 0 END) AS missing_contact,
                  SUM(CASE WHEN assessment_price_display IS NULL OR assessment_price_display='' THEN 1 ELSE 0 END) AS missing_assessment
                FROM auction_items
                GROUP BY source_platform, source_site_name
                ORDER BY source_platform
                """
            )
            platform_rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT resource_type, COUNT(*) AS count_rows
                FROM item_resources
                GROUP BY resource_type
                ORDER BY resource_type
                """
            )
            resource_rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT COUNT(*) AS c
                FROM item_resources
                WHERE resource_url IS NULL OR resource_url=''
                """
            )
            empty_resource_urls = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute(
                """
                SELECT ai.source_platform, COUNT(*) AS item_count
                FROM auction_items ai
                LEFT JOIN item_resources ir
                  ON ir.item_id=ai.item_id AND ir.resource_type='attachment'
                WHERE ir.resource_id IS NULL
                GROUP BY ai.source_platform
                ORDER BY ai.source_platform
                """
            )
            no_attachment_rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT field_namespace, field_key, field_label, COUNT(*) AS count_rows
                FROM field_extractions
                WHERE status <> 'extracted'
                GROUP BY field_namespace, field_key, field_label
                ORDER BY count_rows DESC, field_namespace, field_key
                LIMIT 20
                """
            )
            missing_field_rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT queue_status AS status, COUNT(*) AS count_rows
                FROM ai_enrichment_queue
                GROUP BY queue_status
                ORDER BY queue_status
                """
            )
            ai_queue_rows = cur.fetchall() or []

    result_rows = []
    for item in run_results or []:
        result_rows.append(
            [
                item.get("platform"),
                item.get("scanned_count"),
                item.get("success_count"),
                item.get("failed_count"),
                len(item.get("errors") or []),
            ]
        )

    platform_table = _md_table(
        ["平台", "站点", "标的数", "缺最终价", "缺起拍价", "缺联系方式", "缺评估价"],
        [
            [
                row.get("source_platform"),
                row.get("source_site_name"),
                row.get("item_count"),
                row.get("missing_final_price"),
                row.get("missing_start_price"),
                row.get("missing_contact"),
                row.get("missing_assessment"),
            ]
            for row in platform_rows
        ],
    )
    resource_table = _md_table(
        ["资源类型", "数量"],
        [[row.get("resource_type"), row.get("count_rows")] for row in resource_rows],
    )
    no_attachment_table = _md_table(
        ["平台", "无附件文件的标的数"],
        [[row.get("source_platform"), row.get("item_count")] for row in no_attachment_rows],
    )
    missing_field_table = _md_table(
        ["命名空间", "字段", "中文名", "缺失/异常次数"],
        [
            [row.get("field_namespace"), row.get("field_key"), row.get("field_label"), row.get("count_rows")]
            for row in missing_field_rows
        ],
    )
    ai_queue_table = _md_table(
        ["状态", "数量"],
        [[row.get("status"), row.get("count_rows")] for row in ai_queue_rows],
    )
    result_table = _md_table(
        ["平台", "扫描", "成功", "失败", "错误数"],
        result_rows,
    )

    report = f"""# 模型采集数据质量报告

生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}

## 本次采集结果

{result_table}

## 总览

- 当前库内标的数：{total_items}
- item_resources 空 URL 数：{empty_resource_urls}

## 平台字段缺失

{platform_table}

## 资源入库情况

{resource_table}

## 无附件文件的标的

说明：这里统计的是 `item_resources.resource_type='attachment'` 为空的标的；只有图片/视频不算附件文件。

{no_attachment_table}

## 字段缺失/异常 Top20

{missing_field_table}

## AI 异步队列

{ai_queue_table}
"""
    platform_table = _md_table(
        ["平台", "站点", "标的数", "缺最终价", "缺起拍价", "缺联系方式", "缺评估价"],
        [
            [
                row.get("source_platform"),
                row.get("source_site_name"),
                row.get("item_count"),
                row.get("missing_final_price"),
                row.get("missing_start_price"),
                row.get("missing_contact"),
                row.get("missing_assessment"),
            ]
            for row in platform_rows
        ],
    )
    resource_table = _md_table(
        ["资源类型", "数量"],
        [[row.get("resource_type"), row.get("count_rows")] for row in resource_rows],
    )
    no_attachment_table = _md_table(
        ["平台", "无附件文件的标的数"],
        [[row.get("source_platform"), row.get("item_count")] for row in no_attachment_rows],
    )
    missing_field_table = _md_table(
        ["命名空间", "字段", "中文名", "缺失/异常次数"],
        [
            [row.get("field_namespace"), row.get("field_key"), row.get("field_label"), row.get("count_rows")]
            for row in missing_field_rows
        ],
    )
    ai_queue_table = _md_table(
        ["状态", "数量"],
        [[row.get("status"), row.get("count_rows")] for row in ai_queue_rows],
    )
    result_table = _md_table(
        ["平台", "扫描", "成功", "失败", "错误数"],
        result_rows,
    )
    report = f"""# 模型采集数据质量报告

生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}

## 本次采集结果

{result_table}

## 总览

- 当前库内标的数：{total_items}
- item_resources 空 URL 数：{empty_resource_urls}

## 平台字段缺失

{platform_table}

## 资源入库情况

{resource_table}

## 无附件文件的标的

说明：这里统计的是 `item_resources.resource_type='attachment'` 为空的标的；只有图片/视频不算附件文件。
{no_attachment_table}

## 字段缺失/异常 Top20

{missing_field_table}

## AI 异步队列

{ai_queue_table}
"""
    report_path.write_text(report, encoding="utf-8")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multi-platform auction crawler runner")
    sub = parser.add_subparsers(dest="command", required=True)
    crawl = sub.add_parser("crawl", help="crawl one or more non-JD platforms into MySQL")
    crawl.add_argument("--platform", choices=["jd", "ejy365", "cquae", "ali", "tpre", "prechina", "gxcq", "all"], default="all")
    crawl.add_argument("--limit", type=int, default=10)
    crawl.add_argument("--output-dir", type=Path, default=Path("outputs") / "multi_platform", help="export/output directory")
    crawl.add_argument("--reset-db", action="store_true", help="drop and recreate MySQL V2 tables before crawling")
    crawl.add_argument("--confirm-reset-db", action="store_true", help="required with --reset-db to confirm destructive table drops")
    crawl.add_argument("--mysql-host", default="127.0.0.1")
    crawl.add_argument("--mysql-port", type=int, default=3306)
    crawl.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", ""))
    crawl.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", ""))
    crawl.add_argument("--mysql-database", default=os.getenv("MYSQL_DATABASE", "auction_data"))
    crawl.add_argument("--platform-concurrency", type=int, default=1, help="number of platforms to crawl concurrently")
    crawl.add_argument("--item-concurrency", type=int, default=1, help="number of items to process concurrently per platform")
    crawl.add_argument("--request-timeout", type=float, default=0, help="HTTP timeout seconds; 0 means no local timeout")
    crawl.add_argument("--browser-timeout-ms", type=int, default=0, help="browser navigation timeout; 0 means no timeout")
    crawl.add_argument("--no-browser", action="store_true", help="disable browser fallback for WAF pages")
    crawl.add_argument("--cquae-headed", action="store_true", help="run CQUAE browser fallback in visible headed mode")
    crawl.add_argument("--cquae-profile-path", default="", help="browser user-data-dir for CQUAE WAF fallback")
    crawl.add_argument("--no-ai", action="store_true", help="disable AI enrichment")
    crawl.add_argument(
        "--ai-mode",
        choices=["sync", "async", "off"],
        default="async",
        help="AI extraction mode: sync blocks crawl; async queues enrichment after DB write; off disables AI",
    )
    crawl.add_argument("--ai-model", default="")
    crawl.add_argument("--ai-profile", default="")
    crawl.add_argument("--ai-provider", default="")
    crawl.add_argument("--ai-model-name", default="")
    crawl.add_argument("--ai-api-key", default="")
    crawl.add_argument("--ai-base-url", default="")
    crawl.add_argument("--ali-profile-path", default="")
    crawl.add_argument("--ali-item-url", action="append", help="Ali detail URL; repeatable or comma-separated")
    crawl.add_argument("--ali-headless", action="store_true")
    crawl.add_argument("--jd-categories", default="", help="JD category ids, comma-separated; only used when --platform jd")

    enrich = sub.add_parser("ai-enrich", help="process queued AI enrichment tasks")
    enrich.add_argument("--limit", type=int, default=20)
    enrich.add_argument("--worker-id", default="ai-worker")
    enrich.add_argument("--output-dir", type=Path, default=Path("outputs") / "multi_platform", help="export/output directory")
    enrich.add_argument("--mysql-host", default="127.0.0.1")
    enrich.add_argument("--mysql-port", type=int, default=3306)
    enrich.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", ""))
    enrich.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", ""))
    enrich.add_argument("--mysql-database", default=os.getenv("MYSQL_DATABASE", "auction_data"))
    enrich.add_argument("--ai-model", default="")
    enrich.add_argument("--ai-profile", default="")
    enrich.add_argument("--ai-provider", default="")
    enrich.add_argument("--ai-model-name", default="")
    enrich.add_argument("--ai-api-key", default="")
    enrich.add_argument("--ai-base-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = MySQLConfig(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
    )
    db = MySQLJDScraperDatabase(config)
    if args.command == "ai-enrich":
        db.init_schema()
        jd_v2.init_ai_extractor(
            args.ai_provider or args.ai_model,
            args.ai_api_key,
            args.ai_base_url,
            model_name=args.ai_model_name,
            profile=args.ai_profile,
            mysql_config=config,
        )
        runner = MultiPlatformRunner(db=db, handlers={}, ai_enabled=True, ai_mode="sync")
        result = runner.process_ai_enrichment_queue(limit=args.limit, worker_id=args.worker_id)
        report_path = generate_model_quality_report(config, output_dir=args.output_dir, run_results=[])
        result["quality_report"] = str(report_path)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("failed", 0) == 0 else 1

    if args.reset_db and not args.confirm_reset_db:
        print("--reset-db is destructive; add --confirm-reset-db to confirm table reset.")
        return 2
    if args.reset_db:
        reset_mysql_tables(config)
    db.init_schema()
    db.seed_field_catalog()
    ai_mode = "off" if args.no_ai else args.ai_mode
    if ai_mode == "sync":
        jd_v2.init_ai_extractor(
            args.ai_provider or args.ai_model,
            args.ai_api_key,
            args.ai_base_url,
            model_name=args.ai_model_name,
            profile=args.ai_profile,
            mysql_config=config,
        )
    handlers = build_handlers(args)
    platforms = ["jd", "ejy365", "cquae", "ali", "tpre", "prechina", "gxcq"] if args.platform == "all" else [args.platform]
    runner = MultiPlatformRunner(
        db=db,
        handlers=handlers,
        ai_enabled=ai_mode != "off",
        ai_mode=ai_mode,
        item_concurrency=args.item_concurrency,
    )
    if args.platform_concurrency > 1 and len(platforms) > 1:
        results = []
        with ThreadPoolExecutor(max_workers=min(args.platform_concurrency, len(platforms))) as executor:
            futures = {
                executor.submit(runner.crawl_platform, platform, args.limit): platform
                for platform in platforms
            }
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    results.append(future.result().__dict__)
                except Exception as exc:
                    results.append(
                        {
                            "platform": platform,
                            "batch_id": "",
                            "scanned_count": 0,
                            "success_count": 0,
                            "failed_count": args.limit,
                            "errors": [{"item": "platform", "error": str(exc)}],
                        }
                    )
    else:
        results = [runner.crawl_platform(platform, limit=args.limit).__dict__ for platform in platforms]
    report_path = generate_model_quality_report(config, output_dir=args.output_dir, run_results=results)
    payload = {"results": results, "quality_report": str(report_path)}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if all(result.get("success_count", 0) > 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
