"""
京东资产拍卖采集器 v2.0
集成：
- 集中配置管理 (Phase 1)
- 自定义异常体系 (Phase 1)
- 结构化日志 (Phase 1)
- 字段标准化引擎 (Phase 2)
- 多来源冲突检测 (Phase 2)
- AI 辅助提取兜底 (Phase 2)
"""

import argparse
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests

import jd.ai_extractor as ai_extractor_module
import jd.conflict_detector as conflict_detector_module
import jd.field_standardizer as field_standardizer_module

# ===== Phase 1 集成：基础设施 =====
from jd.config import Config, get_config, set_config
from jd.exceptions import CrawlError, ExtractionError, JDAPIError, DatabaseError
from jd.logger import get_logger

# ===== Phase 2 集成：数据处理引擎 =====
from jd.field_standardizer import FieldStandardizer
from jd.conflict_detector import ConflictDetector
from jd.ai_extractor import (
    AIExtractionContext,
    create_ai_extractor,
    FIELD_DEFINITIONS,
    AI_DETAIL_TEXT_LIMIT,
)
from jd.ai_config import load_mysql_ai_profile, resolve_ai_config

# 初始化全局日志
logger = get_logger()
cfg = get_config()


def sync_module_loggers(configured_logger) -> None:
    ai_extractor_module.logger = configured_logger
    conflict_detector_module.logger = configured_logger
    field_standardizer_module.logger = configured_logger

# 字段标准化器
standardizer = FieldStandardizer()

# 冲突检测器
conflict_detector = ConflictDetector()

# AI 提取器（按需初始化）
ai_extractor = None
_active_ai_batch_budgets: Dict[str, int] = {}

TIME_FIELD_KEYS = {"signup_start_time", "signup_end_time"}
MEDIA_SPECIAL_FIELD_BY_GROUP = {
    "land": "site_images",
    "real_estate": "site_images",
    "equipment": "site_images",
    "vehicle": "vehicle_images",
}
SPECIAL_AREA_FIELD_KEYS = {"land_area", "building_area"}
COMMON_AI_OVERRIDE_KEYS = {
    "signup_start_time",
    "signup_end_time",
    "contact_info",
    "special_notice",
    "assessment_price_time",
}
COMMON_AI_FILL_KEYS = COMMON_AI_OVERRIDE_KEYS | {"disposal_party"}
SPECIAL_NOTICE_HEADINGS = (
    "特别告知",
    "特别提示",
    "特别提醒",
    "特别说明",
    "重要提示",
    "注意事项",
    "重大事项",
    "重大风险提示",
    "瑕疵说明",
    "风险提示",
)
ATTACHMENT_KEYWORDS = ("清单", "债权", "挂牌", "明细", "资产", "户", "截止", "项目")
ATTACHMENT_EXCLUDE_KEYWORDS = (
    "须知",
    "承诺",
    "提醒",
    "提示",
    "公告",
    "协议",
    "通知",
    "判决",
    "决定",
    "裁定",
    "执行",
    "调解",
    "评估",
    "确认",
    "保密",
    "从业",
    "成交",
    "破产",
    "形成",
    "回单",
    "欠款",
    "登记",
    "记账",
    "凭证",
    "审计",
    "备忘录",
)
DEBT_LEGACY_AGGREGATE_FIELDS = (
    "principal_balance",
    "interest_balance",
    "guarantors",
    "collateral",
)
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def init_ai_extractor(
    model: str = "",
    api_key: str = "",
    base_url: str = "",
    *,
    model_name: str = "",
    profile: str = "",
    mysql_config: Any = None,
) -> None:
    """Initialize the global AI extractor from CLI, MySQL profile, or .env."""
    global ai_extractor
    mysql_profile = None
    if mysql_config is not None:
        mysql_profile = load_mysql_ai_profile(mysql_config, profile or cfg.ai.active_profile)
    cli_values = {
        key: value
        for key, value in {
            "profile_name": profile or cfg.ai.active_profile,
            "provider": model or cfg.ai.model,
            "model_name": model_name or cfg.ai.model_name,
            "api_key": api_key or cfg.ai.api_key,
            "base_url": base_url or cfg.ai.base_url,
            "vision_model": cfg.ai.vision_model,
            "timeout": cfg.ai.timeout if cfg.ai.timeout else "",
            "max_retries": cfg.ai.max_retries if cfg.ai.max_retries else "",
            "qps": cfg.ai.qps if cfg.ai.qps and cfg.ai.qps != 10 else "",
        }.items()
        if value not in (None, "")
    }
    resolved = resolve_ai_config(
        mysql_profile=mysql_profile,
        cli=cli_values,
    )
    if not resolved.api_key:
        ai_extractor = None
        logger.warning(
            "ai_extractor_disabled",
            "AI extractor is disabled because no API key was found in CLI, MySQL, or .env",
            ai_profile=resolved.profile_name,
            ai_provider=resolved.provider,
            ai_source=resolved.source,
        )
        return
    ai_extractor = create_ai_extractor(
        resolved.to_extractor_config(
            enable_single_field_fallback=cfg.ai.enable_single_field_fallback,
            circuit_breaker_failures=cfg.ai.circuit_breaker_failures,
            circuit_breaker_cooldown_seconds=cfg.ai.circuit_breaker_cooldown_seconds,
        )
    )
    if ai_extractor is not None:
        setattr(ai_extractor, "profile_name", resolved.profile_name)
        setattr(ai_extractor, "provider", resolved.provider)
        setattr(ai_extractor, "model_name", resolved.model_name)
    logger.info(
        "ai_extractor_enabled",
        "AI extractor enabled",
        ai_profile=resolved.profile_name,
        ai_provider=resolved.provider,
        ai_model=resolved.model_name,
        ai_source=resolved.source,
    )


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JDCategory:
    category_id: str
    name: str


@dataclass
class ParsedHTML:
    key_values: Dict[str, str]
    text: str
    rows: List[List[str]]
    image_urls: Optional[List[str]] = None


# ===== 原有字段定义 =====
COMMON_FIELDS: Tuple[FieldDef, ...] = (
    FieldDef("asset_type", "标的类型", ("资产类型", "类别")),
    FieldDef("asset_location", "标的所在地", ("所在地", "项目所在地", "资产所在地")),
    FieldDef("project_status", "项目状态", ("拍卖状态", "当前状态", "交易状态")),
    FieldDef("auction_stage", "拍卖阶段", ("拍卖轮次", "阶段")),
    FieldDef("bid_records_json", "出价记录", ("竞价记录", "出价信息")),
    FieldDef("data_source", "数据来源", ("来源", "采集来源")),
    FieldDef("project_name", "项目名称", ("标的名称", "标题", "拍品名称")),
    FieldDef("signup_start_time", "报名开始时间", ("报名起始时间", "报名开始", "竞价开始时间", "拍卖开始时间", "拍卖时间", "竞价时间")),
    FieldDef("signup_end_time", "报名截止时间", ("报名结束时间", "报名截止", "竞价截止时间", "拍卖截止时间", "结束时间", "止")),
    FieldDef("disposal_party", "处置方", ("委托方", "处置机构", "拍卖机构", "机构名称")),
    FieldDef("disposal_agency", "处置机构", ("交易机构", "交易中心", "中介机构", "服务机构", "拍卖机构", "管理人")),
    FieldDef("start_price_raw", "起拍价", ("挂牌价", "初始价格", "转让底价")),
    FieldDef("final_price_raw", "最终价", ("当前价", "成交价", "最新价")),
    FieldDef("contact_info", "联系方式", ("联系人", "联系电话", "咨询电话", "联系方式")),
    FieldDef("special_notice", "特别告知", ("注意事项", "特别提醒", "瑕疵说明")),
    FieldDef("assessment_price_time", "评估价格及时间", ("评估价", "评估价格", "评估时间", "市场价", "市场价格")),
    FieldDef("attachments_json", "附件材料", ("附件", "材料", "相关附件")),
)

SPECIAL_FIELDS: Dict[str, Tuple[FieldDef, ...]] = {
    "land": (
        FieldDef("right_certificate_no", "权证编号", ("不动产权证号", "土地证号", "证号")),
        FieldDef("land_area", "土地面积", ("宗地面积", "面积")),
        FieldDef("land_use", "土地用途", ("土地规划用途", "规划用途", "用途")),
        FieldDef("use_term", "使用期限", ("土地使用期限", "终止日期")),
        FieldDef("land_location", "土地位置", ("坐落", "位置")),
        FieldDef("right_holder", "权利人", ("所有权人", "产权人")),
        FieldDef("land_status", "土地状态", ("现状", "使用状态")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "瑕疵说明", "风险提示")),
        FieldDef("site_images", "现场图片", ("图片", "现场照片")),
        FieldDef("land_type", "土地类型", ("土地具体类型", "权利类型")),
        FieldDef("assessment_time_value", "评估时间及价值", ("评估价", "评估价格", "评估时间")),
    ),
    "real_estate": (
        FieldDef("right_certificate_no", "权证编号", ("不动产权证号", "房产证号", "证号")),
        FieldDef("building_area", "建筑面积", ("房屋建筑面积", "套内建筑面积", "面积", "出租面积", "租赁面积", "承租面积")),
        FieldDef("property_use", "房产用途", ("规划用途", "用途", "允许从事行业", "可从事行业", "经营业态", "准入业态")),
        FieldDef("use_term", "使用年限", ("使用期限", "土地使用期限", "出租期限", "租赁期限", "承租期限", "租期")),
        FieldDef("property_location", "房产位置", ("坐落", "位置")),
        FieldDef("property_structure", "房产结构", ("建筑用料", "结构")),
        FieldDef("property_status", "房产状态", ("现状", "使用状态")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示")),
        FieldDef("site_images", "现场图片", ("图片", "现场照片")),
        FieldDef("property_type", "房产类型", ("房屋类型", "物业类型")),
        FieldDef("asset_highlights", "资产亮点", ("亮点", "核心优势")),
    ),
    "equipment": (
        FieldDef("storage_location", "存放位置", ("设备存放地", "所在地", "位置")),
        FieldDef("equipment_status", "设备状态", ("现状", "使用状态")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示")),
        FieldDef("site_images", "现场图片", ("图片", "现场照片")),
        FieldDef("equipment_type", "设备类型", ("设备具体类型", "种类")),
    ),
    "vehicle": (
        FieldDef("storage_location", "存放位置", ("车辆放置地点", "所在地", "位置")),
        FieldDef("vehicle_brand_model", "车型品牌", ("品牌型号", "车辆品牌", "车型")),
        FieldDef("vehicle_usage", "车辆使用情况", ("出厂日期", "里程数", "使用情况")),
        FieldDef("plate_number", "车牌号", ("号牌号码", "牌照号")),
        FieldDef("vehicle_configuration", "车辆配置", ("配置", "排量", "功率")),
        FieldDef("vehicle_status", "车辆状态", ("现状", "使用状态")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "违章", "事故", "保险")),
        FieldDef("vehicle_images", "车辆图片", ("图片", "车辆照片")),
        FieldDef("vehicle_type", "车辆类型", ("类型", "车辆种类")),
    ),
    "debt": (
        FieldDef("debtor_name", "主债务人名称", ("主债务人", "债务人", "借款人", "客户名称", "贷款主体")),
        FieldDef("creditor", "债权人", ("权利人", "转让方", "出让方", "委托方", "委托人", "中国东方")),
        FieldDef("guarantee_method", "担保方式", ("担保类型", "保证方式", "抵押顺位")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示", "特别提示", "特别说明", "特别告知")),
        FieldDef("litigation_status", "诉讼状态总述", ("诉讼进展", "执行情况", "案件状态", "执行状态")),
        FieldDef("household_count", "户数", ("债权笔数", "户")),
        FieldDef("benchmark_date", "基准日", ("截至日", "截止日", "债权基准日")),
    ),
    "equity": (
        FieldDef("transferor", "转让方", ("出让方", "处置方")),
        FieldDef("target_company", "标的企业", ("企业名称", "公司名称")),
        FieldDef("equity_ratio", "股权占比", ("持股比例", "股权比例")),
        FieldDef("company_nature", "企业性质", ("公司性质", "企业类型")),
        FieldDef("company_industry", "企业行业", ("所属行业", "行业")),
        FieldDef("business_scope", "经营范围", ("主营业务",)),
        FieldDef("ownership_structure", "股权结构", ("股东结构",)),
        FieldDef("financial_metrics", "财务指标", ("财务数据", "营业收入", "利润总额")),
        FieldDef("asset_valuation", "资产评估", ("资产总额", "负债总额", "净资产")),
        FieldDef("disclosure_items", "公示事项", ("重大事项", "风险提示")),
        FieldDef("attached_assets", "附带标的", ("附带资产", "同步转让")),
    ),
    "ip": (
        FieldDef("subject_name", "标的名称", ("名称", "知识产权名称")),
        FieldDef("certificate_no", "标的证号", ("证书号", "登记号", "申请号", "专利号", "作品号")),
        FieldDef("ip_type", "知产类型", ("知识产权类型", "类型", "权利类型")),
        FieldDef("ip_count", "知产数量", ("项数", "数量")),
        FieldDef("specific_category", "具体类别", ("类别", "小类")),
        FieldDef("right_holder", "权利人", ("所有权人", "著作权人", "专利权人")),
        FieldDef("subject_intro", "标的简介", ("简介", "基本情况")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示")),
        FieldDef("right_term", "权利期限", ("有效期", "保护期限")),
    ),
    "goods": (
        FieldDef("goods_category", "物资种类", ("种类", "类别")),
        FieldDef("goods_name", "物资名称", ("名称", "标的名称")),
        FieldDef("goods_location", "物资所在位置", ("所在地", "存放位置")),
        FieldDef("goods_details", "物资详情", ("详情", "规格", "数量")),
        FieldDef("right_holder", "权利人", ("所有权人", "产权人")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示")),
        FieldDef("right_burden", "权利负担", ("查封", "抵押", "负担")),
    ),
    "usufruct": (
        FieldDef("right_category", "权益种类", ("权益类型", "权利类型")),
        FieldDef("subject_name", "标的名称", ("名称", "项目名称")),
        FieldDef("subject_location", "标的所在位置", ("所在地", "位置")),
        FieldDef("subject_details", "标的物详情", ("详情", "标的详情")),
        FieldDef("valid_period", "有效期", ("期限", "权利期限")),
        FieldDef("original_right_holder", "原权利人", ("权利人", "原产权人")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示")),
        FieldDef("right_burden", "权利负担", ("查封", "抵押", "负担")),
    ),
    "other": (
        FieldDef("raw_detail_text", "原始详情文本", ("详情文本",)),
        FieldDef("raw_table_pairs_json", "原始表格键值对", ("表格键值",)),
        FieldDef("extracted_summary", "提取摘要", ("标的摘要", "资产摘要", "项目摘要")),
    ),
}

COMMON_FIELD_DATA_TYPES: Dict[str, str] = {
    "project_status": "VARCHAR(20)",
    "auction_stage": "VARCHAR(20)",
    "bid_records_json": "JSON",
    "signup_start_time": "DATETIME",
    "signup_end_time": "DATETIME",
    "start_price_raw": "VARCHAR(100)",
    "final_price_raw": "VARCHAR(100)",
    "assessment_price_time": "VARCHAR(500)",
    "attachments_json": "JSON",
}

SPECIAL_FIELD_DATA_TYPES: Dict[str, Dict[str, str]] = {
    "debt": {"household_count": "INTEGER"},
    "ip": {"ip_count": "INTEGER", "application_date": "DATE"},
}

SPECIAL_NORMALIZED_COLUMNS: Dict[str, Dict[str, str]] = {
    "land": {
        "land_area_sqm": "DECIMAL(18,2)",
        "assessment_amount": "DECIMAL(18,2)",
        "assessment_date": "DATE",
    },
    "real_estate": {
        "building_area_sqm": "DECIMAL(18,2)",
    },
    "debt": {
        "benchmark_date_norm": "DATE",
    },
}

DEDUP_FIELDS_CONFIG: Dict[str, Tuple[str, ...]] = {
    "debt": ("debtor_name", "creditor", "principal_balance", "claim_total", "asset_location"),
    "real_estate": ("right_certificate_no", "asset_location", "property_location", "building_area"),
    "land": ("right_certificate_no", "asset_location", "land_location", "land_area"),
    "vehicle": ("plate_number", "vehicle_brand_model", "storage_location"),
    "equipment": ("equipment_type", "storage_location", "project_name"),
    "equity": ("target_company", "equity_ratio"),
    "ip": ("subject_name", "right_holder", "ip_count"),
    "goods": ("goods_name", "goods_location", "project_name"),
    "usufruct": ("subject_name", "subject_location", "right_category"),
    "other": ("project_name", "asset_location"),
}

ASSET_GROUP_LABELS = {
    "land": "土地",
    "real_estate": "房地产",
    "equipment": "设备",
    "vehicle": "车辆",
    "debt": "债权",
    "equity": "股权",
    "ip": "知识产权",
    "goods": "物资产品",
    "usufruct": "用益物权",
    "other": "其他",
}

ASSET_TABLES = {
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

CATEGORY_GROUPS = {
    "101": "real_estate",
    "102": "real_estate",
    "103": "real_estate",
    "104": "real_estate",
    "105": "vehicle",
    "106": "vehicle",
    "107": "vehicle",
    "108": "equity",
    "109": "debt",
    "110": "usufruct",
    "111": "usufruct",
    "112": "land",
    "114": "equipment",
    "116": "ip",
    "117": "usufruct",
    "118": "goods",
    "119": "goods",
    "120": "goods",
    "121": "goods",
    "122": "goods",
    "124": "goods",
    "125": "goods",
    "126": "goods",
    "127": "goods",
    "128": "goods",
    "129": "goods",
    "130": "goods",
    "131": "goods",
    "132": "goods",
    "133": "goods",
    "134": "goods",
}

FALLBACK_CATEGORIES = (
    JDCategory("101", "住宅用房"),
    JDCategory("102", "商业用房"),
    JDCategory("103", "工业用房"),
    JDCategory("104", "其他用房"),
    JDCategory("105", "机动车"),
    JDCategory("106", "船舶"),
    JDCategory("107", "其他交通运输工具"),
    JDCategory("108", "股权"),
    JDCategory("109", "债权"),
    JDCategory("110", "矿权"),
    JDCategory("111", "林权"),
    JDCategory("112", "土地"),
    JDCategory("113", "工程"),
    JDCategory("114", "机械设备"),
    JDCategory("115", "无形资产"),
    JDCategory("116", "知识产权"),
    JDCategory("117", "租赁/经营权"),
    JDCategory("118", "奢侈品"),
    JDCategory("119", "生活物资"),
    JDCategory("120", "工业物资"),
    JDCategory("121", "库存物资"),
    JDCategory("122", "打包处置"),
    JDCategory("123", "其他财产"),
    JDCategory("124", "大宗农产品"),
    JDCategory("125", "其他大宗"),
    JDCategory("126", "加贸边角料"),
    JDCategory("127", "废旧物资"),
    JDCategory("128", "黄金"),
    JDCategory("129", "酒水"),
    JDCategory("130", "珠宝首饰"),
    JDCategory("131", "生鲜渔获"),
    JDCategory("132", "废旧资产"),
    JDCategory("133", "电子产品"),
    JDCategory("134", "矿产资源"),
)

STATUS_MAP = {
    0: "预告中",
    1: "进行中",
    2: "已结束",
    5: "已撤回",
    6: "已暂缓",
    7: "已中止",
}

STAGE_MAP = {
    1: "一拍",
    2: "二拍",
    3: "再次拍卖",
    4: "变卖",
    6: "破产",
}


# ===== 原有工具函数（保留功能不变） =====
TERMINAL_STATUS_MAP = {
    3: "已拍出",
    5: "已撤回",
    6: "已暂缓",
    7: "已中止",
}

PAIMAI_TIMES_MAP = {
    1: "一拍",
    2: "二拍",
    4: "变卖",
}


def positive_number(value: Any) -> bool:
    text = compact_text(value)
    if not text:
        return False
    try:
        return Decimal(text.replace(",", "")) > 0
    except InvalidOperation:
        return False


def to_int(value: Any) -> Optional[int]:
    text = compact_text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


MONEY_WITH_UNIT_RE = re.compile(
    r"(?:[¥￥]\s*)?-?\d[\d,]*(?:\.\d+)?\s*(?:(?:万亿|亿元)|(?:万元|人民币元)|(?:人民币|亿)|(?:万|元)|(?:yuan|Yuan)|(?:YUAN|RMB)|CNY)?"
)
LABELED_MONEY_RE = re.compile(
    r"(?:评估价(?:(?:格|值))?|(?:评估价值|市场价)(?:格)?|(?:起拍价|转让底价)|(?:债权合计|债权总额)|"
    r"(?:本金余额|本金)|(?:利息余额|利息)|(?:欠息|费用)|金额)\s*[:：]?\s*"
    r"(?P<amount>[¥￥]?\s*-?\d[\d,]*(?:\.\d+)?\s*(?:(?:万亿|亿元)|(?:万元|人民币元)|(?:人民币|亿)|(?:万|元)|(?:yuan|Yuan)|(?:YUAN|RMB)|CNY)?)"
)


def money_candidate_text(value: Any) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    labeled = LABELED_MONEY_RE.search(text)
    if labeled:
        return labeled.group("amount")
    token = MONEY_WITH_UNIT_RE.search(text)
    if not token:
        return None
    candidate = token.group(0).strip()
    has_money_marker = any(marker in candidate for marker in ("¥", "￥", "元", "万", "亿", "人民币", "yuan", "Yuan", "YUAN", "RMB", "CNY"))
    if has_money_marker:
        return candidate
    if DATE_CANDIDATE_RE.search(text):
        return None
    if re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", text):
        return candidate
    return None


def money_numeric(value: Any) -> Optional[Decimal]:
    text = money_candidate_text(value)
    if not text:
        return None
    standardized = standardizer.money(text)
    if standardized.numeric is not None:
        return standardized.numeric
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    if "万亿" in text:
        amount *= Decimal("1000000000000")
    elif "亿" in text:
        amount *= Decimal("100000000")
    elif "万" in text:
        amount *= Decimal("10000")
    return amount


def decimal_to_db(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")), "f")


def datetime_to_db(value: Any) -> Optional[str]:
    parsed = parse_datetime_value(value)
    if not parsed:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


DATE_CANDIDATE_RE = re.compile(
    r"(?P<year>\d{4})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})\s*日?"
)


def date_to_db(value: Any) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    match = DATE_CANDIDATE_RE.search(text)
    if match:
        try:
            return dt.date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
    standardized = standardizer.date(text)
    if standardized.iso_date:
        return standardized.iso_date
    if standardized.iso_datetime:
        return standardized.iso_datetime[:10]
    return None


def contextual_date_to_db(value: Any, context_tokens: Tuple[str, ...]) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    if context_tokens and not any(token in text for token in context_tokens):
        return None
    return date_to_db(text)


def area_sqm_to_db(value: Any) -> Optional[str]:
    standardized = standardizer.area(value)
    if standardized.sqm_equivalent is None:
        return None
    return decimal_to_db(standardized.sqm_equivalent)


def normalize_dedup_part(field_key: str, value: Any) -> str:
    text = compact_text(value) or ""
    if not text:
        return ""
    if any(token in field_key for token in ("area", "面积")):
        standardized = standardizer.area(text)
        if standardized.sqm_equivalent is not None:
            return f"{standardized.sqm_equivalent.normalize()}sqm"
    if any(token in field_key for token in ("price", "amount", "balance", "total", "金额", "价格")):
        amount = money_numeric(text)
        if amount is not None:
            return format(amount.quantize(Decimal("0.01")), "f")
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:、（）()\[\]【】<>《》\"'“”‘’|/\\_-]+", "", text)
    return text


def compute_dedup_hash(asset_group: str, common_values: Dict[str, Any], special_values: Dict[str, Any]) -> Optional[str]:
    fields = DEDUP_FIELDS_CONFIG.get(asset_group) or DEDUP_FIELDS_CONFIG["other"]
    parts: List[str] = []
    for field_key in fields:
        value = special_values.get(field_key)
        if is_blank(value):
            value = common_values.get(field_key)
        normalized = normalize_dedup_part(field_key, value)
        if normalized:
            parts.append(f"{field_key}={normalized}")
    if not parts:
        return None
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def normalized_common_db_values(
    *,
    asset_group: str,
    common_values: Dict[str, Any],
    special_values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    special_values = special_values or {}
    start_price_display = compact_text(common_values.get("start_price_raw"))
    final_price_display = compact_text(common_values.get("final_price_raw"))
    current_price_display = compact_text(
        common_values.get("current_price_raw") or common_values.get("current_price") or final_price_display
    )
    assessment_amount = money_numeric(common_values.get("assessment_price_time"))
    assessment_date = contextual_date_to_db(
        common_values.get("assessment_price_time"),
        ("评估", "估价", "市场价", "市场价格"),
    )
    return {
        "source_platform": "jd",
        "source_item_id": compact_text(common_values.get("paimai_id")),
        "disposal_agency": compact_text(common_values.get("disposal_agency") or common_values.get("disposal_party")),
        "signup_start_time_norm": datetime_to_db(common_values.get("signup_start_time")),
        "signup_end_time_norm": datetime_to_db(common_values.get("signup_end_time")),
        "start_price_display": start_price_display,
        "start_price_amount": decimal_to_db(money_numeric(common_values.get("start_price_raw"))),
        "current_price_display": current_price_display,
        "current_price_amount": decimal_to_db(money_numeric(current_price_display)),
        "final_price_display": final_price_display,
        "final_price_amount": decimal_to_db(money_numeric(common_values.get("final_price_raw"))),
        "assessment_price_amount": decimal_to_db(assessment_amount),
        "assessment_amount": decimal_to_db(assessment_amount),
        "assessment_date": assessment_date,
        "dedup_hash": compute_dedup_hash(asset_group, common_values, special_values),
    }


MONEY_EVIDENCE_FIELDS = {
    "start_price_raw",
    "final_price_raw",
    "current_price_raw",
    "current_price",
    "assessment_price_time",
    "assessment_time_value",
    "principal_balance",
    "interest_balance",
    "recovery_fee",
    "claim_total",
}
DATETIME_EVIDENCE_FIELDS = {"signup_start_time", "signup_end_time"}
DATE_EVIDENCE_FIELDS = {"benchmark_date", "application_date", "assessment_price_time", "assessment_time_value"}


def typed_field_extraction_values(field_key: str, value: Any) -> Dict[str, Any]:
    text = compact_text(value)
    if is_blank(text):
        return {"value_type": None, "numeric_value": None, "date_value": None, "datetime_value": None}

    numeric_value = None
    date_value = None
    datetime_value = None
    value_type = "text"

    if field_key in MONEY_EVIDENCE_FIELDS or any(
        token in field_key for token in ("price", "amount", "balance", "fee", "total", "valuation")
    ):
        numeric_value = decimal_to_db(money_numeric(text))
        if numeric_value is not None:
            value_type = "money"

    if field_key in DATETIME_EVIDENCE_FIELDS:
        datetime_value = datetime_to_db(text)
        if datetime_value:
            value_type = "datetime"

    if field_key in DATE_EVIDENCE_FIELDS:
        if field_key in {"assessment_price_time", "assessment_time_value"} and not has_assessment_date_text(text):
            date_value = None
        else:
            date_value = date_to_db(text)
        if date_value and value_type == "text":
            value_type = "date"

    if value_type == "text" and any(token in field_key for token in ("date", "time")):
        date_value = date_to_db(text)
        datetime_value = datetime_to_db(text)
        if datetime_value:
            value_type = "datetime"
        elif date_value:
            value_type = "date"

    return {
        "value_type": value_type,
        "numeric_value": numeric_value,
        "date_value": date_value,
        "datetime_value": datetime_value,
    }


def normalized_special_db_values(asset_group: str, values: Dict[str, Any]) -> Dict[str, Any]:
    if asset_group == "land":
        assessment_text = values.get("assessment_time_value")
        return {
            "land_area_sqm": area_sqm_to_db(values.get("land_area")),
            "assessment_amount": decimal_to_db(money_numeric(assessment_text)),
            "assessment_date": contextual_date_to_db(assessment_text, ("评估", "估价", "市场价", "市场价格")),
        }
    if asset_group == "real_estate":
        return {
            "building_area_sqm": area_sqm_to_db(values.get("building_area")),
        }
    if asset_group == "debt":
        return {
            "benchmark_date_norm": date_to_db(values.get("benchmark_date")),
        }
    return {}


def compute_project_status(
    *,
    auction_status_code: Any,
    signup_start_time: Any = None,
    signup_end_time: Any = None,
    auction_start_time: Any = None,
    auction_end_time: Any = None,
    remain_time: Any = None,
    realtime_active: bool = False,
    start_price: Any = None,
    final_price: Any = None,
    now: Optional[dt.datetime] = None,
) -> str:
    code = to_int(auction_status_code)
    if code in TERMINAL_STATUS_MAP:
        return TERMINAL_STATUS_MAP[code]

    now_value = now or dt.datetime.now()
    signup_start = parse_datetime_value(signup_start_time)
    signup_end = parse_datetime_value(signup_end_time)
    auction_start = parse_datetime_value(auction_start_time) or signup_start
    auction_end = parse_datetime_value(auction_end_time) or signup_end

    first_start = auction_start or signup_start
    if code == 1 and (realtime_active or positive_number(remain_time) or (auction_end and now_value <= auction_end)):
        return "竞价中"
    if first_start and now_value < first_start:
        return "未开始"
    if signup_start and auction_start and signup_start < auction_start and signup_start <= now_value < auction_start:
        return "报名中"
    if auction_end and now_value <= auction_end:
        return "竞价中"

    start_numeric = money_numeric(start_price)
    final_numeric = money_numeric(final_price)
    if start_numeric is not None and final_numeric is not None:
        return "已成交" if final_numeric > start_numeric else "未成交"
    if code == 2:
        return "已结束"
    return "竞价中" if first_start else (compact_text(auction_status_code) or "未知")


def infer_auction_stage_from_text(*texts: Any) -> Optional[str]:
    text = compact_text(" ".join(compact_text(item) or "" for item in texts)) or ""
    if not text:
        return None
    if any(keyword in text for keyword in ("二拍", "第二次拍卖", "第二次")):
        return "二拍"
    if any(keyword in text for keyword in ("一拍", "第一次拍卖", "第一次")):
        return "一拍"
    if "变卖" in text:
        return "变卖"
    if any(keyword in text for keyword in ("重新拍卖", "再次拍卖")):
        return "再次拍卖"
    if "破产" in text:
        return "破产"
    if "竞价" in text:
        return "竞价"
    if "拍卖" in text:
        return "拍卖"
    return None


def compute_auction_stage(
    paimai_times_code: Any,
    auction_status_code: Any = None,
    *texts: Any,
) -> str:
    status_code = to_int(auction_status_code)
    if status_code == 5:
        return "撤拍"
    if status_code in {3, 7}:
        return "终止"
    stage_code = to_int(paimai_times_code)
    if stage_code in PAIMAI_TIMES_MAP:
        return PAIMAI_TIMES_MAP[stage_code]
    inferred = infer_auction_stage_from_text(*texts)
    if inferred:
        return inferred
    if stage_code == 0:
        return "竞价"
    return compact_text(paimai_times_code) or "未知"


def realtime_indicates_active(realtime_data: Dict[str, Any], auction_end_time: Any = None) -> bool:
    status_code = to_int(realtime_data.get("auctionStatus"))
    if status_code != 1:
        return False
    if positive_number(realtime_data.get("remainTime")):
        return True
    realtime_end = parse_datetime_value(format_time(realtime_data.get("endTime")) or auction_end_time)
    if realtime_end and realtime_end >= dt.datetime.now():
        return True
    return False


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def compact_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        text = safe_json_dumps(value)
    else:
        text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def is_blank(value: Any) -> bool:
    return compact_text(value) is None


def first_non_blank(*values: Any) -> Any:
    for value in values:
        if not is_blank(value):
            return value
    return None


def normalize_label(label: str) -> str:
    label = html.unescape(label or "")
    label = re.sub(r"\s+", "", label)
    label = re.sub(r"^[一二三四五六七八九十\d]+[、.．]\s*", "", label)
    label = re.sub(r"[（(][^）)]*[）)]?", "", label)
    label = label.strip("：:；;，,。.【】[]（）() ")
    return label


def looks_like_label(text: str) -> bool:
    text = normalize_label(text)
    if not text:
        return False
    if re.search(r"\d{4}年|\d+[,.]?\d*|https?://", text):
        return False
    return len(text) <= 10


def is_likely_key_value_row(cells: List[str]) -> bool:
    if len(cells) < 2 or len(cells) % 2 != 0:
        return False
    first = normalize_label(cells[0])
    if first in {"序号", "编号"} or re.fullmatch(r"\d+", first or ""):
        return False
    keys = cells[0::2]
    values = cells[1::2]
    if not all(looks_like_label(key) for key in keys):
        return False
    if values and all(looks_like_label(value) for value in values):
        return False
    return True


class KVHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._text: List[str] = []
        self.image_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_map = {key.lower(): value for key, value in attrs if value}
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
        elif tag == "img":
            for attr_name in ("src", "data-src", "data-original", "data-lazyload"):
                url = normalize_jd_media_url(attrs_map.get(attr_name))
                if url and url not in self.image_urls:
                    self.image_urls.append(url)
                    break
        elif tag in {"p", "div", "br", "li"}:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None:
            cell = compact_text("".join(self._cell))
            if cell and self._row is not None:
                self._row.append(cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag in {"p", "div", "li"}:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        self._text.append(data)

    @property
    def text(self) -> str:
        lines = []
        for line in "".join(self._text).splitlines():
            clean = compact_text(line)
            if clean:
                lines.append(clean)
        return "\n".join(lines)


def extract_key_values_from_html(html_text: Optional[str]) -> ParsedHTML:
    parser = KVHTMLParser()
    parser.feed(html_text or "")
    key_values: Dict[str, str] = {}

    for row in parser.rows:
        cells = [cell for cell in row if not is_blank(cell)]
        if len(cells) < 2:
            continue
        if not is_likely_key_value_row(cells):
            continue
        pairs = zip(cells[0::2], cells[1::2])
        for key, value in pairs:
            clean_key = normalize_label(key)
            clean_value = compact_text(value)
            if clean_key and clean_value and clean_key not in key_values:
                key_values[clean_key] = clean_value

    for line in parser.text.splitlines():
        match = re.match(r"^\s*(?:[一二三四五六七八九十\d]+[、.．]\s*)?([^：:]{2,30})[：:]\s*(.+?)\s*$", line)
        if not match:
            continue
        key = normalize_label(match.group(1))
        value = compact_text(match.group(2))
        if key and value and key not in key_values:
            key_values[key] = value

    return ParsedHTML(key_values=key_values, text=parser.text, rows=parser.rows, image_urls=parser.image_urls)


def classify_category(category: JDCategory) -> str:
    return CATEGORY_GROUPS.get(category.category_id, "other")


def find_by_alias(parsed: ParsedHTML, aliases: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    normalized = {normalize_label(key): value for key, value in parsed.key_values.items()}
    for alias in aliases:
        key = normalize_label(alias)
        if key in normalized and not is_blank(normalized[key]):
            precise_value, precise_excerpt = extract_labeled_text_value(parsed.text, (alias,))
            if not is_blank(precise_value):
                return precise_value, precise_excerpt
            fallback_value = truncate_labeled_value(normalized[key])
            return fallback_value or normalized[key], f"{alias}：{normalized[key]}"

    for line in parsed.text.splitlines():
        for alias in aliases:
            pattern = rf"{re.escape(alias)}\s*[：:]\s*(.+?)(?:$|。|；|;)"
            match = re.search(pattern, line)
            if match:
                value = truncate_labeled_value(match.group(1))
                if value:
                    return value, line[:300]
    return None, None


def extract_creditor_from_notice(text: str) -> Optional[str]:
    candidates: List[str] = []
    patterns = (
        r"(中国东方资产管理股份有限公司[^\n，。；;]{0,40}(?:分公司)?)",
        r"(中国信达资产管理股份有限公司[^\n，。；;]{0,40}(?:分公司)?)",
        r"(中国华融资产管理股份有限公司[^\n，。；;]{0,40}(?:分公司)?)",
        r"(中国长城资产管理股份有限公司[^\n，。；;]{0,40}(?:分公司)?)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = compact_text(match.group(1))
            if value and value not in candidates:
                candidates.append(value)
    return candidates[0] if candidates else None


# ===== 新增：AI 辅助提取兜底函数 =====
def ai_extract_field(
    field_key: str,
    field_label: str,
    html_key_values: Dict[str, str],
    detail_text: str,
    notice_text: str,
    asset_group: str,
    paimai_id: str,
) -> Tuple[Optional[str], float]:
    """使用 AI 提取字段值兜底"""
    if ai_extractor is None:
        return None, 0.0

    field_def = FIELD_DEFINITIONS.get(field_key, {"label": field_label, "description": field_label})
    context = AIExtractionContext(
        html_key_values=html_key_values,
        detail_text=detail_text[:AI_DETAIL_TEXT_LIMIT],
        notice_text=notice_text[:AI_NOTICE_TEXT_LIMIT],
        asset_group=asset_group,
        paimai_id=paimai_id,
    )

    try:
        result = ai_extractor.extract_field(
            field_key=field_key,
            field_label=field_def.get("label", field_label),
            field_description=field_def.get("description", field_label),
            context=context,
        )
        return result.value, result.confidence
    except Exception as e:
        logger.error(
            "ai_extraction_error",
            f"AI 提取失败: {field_key}",
            field_key=field_key,
            paimai_id=paimai_id,
            error=str(e),
        )
        return None, 0.0


def is_ai_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return compact_text(value) is None or compact_text(value) in {"null", "None", "无", "暂无", "/"}
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def is_zero_like(value: Any) -> bool:
    text = compact_text(value)
    if not text:
        return False
    normalized = (
        text.replace(",", "")
        .replace("，", "")
        .replace("￥", "")
        .replace("¥", "")
        .replace("人民币", "")
        .replace("万元", "")
        .replace("元", "")
        .strip()
    )
    if not normalized:
        return False
    try:
        return Decimal(normalized) == 0
    except InvalidOperation:
        return bool(re.fullmatch(r"0+(?:\.0+)?", normalized))


ASSESSMENT_CONTEXT_TERMS = ("评估", "市场价", "市场价格", "market price", "appraisal", "valuation")
ASSESSMENT_DATE_TERMS = ("评估基准日", "评估日期", "评估时间", "appraisal date", "valuation date")
ASSESSMENT_RATIO_RE = re.compile(
    r"(?:评估(?:(?:价|价格)|价值)?|市场(?:(?:价|价格))|market\s+(?:price|appraisal)|valuation)"
    r"\s*[:：]?\s*[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:(?:倍|折)|(?:成|次)|%|％)",
    re.IGNORECASE,
)
ASSESSMENT_LABELED_AMOUNT_RE = re.compile(
    r"(?:评估(?:(?:价|价格)|价值)?|市场(?:(?:价|价格))|market\s+(?:price|appraisal)|valuation)"
    r"\s*[:：]?\s*[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:(?:亿元|万元)|(?:元|万)|(?:亿|yuan)|(?:rmb|cny))?"
    r"(?!\s*(?:(?:倍|折)|(?:成|次)|%|％))",
    re.IGNORECASE,
)
ASSESSMENT_MONEY_MARKER_RE = re.compile(r"[¥￥元万亿]|(?:人民币|yuan)|(?:rmb|cny)", re.IGNORECASE)


def has_assessment_context(text: Any) -> bool:
    normalized = compact_text(text) or ""
    lowered = normalized.lower()
    return any(term in lowered for term in ASSESSMENT_CONTEXT_TERMS)


def has_assessment_date_text(text: Any) -> bool:
    normalized = compact_text(text) or ""
    lowered = normalized.lower()
    return any(term in lowered for term in ASSESSMENT_DATE_TERMS) and bool(DATE_CANDIDATE_RE.search(normalized))


def has_valid_assessment_money_text(text: Any) -> bool:
    normalized = compact_text(text) or ""
    if not normalized or ASSESSMENT_RATIO_RE.search(normalized):
        return False
    for match in ASSESSMENT_LABELED_AMOUNT_RE.finditer(normalized):
        snippet = match.group(0)
        if ASSESSMENT_MONEY_MARKER_RE.search(snippet) or re.search(r"[:：]", snippet):
            return money_numeric(snippet) is not None
    return False


def is_valid_assessment_price_time(
    value: Any,
    source_text: Any = None,
    *,
    structured_assessment_field: bool = False,
    require_source_assessment_signal: bool = False,
) -> bool:
    text = compact_text(display_ai_value(value)) or ""
    if not text or is_zero_like(text) or ASSESSMENT_RATIO_RE.search(text):
        return False
    if structured_assessment_field:
        return money_numeric(text) is not None or has_assessment_date_text(text)

    source = compact_text(source_text) or ""
    if require_source_assessment_signal and not has_assessment_context(source):
        return False
    if has_assessment_date_text(text) or has_valid_assessment_money_text(text):
        return True
    if money_numeric(text) is not None and has_assessment_context(source):
        if has_assessment_context(text):
            return False
        if ASSESSMENT_RATIO_RE.search(source) and not ASSESSMENT_MONEY_MARKER_RE.search(text):
            return False
        return True
    return False


def normalize_assessment_price_time(
    value: Any,
    source_text: Any = None,
    *,
    structured_assessment_field: bool = False,
    require_source_assessment_signal: bool = False,
) -> Optional[str]:
    text = compact_text(display_ai_value(value))
    if not text or is_zero_like(text):
        return None
    if not is_valid_assessment_price_time(
        text,
        source_text,
        structured_assessment_field=structured_assessment_field,
        require_source_assessment_signal=require_source_assessment_signal,
    ):
        return None
    return text


def ai_field_tuple(field: FieldDef) -> Tuple[str, str, str]:
    field_def = FIELD_DEFINITIONS.get(field.key, {})
    return (
        field.key,
        field_def.get("label", field.label),
        field_def.get("description", field.label),
    )


AI_NOTICE_TEXT_LIMIT = 30000
AI_TABLE_ROWS_LIMIT = 300
AI_TARGET_TABLE_ROWS_LIMIT = 20
AI_IMAGE_URL_LIMIT = 50


def extract_activity_time_hints(*texts: Any, max_hints: int = 12) -> List[str]:
    hints: List[str] = []

    def add_hint(value: Any) -> None:
        hint = compact_text(value) or ""
        if not hint or hint in hints:
            return
        if not re.search(r"\d{4}|[一二三四五六七八九十]|\d{1,2}\s*(?:(?:时|点))", hint):
            return
        misleading = ("公告发布日期", "发布时间", "展示看样期", "看样时间", "资质审核截止日", "资质审核截止时间")
        if any(word in hint for word in misleading) and not any(word in hint for word in ("竞价", "拍卖", "变卖")):
            return
        hints.append(hint[:500])

    time_patterns = (
        r"[^。；;\n]{0,100}(?:(?:竞价时间|拍卖时间)|(?:拍卖竞价时间|报名时间)|(?:竞买时间|变卖时间)|(?:将于|定于))"
        r"[^。；;\n]{0,260}(?:(?:止|结束)|截止)[^。；;\n]{0,80}",
        r"[^。；;\n]{0,120}\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
        r"[^。；;\n]{0,220}(?:(?:起|至)|到)[^。；;\n]{0,220}(?:(?:止|结束)|截止)[^。；;\n]{0,80}",
        r"[^。；;\n]{0,120}\d{4}[-/]\d{1,2}[-/]\d{1,2}"
        r"[^。；;\n]{0,220}(?:(?:起|至)|到|-|—|~|～)[^。；;\n]{0,220}(?:(?:止|结束)|截止)[^。；;\n]{0,80}",
    )
    for text in texts:
        clean = compact_text(text) or ""
        if not clean:
            continue
        for span in auction_time_candidate_spans(clean):
            add_hint(span)
            if len(hints) >= max_hints:
                return hints
        for pattern in time_patterns:
            for match in re.finditer(pattern, clean):
                add_hint(match.group(0))
                if len(hints) >= max_hints:
                    return hints
    return hints


def project_table_match_tokens(project_name: Any) -> List[str]:
    text = compact_text(project_name)
    if not text:
        return []
    normalized = re.sub(r"\s+", "", text)
    tokens: List[str] = []

    def add(token: Any) -> None:
        clean = compact_text(token)
        if clean and clean not in tokens:
            tokens.append(clean)

    add(normalized)
    for match in re.finditer(
        r"[A-Za-z]?\d+[A-Za-z]?\s*(?:(?:号楼|幢)|(?:栋|座)|(?:层|室)|(?:房|铺)|(?:商铺|车位)|(?:摩托车位|号))?(?:\s*(?:(?:摩托车位|车位)))?",
        text,
    ):
        add(match.group(0))
    for match in re.finditer(r"\d+\s*号\s*(?:(?:摩托车位|车位)|(?:房|室)|(?:铺|商铺))?", text):
        add(match.group(0))
    return [token for token in tokens if len(token) >= 2]


def _looks_like_table_header(row: List[str]) -> bool:
    joined = compact_text(" ".join(row))
    if not joined:
        return False
    header_hits = sum(
        1
        for token in ("序号", "标的", "产权", "权证", "面积", "评估价", "起拍价", "保证金", "增价")
        if token in joined
    )
    return header_hits >= 2


def focus_table_rows_for_project(
    rows: List[List[str]],
    project_name: Any,
    *,
    max_rows: int = 8,
) -> List[List[str]]:
    tokens = project_table_match_tokens(project_name)
    if not rows or not tokens:
        return []

    scored: List[Tuple[int, int]] = []
    for index, row in enumerate(rows):
        row_text = compact_text(" ".join(row))
        if not row_text or _looks_like_table_header(row):
            continue
        score = 0
        for token in tokens:
            if token in row_text:
                score += max(len(token), 2)
        if score:
            scored.append((score, index))
    if not scored:
        return []

    best_score = max(score for score, _ in scored)
    matched_indexes = [index for score, index in scored if score == best_score]
    header_indexes = [index for index, row in enumerate(rows) if _looks_like_table_header(row) and index <= matched_indexes[0]]
    selected: List[int] = []
    if header_indexes:
        selected.append(header_indexes[-1])
    selected.extend(matched_indexes)

    focused: List[List[str]] = []
    for index in selected:
        if 0 <= index < len(rows) and rows[index] not in focused:
            focused.append(rows[index])
        if len(focused) >= max_rows:
            break
    return focused


def build_resource_payload(files: Any, core: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(files, list):
        file_items = files
    elif files:
        file_items = [files]
    else:
        file_items = []
    return {"files": file_items, "media": extract_media(core or {})}


def build_ai_context(
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    asset_group: str,
    paimai_id: str,
    project_name: Any = None,
) -> AIExtractionContext:
    key_values: Dict[str, str] = {}
    image_urls: List[str] = []
    if project_name:
        key_values["_current_project_name"] = compact_text(project_name) or ""
    focused_rows: List[List[str]] = []
    for source_rows in (parsed.rows, notice_parsed.rows):
        focused_rows.extend(focus_table_rows_for_project(source_rows, project_name))
    if focused_rows:
        key_values["_target_item_table_rows_json"] = safe_json_dumps(focused_rows[:AI_TARGET_TABLE_ROWS_LIMIT])
    for parsed_part in (parsed, notice_parsed):
        for url in parsed_part.image_urls or []:
            if url not in image_urls:
                image_urls.append(url)
    activity_time_hints = extract_activity_time_hints(parsed.text, notice_parsed.text)
    if activity_time_hints:
        key_values["_activity_time_hints"] = safe_json_dumps(activity_time_hints)
    if image_urls:
        key_values["_image_urls_json"] = safe_json_dumps(image_urls[:AI_IMAGE_URL_LIMIT])
    key_values.update(parsed.key_values)
    if parsed.rows:
        key_values["_detail_table_rows_json"] = safe_json_dumps(parsed.rows[:AI_TABLE_ROWS_LIMIT])
    if notice_parsed.rows:
        key_values["_notice_table_rows_json"] = safe_json_dumps(notice_parsed.rows[:AI_TABLE_ROWS_LIMIT])
    return AIExtractionContext(
        html_key_values=key_values,
        detail_text=(parsed.text or "")[:AI_DETAIL_TEXT_LIMIT],
        notice_text=(notice_parsed.text or "")[:AI_NOTICE_TEXT_LIMIT],
        image_urls=image_urls,
        asset_group=asset_group,
        paimai_id=paimai_id,
    )


def start_ai_batch_budget(paimai_id: str) -> None:
    if getattr(cfg.ai, "max_batches_per_item", 0) > 0:
        _active_ai_batch_budgets[paimai_id] = 0


def clear_ai_batch_budget(paimai_id: str) -> None:
    _active_ai_batch_budgets.pop(paimai_id, None)


def consume_ai_batch_budget(paimai_id: str, field_count: int) -> bool:
    max_batches = getattr(cfg.ai, "max_batches_per_item", 0)
    if max_batches <= 0 or paimai_id not in _active_ai_batch_budgets:
        return True
    used = _active_ai_batch_budgets.get(paimai_id, 0)
    if used >= max_batches:
        logger.info(
            "ai_batch_budget_skipped",
            "快速模式下跳过本标的后续批量 AI 请求",
            paimai_id=paimai_id,
            used_batches=used,
            max_batches=max_batches,
            field_count=field_count,
        )
        return False
    _active_ai_batch_budgets[paimai_id] = used + 1
    return True


def ai_batch_extract_fields(
    fields: List[Tuple[str, str, str]],
    *,
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    asset_group: str,
    paimai_id: str,
    project_name: Any = None,
) -> Dict[str, Any]:
    if ai_extractor is None or not fields:
        return {}
    if not consume_ai_batch_budget(paimai_id, len(fields)):
        return {}
    context = build_ai_context(parsed, notice_parsed, asset_group, paimai_id, project_name=project_name)
    try:
        return ai_extractor.batch_extract(fields, context)
    except Exception as exc:
        logger.error(
            "batch_ai_extraction_error",
            f"批量 AI 提取失败: {exc}",
            paimai_id=paimai_id,
            error=str(exc),
        )
        return {}


def special_ai_field_tuples(asset_group: str) -> List[Tuple[str, str, str]]:
    if asset_group not in SPECIAL_FIELDS:
        return []
    if asset_group == "other":
        return [
            (
                "extracted_summary",
                "其他资产提取摘要",
                (
                    "从标的详情、竞买公告和竞买须知中提炼本次标的摘要，说明标的物名称、范围、"
                    "所在地、权属或处置主体、主要瑕疵/风险。不要摘录模板条款；找不到则返回 null。"
                ),
            )
        ]
    fields = [ai_field_tuple(field) for field in SPECIAL_FIELDS[asset_group]]
    if asset_group == "debt":
        fields.append(
            (
                "debt_package_details_json",
                "debt package details",
                (
                    "Extract row-level debt package details as a JSON array. "
                    "Each row should include sequence_no, debtor_name, guarantor, collateral, "
                    "principal_balance, interest_balance, recovery_fee, claim_total, "
                    "litigation_status, benchmark_date, amount_unit, source_excerpt. "
                    "Do not return headers, totals, unit notes, risk notices, or summary rows."
                ),
            )
        )
    if asset_group == "ip":
        ip_definition = FIELD_DEFINITIONS.get("ip_details", {})
        fields.append(
            (
                "ip_details",
                ip_definition.get("label", "ip details"),
                ip_definition.get(
                    "description",
                    (
                        "Extract row-level intellectual property details as a JSON array. "
                        "Each row should include sequence_no, ip_name, certificate_no, "
                        "ip_type, application_date, patent_type, status, source_excerpt."
                    ),
                ),
            )
        )
    return fields


def prefetch_combined_ai_results(
    *,
    asset_group: str,
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    paimai_id: str,
    project_name: Any = None,
) -> Dict[str, Any]:
    if ai_extractor is None:
        return {}
    fields: List[Tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(field_tuple: Tuple[str, str, str]) -> None:
        if field_tuple[0] in seen:
            return
        seen.add(field_tuple[0])
        fields.append(field_tuple)

    for field in COMMON_FIELDS:
        if field.key in COMMON_AI_FILL_KEYS:
            add(ai_field_tuple(field))
    for field_tuple in special_ai_field_tuples(asset_group):
        add(field_tuple)

    return ai_batch_extract_fields(
        fields,
        parsed=parsed,
        notice_parsed=notice_parsed,
        asset_group=asset_group,
        paimai_id=paimai_id,
        project_name=project_name,
    )


def parse_chinese_datetime_text(text: str) -> Optional[str]:
    matches = _chinese_datetime_matches(text)
    if not matches or not matches[0][3]:
        return None
    return _format_chinese_match_datetime(matches[0])


def _native_chinese_hour(hour: Optional[str], meridiem: Optional[str] = None) -> Optional[str]:
    if not hour:
        return None
    value = int(hour)
    marker = meridiem or ""
    if marker in {"下午", "晚上", "傍晚"} and 1 <= value < 12:
        value += 12
    elif marker == "中午" and value < 12:
        value = 12
    elif marker == "凌晨" and value == 12:
        value = 0
    return str(value)


def _chinese_datetime_matches(text: str) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    native_pattern = re.compile(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*"
        r"(?:((?:上午|下午)|(?:晚上|傍晚)|(?:中午|凌晨)|早上)?\s*"
        r"(\d{1,2})(?:(?:[:：]\s*(\d{1,2}))|(?:\s*[时点](?:\s*(\d{1,2})分?)?)))?"
    )
    collected: List[Tuple[int, Tuple[str, str, str, Optional[str], Optional[str]]]] = []
    for match in native_pattern.finditer(text):
        year, month, day, meridiem, hour, minute_a, minute_b = match.groups()
        minute = minute_a or minute_b
        collected.append((match.start(), (year, month, day, _native_chinese_hour(hour, meridiem), minute)))
    collected.sort(key=lambda item: item[0])
    return [item for _, item in collected]


def _format_chinese_match_datetime(
    match: Tuple[str, str, str, Optional[str], Optional[str]],
    fallback_hour: int = 0,
) -> str:
    year, month, day, hour, minute = match
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
        f"{int(hour) if hour else fallback_hour:02d}:{int(minute or 0):02d}:00"
    )


def parse_auction_time_range(text: str, field_key: str) -> Optional[str]:
    clean = compact_text(text) or ""
    if not clean:
        return None
    matches = _chinese_datetime_matches(clean)
    timed_matches = [match for match in matches if match[3]]
    if len(timed_matches) >= 2:
        return _format_chinese_match_datetime(
            timed_matches[0] if field_key == "signup_start_time" else timed_matches[1]
        )
    if len(timed_matches) == 1:
        time_only = re.search(
            r"(?:(?:起|开始))\s*(?:(?:至|到)|—|-|~)?\s*(\d{1,2})(?:(?:[:：]\s*(\d{1,2}))|(?:\s*[时点](?:\s*(\d{1,2})分?)?))\s*(?:(?:止|结束))?",
            clean,
        )
        if time_only and field_key == "signup_end_time":
            year, month, day, _, _ = timed_matches[0]
            end_hour, minute_a, minute_b = time_only.groups()
            return (
                f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
                f"{int(end_hour):02d}:{int(minute_a or minute_b or 0):02d}:00"
            )
    pattern = re.compile(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2})(?:[:：时点]\s*(\d{1,2}))?"
        r"\s*(?:(?:起|开始))?\s*(?:(?:至|到)|—|-|~)?\s*"
        r"(?:(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*)?"
        r"(\d{1,2})(?:[:：时点]\s*(\d{1,2}))?\s*(?:(?:止|结束))?"
    )
    match = pattern.search(clean)
    if not match:
        return None
    (
        start_year,
        start_month,
        start_day,
        start_hour,
        start_minute,
        end_year,
        end_month,
        end_day,
        end_hour,
        end_minute,
    ) = match.groups()
    if field_key == "signup_start_time":
        return (
            f"{int(start_year):04d}-{int(start_month):02d}-{int(start_day):02d} "
            f"{int(start_hour):02d}:{int(start_minute or 0):02d}:00"
        )
    year = end_year or start_year
    month = end_month or start_month
    day = end_day or start_day
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {int(end_hour):02d}:{int(end_minute or 0):02d}:00"


AUCTION_TIME_KEYWORDS = (
    "拍卖竞价时间",
    "竞价时间",
    "拍卖时间",
    "变卖时间",
    "竞价活动",
    "拍卖活动",
    "公开拍卖活动",
    "公开竞价活动",
    "将于",
    "定于",
)
AUCTION_TIME_EXCLUDE_KEYWORDS = (
    "看样",
    "展示",
    "公告期",
    "资质审核",
    "报名截止",
    "保证金",
    "尾款",
    "付款",
)


def auction_time_candidate_spans(text: Any) -> List[str]:
    clean = compact_text(text) or ""
    if not clean:
        return []
    spans: List[str] = []
    sentences = re.split(r"(?<=[。；;])", clean)
    for sentence in sentences:
        if any(keyword in sentence for keyword in AUCTION_TIME_KEYWORDS):
            spans.append(sentence)
    for keyword in AUCTION_TIME_KEYWORDS:
        for match in re.finditer(re.escape(keyword), clean):
            start = max(0, match.start() - 120)
            end = min(len(clean), match.end() + 220)
            spans.append(clean[start:end])
    unique: List[str] = []
    for span in spans:
        span = compact_text(span) or ""
        if not span or span in unique:
            continue
        if any(keyword in span for keyword in AUCTION_TIME_EXCLUDE_KEYWORDS) and not any(
            keyword in span for keyword in ("拍卖", "竞价", "变卖")
        ):
            continue
        unique.append(span)
    return unique


def extract_auction_time_range_text(*texts: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    candidates: List[str] = []
    for text in texts:
        candidates.extend(auction_time_candidate_spans(text))
    for span in candidates:
        start = parse_auction_time_range(span, "signup_start_time")
        end = parse_auction_time_range(span, "signup_end_time")
        if start and end:
            return start, end, span
    return None, None, None


def normalize_time_field(field_key: str, value: Any, source_text: Any = None) -> Any:
    if field_key not in TIME_FIELD_KEYS:
        return value
    source_range = parse_auction_time_range(compact_text(source_text) or "", field_key)
    value_text = compact_text(value) or ""
    value_has_time = bool(re.search(r"\d{1,2}[:：时点]\d{0,2}", value_text))
    if source_range and not value_has_time:
        return source_range
    chinese_datetime = parse_chinese_datetime_text(value_text)
    if chinese_datetime:
        return chinese_datetime
    standardized = standardizer.date(value)
    normalized = standardized.iso_datetime or standardized.display or value
    if source_range and str(normalized).endswith("00:00:00"):
        return source_range
    return normalized


def is_valid_ai_time_source(field_key: str, source_text: Any) -> bool:
    if field_key not in TIME_FIELD_KEYS:
        return True
    source = compact_text(source_text) or ""
    if not source:
        return False
    has_activity_keyword = any(keyword in source for keyword in AUCTION_TIME_KEYWORDS)
    has_range_marker = any(marker in source for marker in ("起", "至", "到", "止", "结束", "截止"))
    if parse_auction_time_range(source, field_key):
        return True
    misleading = (
        "看样",
        "展示",
        "预约",
        "咨询",
        "开拍前",
        "拍卖开始前",
        "竞价开始前",
        "资质审核",
        "公告发布",
        "公告期",
        "保证金",
        "尾款",
        "付款",
    )
    if any(word in source for word in misleading):
        return False
    if field_key == "signup_end_time" and not (has_activity_keyword and has_range_marker):
        return False
    return has_activity_keyword or has_range_marker


def time_value_conflicts_with_pair(field_key: str, value: Any, values: Dict[str, Any]) -> bool:
    candidate = parse_datetime_value(value)
    if not candidate:
        return False
    if field_key == "signup_end_time":
        start = parse_datetime_value(values.get("signup_start_time"))
        return bool(start and candidate <= start)
    if field_key == "signup_start_time":
        end = parse_datetime_value(values.get("signup_end_time"))
        return bool(end and candidate >= end)
    return False


def time_value_conflicts_with_source_pair(
    field_key: str,
    value: Any,
    source_text: Any,
    values: Dict[str, Any],
) -> bool:
    if field_key not in TIME_FIELD_KEYS:
        return False
    source_start, source_end, _ = extract_auction_time_range_text(source_text)
    if not source_start or not source_end:
        return False
    if field_key == "signup_end_time":
        existing_start = parse_datetime_value(values.get("signup_start_time"))
        source_start_dt = parse_datetime_value(source_start)
        return bool(existing_start and source_start_dt and existing_start != source_start_dt)
    if field_key == "signup_start_time":
        existing_end = parse_datetime_value(values.get("signup_end_time"))
        source_end_dt = parse_datetime_value(source_end)
        return bool(existing_end and source_end_dt and existing_end != source_end_dt)
    return False


def ai_result_source_text(ai_result: Any) -> str:
    return (
        compact_text(getattr(ai_result, "original_text", None))
        or compact_text(getattr(ai_result, "source_text", None))
        or compact_text(getattr(ai_result, "reasoning", None))
        or ""
    )


def display_ai_value(value: Any) -> Any:
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list, tuple)) for item in value):
            return "；".join(compact_text(item) or "" for item in value if not is_blank(item))
        return safe_json_dumps(value)
    if isinstance(value, dict):
        return safe_json_dumps(value)
    return value


def has_contact_signal(value: Any) -> bool:
    text = compact_text(value) or ""
    return bool(re.search(r"(?:0\d{2,4}-?\d{6,8}|1[3-9]\d{9}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)", text))


CONTACT_SIGNAL_RE = re.compile(r"(?:0\d{2,4}-?\d{6,8}|1[3-9]\d{9}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)")
PHONE_SIGNAL_RE = re.compile(r"(?:0\d{2,4}-?\d{6,8}|1[3-9]\d{9})")
CONTACT_NAME_STOPWORDS = {
    "联系电话",
    "咨询电话",
    "联系方式",
    "联系号码",
    "联系人",
    "电话",
    "手机",
    "管理人咨询电话",
    "管理员咨询电话",
    "法院咨询电话",
    "京东平台咨询电话",
}


def normalize_contact_phone(signal: str) -> str:
    return signal.replace("-", "") if signal.startswith("0") else signal


def clean_contact_name(name: Any) -> Optional[str]:
    text = compact_text(name)
    if not text:
        return None
    text = re.sub(r"^(?:(?:咨询|看样)|(?:管理人|管理员))?联系人\s*[:：]?", "", text)
    text = re.sub(r"^(?:(?:联系电话|咨询电话)|(?:联系方式|电话)\d?|手机)\s*[:：]?", "", text)
    text = text.strip(" ：:，,、；;()（）")
    if not text or text in CONTACT_NAME_STOPWORDS:
        return None
    if any(word in text for word in ("电话", "联系", "咨询", "保证金", "账户", "客服")):
        return None
    if len(text) > 12:
        return None
    return text


def extract_contact_entries_from_line(line: Any) -> List[str]:
    clean = compact_text(line) or ""
    if not clean:
        return []
    phones = PHONE_SIGNAL_RE.findall(clean)
    if not phones:
        return []
    entries: List[str] = []
    used: set[str] = set()

    leading_name = re.search(
        r"(?:(?:咨询联系人|看样联系人)|联系人)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z]{2,12}(?:(?:先生|女士)|(?:法官|经理))?)",
        clean,
    )
    if leading_name:
        name = clean_contact_name(leading_name.group(1))
        if name:
            phone = phones[0]
            entries.append(f"{name} {phone}")
            used.add(normalize_contact_phone(phone))

    pair_pattern = re.compile(
        rf"([\u4e00-\u9fffA-Za-z]{{2,12}}(?:(?:先生|女士)|(?:法官|经理))?)\s*[:：,，、]?\s*({PHONE_SIGNAL_RE.pattern})"
    )
    for match in pair_pattern.finditer(clean):
        name = clean_contact_name(match.group(1))
        phone = match.group(2)
        normalized_phone = normalize_contact_phone(phone)
        if not name or normalized_phone in used:
            continue
        entries.append(f"{name} {phone}")
        used.add(normalized_phone)

    for phone in phones:
        normalized_phone = normalize_contact_phone(phone)
        if normalized_phone not in used:
            entries.append(phone)
            used.add(normalized_phone)
    return list(dict.fromkeys(entries))


def normalize_contact_info(value: Any) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    raw_parts = re.split(r"[;\n\r；]+", text)
    parts: List[str] = []
    seen_signals: set[str] = set()
    for raw in raw_parts:
        part = compact_text(raw)
        if not part:
            continue
        expanded_entries = extract_contact_entries_from_line(part)
        if expanded_entries:
            for entry in expanded_entries:
                signals = CONTACT_SIGNAL_RE.findall(entry)
                normalized_signals = [normalize_contact_phone(signal) for signal in signals]
                if all(signal in seen_signals for signal in normalized_signals):
                    continue
                parts.append(entry)
                seen_signals.update(normalized_signals)
            continue
        signals = CONTACT_SIGNAL_RE.findall(part)
        if not signals:
            continue
        normalized_signals = [normalize_contact_phone(signal) for signal in signals]
        if all(signal in seen_signals for signal in normalized_signals):
            continue
        cleaned = re.sub(
            r"^(?:(?:咨询电话|联系电话)|(?:联系方式|联系号码)|电话\d?|(?:中国东方咨询电话|京东平台咨询电话))\s*[:：]?\s*",
            "",
            part,
        )
        cleaned = compact_text(cleaned) or part
        parts.append(cleaned)
        seen_signals.update(normalized_signals)
    return "；".join(parts) if parts else None


def has_special_notice_heading(value: Any) -> bool:
    text = compact_text(value) or ""
    if not text:
        return False
    for heading in SPECIAL_NOTICE_HEADINGS:
        pattern = rf"(?:^|[\s。；;！？!?]|[一二三四五六七八九十\d]+[、.．]){re.escape(heading)}(?:\s*[:：、.．（(]|$)"
        if re.search(pattern, text):
            return True
    return False


def meaningful_special_notice_value(value: Any, source_path: str) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    if len(text) < 20 and has_special_notice_heading(text):
        return None
    return text


def parse_datetime_value(value: Any) -> Optional[dt.datetime]:
    text = compact_text(value)
    if not text:
        return None
    if text.isdigit():
        formatted = format_time(text)
        if formatted and formatted != text:
            text = formatted
    text = re.sub(r"([T ][0-2]\d:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$", r"\1", text)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y年%m月%d日%H时%M分",
        "%Y年%m月%d日%H时",
        "%Y年%m月%d日",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if fmt in {"%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"}:
                return parsed.replace(hour=23, minute=59, second=59)
            return parsed
        except ValueError:
            continue
    parsed = parse_chinese_datetime_text(text)
    if parsed and parsed != text:
        return parse_datetime_value(parsed)
    standardized = standardizer.date(text)
    candidate = standardized.iso_datetime or standardized.iso_date
    if candidate and candidate != text:
        return parse_datetime_value(candidate)
    return None


def adjust_project_status_by_time(
    values: Dict[str, Any],
    results: Dict[str, Dict[str, Any]],
    *,
    active_realtime: bool = False,
) -> None:
    if active_realtime:
        return
    status = compact_text(values.get("project_status"))
    if status in {"已拍出", "已撤回", "已暂缓", "已中止", "已成交", "未成交"}:
        return
    end_time = parse_datetime_value(values.get("signup_end_time"))
    if not end_time:
        return
    if end_time < dt.datetime.now():
        values["project_status"] = "已结束"
        previous = results.get("project_status", {})
        results["project_status"] = field_result_value(
            "已结束",
            previous.get("source_payload_type", "derived"),
            "signup_end_time_status_check",
            f"结束时间 {values.get('signup_end_time')} 已早于当前采集时间",
            "derived",
            min(float(previous.get("confidence", 0.9) or 0.9), 0.9),
        )


def attachment_name_and_url(attachment: Any) -> Tuple[str, str]:
    if not isinstance(attachment, dict):
        return "", ""
    name = first_non_blank(
        attachment.get("attachmentName"),
        attachment.get("fileName"),
        attachment.get("name"),
        attachment.get("title"),
        attachment.get("attachmentCode"),
    )
    url = first_non_blank(
        attachment.get("attachmentAddress"),
        attachment.get("fileUrl"),
        attachment.get("downloadUrl"),
        attachment.get("downloadURL"),
        attachment.get("url"),
        attachment.get("href"),
        attachment.get("attachmentUrl"),
        attachment.get("filePath"),
        attachment.get("path"),
        attachment.get("src"),
    )
    return compact_text(name) or "", compact_text(url) or ""


def is_debt_attachment_name(name: str) -> bool:
    clean = compact_text(name) or ""
    if not clean:
        return False
    if any(keyword in clean for keyword in ATTACHMENT_EXCLUDE_KEYWORDS):
        return False
    return any(keyword in clean for keyword in ATTACHMENT_KEYWORDS)


def download_attachment(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        response = requests.get(url, timeout=cfg.crawl.default_timeout, stream=True)
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_ATTACHMENT_BYTES:
            logger.warning("attachment_too_large", f"附件过大，跳过解析: {url[:120]}", size=len(content))
            return None
        return content
    except Exception as exc:
        logger.warning("attachment_download_failed", f"附件下载失败: {url[:120]}", error=str(exc))
        return None


def extract_text_from_attachment(content: bytes, filename: str) -> Optional[str]:
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        try:
            import PyPDF2

            reader = PyPDF2.PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            logger.warning("pdf_extract_failed", f"PDF 文本提取失败: {filename}", error=str(exc))
            return None
    if lower.endswith((".xlsx", ".xls")):
        try:
            import pandas as pd

            sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
            parts = []
            for sheet_name, df in sheets.items():
                parts.append(f"=== Sheet: {sheet_name} ===")
                parts.append(df.to_string(index=False))
            return "\n\n".join(parts)
        except Exception as exc:
            logger.warning("excel_extract_failed", f"Excel 文本提取失败: {filename}", error=str(exc))
            return None
    if lower.endswith(".docx"):
        try:
            import docx

            doc = docx.Document(io.BytesIO(content))
            return "\n".join(paragraph.text for paragraph in doc.paragraphs)
        except Exception as exc:
            logger.warning("word_extract_failed", f"Word 文本提取失败: {filename}", error=str(exc))
            return None
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning("attachment_text_decode_failed", f"附件文本解码失败: {filename}")
    return None


def ai_parse_attachment(attachment_text: str, filename: str, paimai_id: str) -> List[Dict[str, Any]]:
    if ai_extractor is None or not getattr(ai_extractor, "is_available", lambda: False)():
        return []
    text = attachment_text[:15000]
    context = AIExtractionContext(
        html_key_values={},
        detail_text=text,
        notice_text="",
        asset_group="debt",
        paimai_id=paimai_id,
    )
    try:
        result = ai_extractor.extract_field(
            "attachment_debt_details",
            f"附件债权明细（{filename}）",
            (
                "从附件文本中逐户提取债权资产明细，返回 JSON 数组。字段包括 sequence_no、debtor_name/"
                "debtor_or_asset、principal_balance、interest_balance、recovery_fee、claim_total、guarantor、"
                "collateral、litigation_status、benchmark_date、amount_unit、source_excerpt。忽略表头、合计行、说明文字。"
            ),
            context,
        )
    except Exception as exc:
        logger.warning("attachment_ai_parse_failed", f"AI 解析附件失败: {filename}", paimai_id=paimai_id, error=str(exc))
        return []
    return normalize_ai_debt_details(getattr(result, "value", None))


def extract_debt_details_from_attachments(
    attachments: Any,
    paimai_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if isinstance(attachments, dict):
        files = attachments.get("files") or attachments.get("data") or []
    elif isinstance(attachments, list):
        files = attachments
    else:
        files = []
    all_details: List[Dict[str, Any]] = []
    attachment_texts: Dict[str, Any] = {}
    for attachment in files:
        name, url = attachment_name_and_url(attachment)
        if not is_debt_attachment_name(name):
            continue
        content = download_attachment(url)
        if not content:
            continue
        text = extract_text_from_attachment(content, name)
        if not text:
            continue
        attachment_texts[name or url] = {"url": url, "text": text[:50000], "chars": len(text)}
        details = ai_parse_attachment(text, name or url, paimai_id)
        for detail in details:
            detail.setdefault("source_excerpt", f"附件：{name or url}")
        all_details.extend(details)
    return all_details, attachment_texts


def apply_common_ai_batch(
    values: Dict[str, Any],
    results: Dict[str, Dict[str, Any]],
    *,
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    asset_group: str,
    paimai_id: str,
    preloaded_ai_results: Optional[Dict[str, Any]] = None,
) -> None:
    fields = [
        ai_field_tuple(field)
        for field in COMMON_FIELDS
        if field.key in COMMON_AI_FILL_KEYS
        and (field.key in COMMON_AI_OVERRIDE_KEYS or is_blank(values.get(field.key)))
    ]
    if preloaded_ai_results is None:
        ai_results = ai_batch_extract_fields(
            fields,
            parsed=parsed,
            notice_parsed=notice_parsed,
            asset_group=asset_group,
            paimai_id=paimai_id,
        )
    else:
        wanted_keys = {field_key for field_key, _, _ in fields}
        ai_results = {
            field_key: ai_result
            for field_key, ai_result in preloaded_ai_results.items()
            if field_key in wanted_keys
        }
    for field_key, ai_result in ai_results.items():
        ai_value = getattr(ai_result, "value", None)
        if is_ai_blank(ai_value):
            continue
        source_text = ai_result_source_text(ai_result)
        if field_key == "contact_info" and not has_contact_signal(ai_value):
            logger.info(
                "batch_ai_common_field_skipped",
                "AI 联系方式缺少电话或邮箱，跳过覆盖",
                field_key=field_key,
                paimai_id=paimai_id,
            )
            continue
        if field_key == "special_notice" and not (
            has_special_notice_heading(source_text) or has_special_notice_heading(ai_value)
        ):
            logger.info(
                "batch_ai_common_field_skipped",
                "AI 特别告知缺少明确提示类标题，跳过覆盖",
                field_key=field_key,
                paimai_id=paimai_id,
            )
            continue
        if field_key == "assessment_price_time":
            ai_value = normalize_assessment_price_time(
                ai_value,
                source_text,
                require_source_assessment_signal=True,
            )
            if not ai_value:
                logger.info(
                    "batch_ai_common_field_skipped",
                    "AI 评估价格缺少明确评估上下文或为 0，跳过覆盖",
                    field_key=field_key,
                    paimai_id=paimai_id,
                )
                continue
        if field_key in TIME_FIELD_KEYS and not is_valid_ai_time_source(field_key, source_text):
            logger.info(
                "batch_ai_common_field_skipped",
                "AI 时间来源不是竞价/拍卖活动时段，跳过覆盖",
                field_key=field_key,
                paimai_id=paimai_id,
            )
            continue
        value = normalize_time_field(field_key, ai_value, source_text)
        if field_key in TIME_FIELD_KEYS and time_value_conflicts_with_source_pair(field_key, value, source_text, values):
            logger.info(
                "batch_ai_common_field_skipped",
                "AI 时间来自另一段完整竞价时间范围，避免与已提取时间拼接",
                field_key=field_key,
                paimai_id=paimai_id,
            )
            continue
        if field_key in TIME_FIELD_KEYS and time_value_conflicts_with_pair(field_key, value, values):
            logger.info(
                "batch_ai_common_field_skipped",
                "AI 时间与已提取的起止时间顺序冲突，跳过覆盖",
                field_key=field_key,
                paimai_id=paimai_id,
            )
            continue
        if field_key == "contact_info":
            value = normalize_contact_info(value)
            if not value:
                continue
        value = display_ai_value(value)
        values[field_key] = value
        results[field_key] = field_result_value(
            value,
            "ai_extraction",
            "llm_batch",
            source_text or getattr(ai_result, "reasoning", None),
            "ai",
            getattr(ai_result, "confidence", 0.0),
        )
        logger.info(
            "batch_ai_common_field_applied",
            f"批量 AI 提取已应用: {field_key}",
            field_key=field_key,
            paimai_id=paimai_id,
            confidence=getattr(ai_result, "confidence", 0.0),
        )


def first_mapping_value(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and not is_ai_blank(mapping.get(key)):
            return mapping.get(key)
    return None


def coerce_json_like(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return value
    return value


def normalize_ai_debt_details(raw_value: Any) -> List[Dict[str, Any]]:
    raw_value = coerce_json_like(raw_value)
    if isinstance(raw_value, dict):
        raw_value = first_mapping_value(raw_value, "details", "rows", "data", "items", "债权明细", "明细") or raw_value
    if not isinstance(raw_value, list):
        return []

    details: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_value, start=1):
        if not isinstance(item, dict):
            continue
        debtor_name = first_mapping_value(
            item,
            "debtor_name",
            "debtor_or_asset",
            "主债务人",
            "债务人",
            "借款人",
            "客户名称",
            "名称",
            "抵债资产",
        )
        guarantor = first_mapping_value(
            item,
            "guarantor",
            "guarantor_or_related_party",
            "保证人",
            "担保人",
            "相关人",
            "延伸债权相关义务人",
        )
        detail = {
            "sequence_no": first_mapping_value(item, "sequence_no", "序号", "编号") or str(index),
            "debtor_name": debtor_name,
            "debtor_or_asset": debtor_name,
            "guarantor": guarantor,
            "guarantor_or_related_party": guarantor,
            "collateral": first_mapping_value(item, "collateral", "抵质押物", "抵押物", "质押物", "担保物", "担保方式"),
            "principal_balance": first_mapping_value(item, "principal_balance", "本金余额", "本金", "借款本金", "贷款本金", "延伸债权本金"),
            "interest_balance": first_mapping_value(item, "interest_balance", "利息余额", "利息", "欠息", "欠息金额", "延伸债权利息"),
            "recovery_fee": first_mapping_value(item, "recovery_fee", "费用", "诉讼费", "实现债权费用", "挂账诉讼费"),
            "claim_total": first_mapping_value(item, "claim_total", "债权合计", "债权总额", "合计", "合计金额"),
            "litigation_status": first_mapping_value(item, "litigation_status", "诉讼状态", "执行状态", "案件状态"),
            "benchmark_date": first_mapping_value(item, "benchmark_date", "基准日", "截止日", "截至日期"),
            "amount_unit": first_mapping_value(item, "amount_unit", "单位") or "",
            "source_excerpt": first_mapping_value(item, "source_excerpt", "source_text", "原文片段") or "",
        }
        if any(not is_blank(detail.get(key)) for key in ("debtor_name", "principal_balance", "interest_balance", "claim_total")):
            details.append(detail)
    return details


def normalize_ai_ip_details(raw_value: Any) -> List[Dict[str, Any]]:
    raw_value = coerce_json_like(raw_value)
    if isinstance(raw_value, dict):
        raw_value = first_mapping_value(raw_value, "details", "rows", "data", "items", "ip_details", "明细") or raw_value
    if not isinstance(raw_value, list):
        return []

    details: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_value, start=1):
        if not isinstance(item, dict):
            continue
        detail = {
            "sequence_no": first_mapping_value(item, "sequence_no", "序号", "编号") or str(index),
            "ip_name": first_mapping_value(item, "ip_name", "name", "subject_name", "单项名称", "名称"),
            "certificate_no": first_mapping_value(item, "certificate_no", "cert_no", "patent_no", "registration_no", "申请号", "证号", "登记号", "专利号", "证书号"),
            "ip_type": first_mapping_value(item, "ip_type", "type", "知识产权类型", "类型"),
            "application_date": first_mapping_value(item, "application_date", "register_date", "registration_date", "申请日", "登记日期"),
            "patent_type": first_mapping_value(item, "patent_type", "专利类型"),
            "status": first_mapping_value(item, "status", "legal_status", "法律状态", "状态"),
            "source_excerpt": first_mapping_value(item, "source_excerpt", "source_text", "原文片段") or "",
        }
        if any(not is_blank(detail.get(key)) for key in ("ip_name", "certificate_no", "ip_type")):
            details.append(detail)
    return details


AGGREGATED_IP_DETAIL_RE = re.compile(r"[（(]?\s*\d+\s*(?:(?:项|件)|个)[）)]?")


def ip_details_look_aggregated(details: List[Dict[str, Any]]) -> bool:
    if not details:
        return False
    if len(details) > 3:
        return False
    for detail in details:
        name = compact_text(detail.get("ip_name")) or ""
        cert_no = compact_text(detail.get("certificate_no")) or ""
        if AGGREGATED_IP_DETAIL_RE.search(name) and not cert_no:
            return True
    return False


IP_COUNT_TYPES = (
    "计算机软件著作权",
    "软件著作权",
    "作品著作权",
    "外观设计专利",
    "实用新型专利",
    "发明专利",
    "著作权",
    "专利权",
    "商标权",
    "使用许可",
    "版权",
    "专利",
    "商标",
)


def normalize_ip_count_type(value: Any) -> str:
    text = compact_text(value) or ""
    if text == "专利":
        return "专利权"
    if text == "商标":
        return "商标权"
    return text


def extract_ip_summary_details_from_text(text: Any) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """从标题或正文中的“7项软件著作权及17项专利权”提取数量和概要明细。"""
    source = compact_text(text)
    if not source:
        return None, []
    type_pattern = "|".join(re.escape(item) for item in sorted(IP_COUNT_TYPES, key=len, reverse=True))
    patterns = (
        re.compile(rf"(?P<count>\d+)\s*(?:(?:项|件)|个)\s*(?P<type>{type_pattern})"),
        re.compile(rf"(?P<type>{type_pattern})\s*(?P<count>\d+)\s*(?:(?:项|件)|个)"),
    )
    matches: List[Tuple[str, int, str, int]] = []
    seen: set[Tuple[str, int, str]] = set()
    for pattern in patterns:
        for match in pattern.finditer(source):
            ip_type = normalize_ip_count_type(match.group("type"))
            try:
                count = int(match.group("count"))
            except (TypeError, ValueError):
                continue
            excerpt = compact_text(match.group(0)) or ""
            key = (ip_type, count, excerpt)
            if not ip_type or count <= 0 or key in seen:
                continue
            seen.add(key)
            matches.append((ip_type, count, excerpt, match.start()))
    matches.sort(key=lambda item: item[3])
    if not matches:
        return None, []

    total = sum(count for _, count, _, _ in matches)
    details = [
        {
            "sequence_no": str(index),
            "ip_name": f"{ip_type}（{count}项）",
            "certificate_no": "",
            "ip_type": ip_type,
            "application_date": "",
            "patent_type": "",
            "status": "",
            "source_excerpt": excerpt,
        }
        for index, (ip_type, count, excerpt, _) in enumerate(matches, start=1)
    ]
    return str(total), details


def right_holder_looks_like_asset_name(value: Any, values: Dict[str, Any]) -> bool:
    candidate = compact_text(value)
    if not candidate:
        return False
    normalized_candidate = re.sub(r"\s+", "", candidate)
    if len(normalized_candidate) < 3:
        return False
    for key in ("project_name", "subject_name", "goods_name"):
        asset_name = compact_text(values.get(key))
        if not asset_name:
            continue
        normalized_asset = re.sub(r"\s+", "", asset_name)
        if not normalized_asset:
            continue
        if normalized_candidate == normalized_asset:
            return True
        if len(normalized_candidate) >= 4 and (
            normalized_asset.startswith(normalized_candidate)
            or normalized_candidate.startswith(normalized_asset)
        ):
            return True
        shorter, longer = sorted((normalized_candidate, normalized_asset), key=len)
        if len(shorter) >= 6 and shorter in longer and len(shorter) / max(len(longer), 1) >= 0.75:
            return True
    return False


def ai_extract_debt_details(parsed: ParsedHTML, notice_parsed: ParsedHTML, paimai_id: str) -> Tuple[List[Dict[str, Any]], float]:
    fields = [
        (
            "debt_package_details_json",
            "债权资产包明细",
            "从表格或正文中逐户提取债权明细，value 必须是 JSON 数组。每行字段包括 sequence_no、debtor_name、guarantor、collateral、principal_balance、interest_balance、recovery_fee、claim_total、litigation_status、benchmark_date、amount_unit、source_excerpt。不要把表头、合计行或说明文字当成数据。",
        )
    ]
    ai_results = ai_batch_extract_fields(
        fields,
        parsed=parsed,
        notice_parsed=notice_parsed,
        asset_group="debt",
        paimai_id=paimai_id,
    )
    ai_result = ai_results.get("debt_package_details_json")
    if not ai_result or is_ai_blank(getattr(ai_result, "value", None)):
        return [], 0.0
    return normalize_ai_debt_details(getattr(ai_result, "value", None)), getattr(ai_result, "confidence", 0.0)

# ===== API 客户端（使用自定义异常 + 配置） =====
class JDClient:
    def __init__(self, throttle_seconds: float = cfg.crawl.default_throttle, timeout: int = cfg.crawl.default_timeout) -> None:
        self.throttle_seconds = throttle_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://pmsearch.jd.com",
                "Referer": "https://pmsearch.jd.com/?projectType=1",
                "Content-Type": "application/json;charset=UTF-8",
            }
        )
        logger.info(
            "jd_client_initialized",
            "JD API 客户端初始化完成",
            throttle_seconds=throttle_seconds,
            timeout=timeout,
        )

    def api(self, function_id: str, body: Dict[str, Any], appid: str = cfg.api.default_appid, referer: Optional[str] = None) -> Dict[str, Any]:
        """API 调用（带重试和自定义异常）"""
        if referer:
            self.session.headers["Referer"] = referer
        url = f"{cfg.api.jd_api_url}?appid={appid}&functionId={function_id}&body={quote(safe_json_dumps(body))}"
        last_error: Optional[Exception] = None

        for attempt in range(1, cfg.crawl.max_retries + 1):
            try:
                response = self.session.post(url, data="null", timeout=self.timeout)
                response.raise_for_status()
                time.sleep(self.throttle_seconds)
                result = json.loads(response.content.decode("utf-8", errors="replace"))
                logger.debug(
                    "api_call_success",
                    f"API 调用成功: {function_id}",
                    function_id=function_id,
                    attempt=attempt,
                )
                return result
            except Exception as exc:
                last_error = exc
                logger.log_api_retry(function_id, attempt, cfg.crawl.max_retries, str(exc))
                time.sleep(self.throttle_seconds * attempt * 2)

        raise JDAPIError(
            function_id=function_id,
            message=f"API 调用失败，已重试 {cfg.crawl.max_retries} 次",
            status_code=getattr(last_error, "status_code", None) if isinstance(last_error, requests.HTTPError) else None,
            original_error=last_error,
        )

    def get_categories(self) -> List[JDCategory]:
        try:
            data = self.api("paimai_getPublicSearchCategory", {}, referer="https://pmsearch.jd.com/?projectType=1")
            items = data.get("datas") or data.get("data") or []
            categories = [JDCategory(str(item["id"]), str(item["name"])) for item in items if item.get("id") and item.get("name")]
            return categories or list(FALLBACK_CATEGORIES)
        except JDAPIError:
            logger.warning("fallback_categories", "获取类目失败，使用备用类目列表")
            return list(FALLBACK_CATEGORIES)

    def search_items(self, category_id: str, page: int = 1, page_size: int = 2) -> Tuple[List[Dict[str, Any]], int]:
        body = {
            "investmentType": "",
            "apiType": 12,
            "page": page,
            "pageSize": page_size,
            "keyword": "",
            "provinceId": "",
            "cityId": "",
            "countyId": "",
            "multiPaimaiStatus": "",
            "multiDisplayStatus": "",
            "multiPaimaiTimes": "",
            "childrenCateId": category_id,
            "currentPriceRangeStart": "",
            "currentPriceRangeEnd": "",
            "timeRangeTime": "endTime",
            "timeRangeStart": "",
            "timeRangeEnd": "",
            "loan": "",
            "purchaseRestriction": "",
            "liupaiBuyAgain": "",
            "orgId": "",
            "orgType": "",
            "sortField": 8,
            "projectType": 1,
            "reqSource": 0,
            "labelSet": "",
            "publishSource": "",
        }
        data = self.api("paimai_unifiedSearch", body, referer="https://pmsearch.jd.com/?projectType=1")
        items = data.get("datas")
        if items is None and isinstance(data.get("result"), dict):
            items = data["result"].get("data") or data["result"].get("list")
        return list(items or []), int(data.get("totalItem") or 0)

    def fetch_detail_bundle(self, paimai_id: str, list_item: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug("fetch_detail_start", f"开始获取详情: {paimai_id}", paimai_id=paimai_id)
        referer = f"https://paimai.jd.com/{paimai_id}"
        core = self.api(
            "getWareCoreDataBff",
            {
                "paimaiId": int(paimai_id),
                "identityCode": 0,
                "customViewList": "1,4,5,6,7,8,9,10,11,12,13",
                "configEnums": "PAIMAI_LABEL_DATA,PAIMAI_ALLOCATION_DATA,PAIMAI_INSURANCE_DATA,"
                "PAIMAI_LOAN_DATA,PAIMAI_CAREFREE_DATA",
                "start": 0,
                "end": 20,
                "separateGetStart": 0,
                "separateGetEnd": 20,
            },
            appid="paimai-item-pc",
            referer=referer,
        )
        realtime = self.api(
            "getPaimaiRealTimeData",
            {"paimaiId": int(paimai_id), "priceReductionRecordStart": 0, "priceReductionRecordEnd": 20},
            referer=referer,
        )
        try:
            product_basic = self.api("getProductBasicInfo", {"paimaiId": int(paimai_id)}, referer=referer)
        except JDAPIError as exc:
            logger.warning(
                "product_basic_api_failed",
                f"获取商品基础信息失败: {paimai_id}",
                paimai_id=paimai_id,
                error=str(exc),
            )
            product_basic = {"error": str(exc)}
        description = self.api("queryProductDescription", {"paimaiId": int(paimai_id), "source": 5}, referer=referer)
        notice = self.api("queryNotice", {"paimaiId": int(paimai_id)}, referer=referer)
        basic = ((core.get("data") or {}).get("basicData") or {})
        product_basic_data = (product_basic.get("data") or {}) if isinstance(product_basic, dict) and isinstance(product_basic.get("data"), dict) else {}
        album_id = first_non_blank(
            basic.get("albumId"),
            basic.get("albumID"),
            product_basic_data.get("albumId"),
            product_basic_data.get("albumID"),
            list_item.get("albumId"),
            list_item.get("albumID"),
        )
        if not album_id:
            try:
                album_info = self.api("queryAlbumInfo", {"paimaiId": int(paimai_id)}, referer=referer)
                album_data = (album_info.get("data") or {}) if isinstance(album_info, dict) and isinstance(album_info.get("data"), dict) else {}
                album_id = first_non_blank(album_data.get("albumId"), album_data.get("albumID"), album_data.get("id"))
            except JDAPIError as exc:
                logger.warning(
                    "album_info_api_failed",
                    f"鑾峰彇鐩稿唽淇℃伅澶辫触: {paimai_id}",
                    paimai_id=paimai_id,
                    error=str(exc),
                )
        announcement_body = {"paimaiId": int(paimai_id)}
        if album_id:
            try:
                announcement_body["albumId"] = int(album_id)
            except (TypeError, ValueError):
                announcement_body["albumId"] = album_id
        announcement = self.api("queryAnnouncement", announcement_body, referer=referer)
        attachments = self.api("queryAttachFilesForIntro", {"paimaiId": int(paimai_id), "custom": 9}, referer=referer)

        vendor = {}
        vendor_id = first_non_blank(basic.get("vendorId"), list_item.get("vendorId"))
        if vendor_id:
            try:
                vendor = self.api(
                    "queryVendorInfo",
                    {
                        "vendorId": vendor_id,
                        "publishSource": first_non_blank(basic.get("publishSource"), list_item.get("publishSource"), 9),
                        "paimaiId": int(paimai_id),
                        "orgId": first_non_blank(basic.get("orgId"), list_item.get("orgId"), ""),
                    },
                    referer=referer,
                )
            except JDAPIError as exc:
                logger.warning(
                    "vendor_api_failed",
                    f"获取处置方信息失败: {paimai_id}",
                    paimai_id=paimai_id,
                    error=str(exc),
                )
                vendor = {"error": str(exc)}

        logger.debug("fetch_detail_success", f"获取详情完成: {paimai_id}", paimai_id=paimai_id)
        return {
            "core": core,
            "realtime": realtime,
            "product_basic": product_basic,
            "description_html": description.get("data") if isinstance(description, dict) else "",
            "description_response": description,
            "notice_html": notice.get("data") if isinstance(notice, dict) else "",
            "notice_response": notice,
            "announcement_html": ((announcement.get("data") or {}).get("content") if isinstance(announcement.get("data"), dict) else announcement.get("data")) if isinstance(announcement, dict) else "",
            "announcement_response": announcement,
            "attachments": attachments.get("data") if isinstance(attachments, dict) else [],
            "attachments_response": attachments,
            "vendor": vendor,
        }


# ===== 字段提取逻辑（集成标准化和 AI 兜底） =====
def extract_extend_info(core: Dict[str, Any]) -> Dict[str, Any]:
    basic = ((core.get("data") or {}).get("basicData") or {})
    return parse_json_object(basic.get("extendInfoMap"))


def extract_media(core: Dict[str, Any]) -> List[Any]:
    data = core.get("data") or {}
    media = []
    for key in ("imageVideoArea", "imageVideoData", "imageData", "videoData"):
        value = data.get(key)
        if value:
            media.append({key: value})
    basic = data.get("basicData") or {}
    for key in ("image", "productImage", "paimaiImages", "imageList"):
        value = basic.get(key)
        if value:
            media.append({key: value})
    return media


def normalize_jd_media_url(value: Any) -> Optional[str]:
    path = compact_text(value)
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    clean = path.lstrip("/")
    if clean.startswith("jfs/"):
        return f"https://img30.360buyimg.com/popWaterMark/{clean}"
    return path


def collect_media_urls(value: Any) -> List[str]:
    urls: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {
                    "imagePath",
                    "imgPath",
                    "imageUrl",
                    "imgUrl",
                    "picUrl",
                    "pictureUrl",
                    "videoPath",
                    "videoUrl",
                }:
                    url = normalize_jd_media_url(item)
                    if url and url not in urls:
                        urls.append(url)
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return urls


def extract_media_urls(core: Dict[str, Any], attachments: Any = None) -> List[str]:
    media_sources: List[Any] = []
    media_sources.extend(extract_media(core))
    if isinstance(attachments, dict):
        media_sources.extend(attachments.get("media") or [])
    urls: List[str] = []
    for source in media_sources:
        for url in collect_media_urls(source):
            if url not in urls:
                urls.append(url)
    return urls


AREA_UNIT_RE = re.compile(r"((?:平方米|平方)|平米|㎡|(?:m2|m²)|(?:亩|公顷))")


def normalize_area_value(value: Any, source_text: Any = None) -> Any:
    text = compact_text(display_ai_value(value))
    if not text:
        return value
    if AREA_UNIT_RE.search(text):
        direct_match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*((?:平方米|平方)|平米|㎡|(?:m2|m²)|(?:亩|公顷))", text)
        if direct_match:
            number, unit = direct_match.groups()
            return f"{number}{unit}"
    source = compact_text(source_text) or ""
    number_match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not number_match or not source:
        return text
    wanted = number_match.group(0).replace(",", "")
    unit_pattern = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*((?:平方米|平方)|平米|㎡|(?:m2|m²)|(?:亩|公顷))")
    for match in unit_pattern.finditer(source):
        source_number, unit = match.groups()
        if source_number.replace(",", "") == wanted:
            return f"{text}{unit}"
    loose_unit = AREA_UNIT_RE.search(source)
    if loose_unit:
        return f"{wanted}{loose_unit.group(1)}"
    return text


def extract_assessment_text(text: Any) -> Tuple[Optional[str], Optional[str]]:
    clean = compact_text(text) or ""
    if "评估" not in clean and "市场价" not in clean and "市场价格" not in clean:
        return None, None
    patterns = (
        r"(评估(?:(?:价|价格)|价值)?\s*[:：]?\s*[¥￥]?\s*[\d,]+(?:\.\d+)?\s*(?:(?:亿元|万元)|(?:元|万)|亿)(?!\s*(?:(?:倍|折)|(?:成|次)|%|％)))",
        r"(评估(?:(?:价|价格)|价值)\s*[:：]\s*[¥￥]?\s*[\d,]+(?:\.\d+)?(?!\s*(?:(?:倍|折)|(?:成|次)|%|％)))",
        r"(评估基准日\s*[:：]?\s*\d{4}年\d{1,2}月\d{1,2}日[^。；;]{0,80})",
        r"(市场(?:(?:价|价格))\s*[:：]?\s*[¥￥]?\s*[\d,]+(?:\.\d+)?\s*(?:(?:亿元|万元)|(?:元|万)|亿)(?!\s*(?:(?:倍|折)|(?:成|次)|%|％)))",
        r"(市场(?:(?:价|价格))\s*[:：]\s*[¥￥]?\s*[\d,]+(?:\.\d+)?(?!\s*(?:(?:倍|折)|(?:成|次)|%|％)))",
    )
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            value = compact_text(match.group(1))
            if normalize_assessment_price_time(value, clean, require_source_assessment_signal=True):
                return value, value
    return None, None


def format_market_price(value: Any, display: Any = None) -> Optional[str]:
    display_text = compact_text(display)
    if display_text and not is_zero_like(display_text):
        return f"市场价：{display_text}"
    money_text = format_money(value)
    if money_text and not is_zero_like(money_text):
        return f"市场价：{money_text}"
    return None


def extract_use_term_text(text: Any) -> Tuple[Optional[str], Optional[str]]:
    clean = compact_text(text) or ""
    if not clean:
        return None, None
    patterns = (
        r"((?:土地)?使用(?:截止)?期限\s*[:：]?\s*[^。；;，,]{2,80}?(?:(?:止|日)|(?:年|月)|$))",
        r"((?:(?:房屋|土地))?使用权\s*[^。；;，,]{0,20}年[^。；;]{0,120}?(?:(?:止|日)))",
        r"((?:(?:出租|租赁)|承租)?(?:(?:期限|年限)|租期)\s*[:：]?\s*[^。；;，,]{1,80}?(?:(?:止|日)|(?:年|月)|$))",
    )
    for pattern in patterns:
        match = re.search(pattern, clean)
        if not match:
            continue
        excerpt = compact_text(match.group(1))
        if not excerpt:
            continue
        value = re.sub(r"^(?:土地)?使用(?:截止)?期限\s*[:：]?\s*", "", excerpt)
        value = re.sub(r"^(?:(?:房屋|土地))?使用权\s*", "", value)
        value = re.sub(r"^(?:(?:出租|租赁)|承租)?(?:(?:期限|年限)|租期)\s*[:：]?\s*", "", value)
        return compact_text(value), excerpt
    return None, None


FIELD_VALUE_BOUNDARY_RE = re.compile(
    r"\s+(?:"
    r"(?:名称|坐落)|(?:现状|出租面积)|(?:租赁面积|承租面积)|建筑面积|"
    r"(?:出租期限|租赁期限)|(?:承租期限|租期)|免租期|"
    r"(?:租赁底价|租金年递增率)|(?:租金支付方式|履约保证金)|"
    r"(?:允许从事行业|可从事行业)|(?:经营业态|准入业态)|"
    r"(?:起拍价|报名保证金)|(?:保证金|加价幅度)|(?:公告披露期|竞拍时间)|(?:拍卖时间|报名时间)|"
    r"(?:物业费收费标准|联系咨询)|(?:看样及签约咨询|报名及竞拍咨询)"
    r")(?:[（(][^）)]*[）)])?\s*[:：]"
)


def truncate_labeled_value(value: Any) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    section_boundary = re.search(r"\s+(?:\d+|[一二三四五六七八九十]+)[\.．、]\s*[\u4e00-\u9fff]{2,20}", text)
    if section_boundary:
        text = text[:section_boundary.start()]
    boundary = FIELD_VALUE_BOUNDARY_RE.search(text)
    if boundary:
        text = text[:boundary.start()]
    return compact_text(text.strip("。；;，, "))


def extract_labeled_text_value(text: Any, labels: Iterable[str], max_chars: int = 120) -> Tuple[Optional[str], Optional[str]]:
    clean = compact_text(text) or ""
    if not clean:
        return None, None
    for label in labels:
        pattern = rf"{re.escape(label)}(?:[（(][^）)]*[）)])?\s*[:：]\s*([^。；;\n]{{1,{max_chars}}})"
        match = re.search(pattern, clean)
        if not match:
            continue
        value = truncate_labeled_value(match.group(1))
        if value:
            return value, compact_text(match.group(0))
    return None, None


def normalize_special_field_value(field_key: str, value: Any, source_text: Any = None) -> Any:
    if field_key in SPECIAL_AREA_FIELD_KEYS:
        return normalize_area_value(value, source_text)
    if field_key == "use_term":
        text = compact_text(value)
        source = compact_text(source_text) or ""
        if text and re.fullmatch(r"\d+(?:\.\d+)?", text):
            if "年" in source:
                return f"{text}年"
            if "月" in source:
                return f"{text}个月"
        return text
    if field_key in {"assessment_time_value", "asset_valuation"}:
        normalized = normalize_assessment_price_time(value, source_text, require_source_assessment_signal=True)
        return normalized if normalized else value
    return value


def field_result_value(value: Any, source_type: str, source_path: str, excerpt: Optional[str] = None, method: str = "api", confidence: Optional[float] = None) -> Dict[str, Any]:
    """生成字段提取结果（支持动态置信度）"""
    if confidence is None:
        confidence = 0.95 if not is_blank(value) else 0.0
    return {
        "value": value,
        "status": "extracted" if not is_blank(value) else "missing_on_page",
        "method": method,
        "confidence": confidence,
        "source_payload_type": source_type,
        "source_path": source_path,
        "source_excerpt": excerpt or compact_text(value),
    }


def extract_contact(
    product_basic_data: Dict[str, Any],
    core: Dict[str, Any],
    vendor: Dict[str, Any],
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    candidates: List[str] = []
    sources: List[str] = []

    def add_candidate(value: Any, source: str) -> None:
        normalized = normalize_contact_info(value)
        if not normalized:
            return
        candidates.append(normalized)
        if source not in sources:
            sources.append(source)

    api_value = deep_find(core, ("contactPhone", "contactTel", "consultTel", "phone", "mobile", "telephone"))
    name = deep_find(core, ("contactName", "consultName", "linkMan", "contacts"))
    if api_value or name:
        add_candidate(" ".join(filter(None, [compact_text(name), compact_text(api_value)])), "detail")

    product_basic_sources = [
        product_basic_data,
        product_basic_data.get("judicatureBasicInfoResult") if isinstance(product_basic_data.get("judicatureBasicInfoResult"), dict) else {},
        product_basic_data.get("bankruptcyBasicInfoResult") if isinstance(product_basic_data.get("bankruptcyBasicInfoResult"), dict) else {},
    ]
    for source_data in product_basic_sources:
        if not isinstance(source_data, dict):
            continue
        source_name = deep_find(source_data, ("contactName", "consultName", "linkMan", "contacts", "managerName"))
        source_phone = deep_find(source_data, ("contactPhone", "contactTel", "consultTel", "consultPhone", "phone", "mobile", "telephone"))
        if source_name or source_phone:
            add_candidate(" ".join(filter(None, [compact_text(source_name), compact_text(source_phone)])), "product_basic")

    vendor_phone = deep_find(vendor, ("phone", "mobile", "telephone"))
    if vendor_phone:
        add_candidate(vendor_phone, "vendor")

    table_value, table_excerpt = find_by_alias(parsed, ("联系方式", "咨询电话", "联系电话", "联系人"))
    if table_value:
        add_candidate(table_value, "description")

    notice_contacts = extract_contact_lines(notice_parsed.text)
    if notice_contacts:
        add_candidate("；".join(notice_contacts), "notice")

    value = normalize_contact_info("；".join(candidates))
    if not value:
        return None, None

    if sources == ["notice"]:
        return value, field_result_value(value, "notice_html", "contact_lines", value, "html_text_regex")
    if sources == ["description"]:
        return value, field_result_value(value, "description_html", "html_text", table_excerpt, "html_table")
    if sources == ["vendor"]:
        return value, field_result_value(value, "vendor_json", "deep_find(phone)")
    if sources == ["detail"]:
        return value, field_result_value(value, "detail_json", "deep_find(contact*)")
    if sources == ["product_basic"]:
        return value, field_result_value(value, "product_basic_json", "product_basic.contact*")
    source_type_map = {
        "detail": "detail_json",
        "product_basic": "product_basic_json",
        "vendor": "vendor_json",
        "description": "description_html",
        "notice": "notice_html",
    }
    source_payload_type = "+".join(source_type_map[source] for source in sources)
    return value, field_result_value(value, source_payload_type, "merged_contacts", value, "multi_source", 0.95)


def extract_contact_lines(text: str) -> List[str]:
    contacts: List[str] = []
    for line in text.splitlines():
        clean = compact_text(line)
        if not clean:
            continue
        if len(clean) > 360:
            continue
        if any(word in clean for word in ("举报", "监督", "开户银行", "账号", "保证金归", "缴入法院指定账户")):
            continue
        has_phone = re.search(r"(?:0\d{2,4}-?\d{6,8}|1[3-9]\d{9})", clean)
        if not has_phone:
            continue
        if re.search(r"((?:咨询电话|联系电话)|(?:联系方式|联系人)|(?:法院咨询电话|京东平台咨询电话)|(?:中国东方咨询电话|电话)\d?|电话[一二]?|经理)", clean):
            entries = extract_contact_entries_from_line(clean)
            contacts.extend(entries or [clean])
    return list(dict.fromkeys(contacts))


DISPOSAL_PARTY_LABELS = ("处置单位", "处置方", "转让方", "委托人", "委托方")


def clean_disposal_party(value: Any) -> Optional[str]:
    text = compact_text(value)
    if not text:
        return None
    for separator in ("监督单位", "监管单位", "网址", "账号", "现公告", "进行公开", "将在", "将于"):
        if separator in text:
            text = text.split(separator, 1)[0]
    text = text.strip(" ：:，,、；;()（）[]【】")
    if not text or len(text) < 3 or len(text) > 80:
        return None
    if any(word in text for word in ("监督单位", "监管单位", "联系电话", "咨询电话", "保证金", "京东")):
        return None
    return text


def extract_explicit_disposal_party(*texts: Any) -> Tuple[Optional[str], Optional[str]]:
    combined = "\n".join(compact_text(text) or "" for text in texts if not is_blank(text))
    if not combined:
        return None, None
    patterns = [
        rf"(?:{'|'.join(DISPOSAL_PARTY_LABELS)})\s*[:：]\s*([^，。；;\n（）()]{{3,90}})",
        rf"（\s*(?:{'|'.join(DISPOSAL_PARTY_LABELS)})\s*[:：]\s*([^，。；;\n（）()]{{3,90}})",
        r"([\u4e00-\u9fffA-Za-z0-9（）()·\-\s]{4,90}管理人)\s*将于\s*\d{4}\s*年",
        r"受委托[，,]\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-\s]{3,90})\s*将于\s*\d{4}\s*年",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined)
        if not match:
            continue
        party = clean_disposal_party(match.group(1))
        if party:
            return party, match.group(0)
    return None, None


def extract_disposal_party(
    product_basic_data: Dict[str, Any],
    basic: Dict[str, Any],
    vendor: Dict[str, Any],
    list_item: Dict[str, Any],
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
) -> Tuple[Optional[str], Dict[str, Any]]:
    explicit, excerpt = extract_explicit_disposal_party(notice_parsed.text, parsed.text)
    if explicit:
        return explicit, field_result_value(
            explicit,
            "notice_html",
            "explicit_disposal_party",
            excerpt or explicit,
            "html_text_regex",
            0.95,
        )
    upload_org = clean_disposal_party(
        first_non_blank(
            product_basic_data.get("uploadOrganization"),
            product_basic_data.get("uploadOrganizationName"),
            product_basic_data.get("disposalOrganization"),
            product_basic_data.get("serviceOrganization"),
        )
    )
    if upload_org:
        return upload_org, field_result_value(
            upload_org,
            "product_basic_json",
            "product_basic.uploadOrganization",
            upload_org,
            "api",
            0.95,
        )
    fallback = first_non_blank(
        deep_find(vendor, ("orgName",)),
        basic.get("shopName"),
        list_item.get("shopName"),
    )
    return fallback, field_result_value(fallback, "detail_json", "vendor.orgName/basicData.shopName")


# ===== 共有字段提取（集成 AI 兜底 + 标准化） =====
def extract_common_values(
    *,
    category: JDCategory,
    asset_group: str,
    list_item: Dict[str, Any],
    bundle: Dict[str, Any],
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    paimai_id: str,
    preloaded_ai_results: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """提取共有字段（集成标准化和 AI 兜底）"""
    core = bundle["core"]
    realtime = bundle["realtime"]
    vendor = bundle.get("vendor") or {}
    product_basic = bundle.get("product_basic") or {}
    product_basic_data = product_basic.get("data") if isinstance(product_basic, dict) else {}
    if not isinstance(product_basic_data, dict):
        product_basic_data = {}
    data = core.get("data") or {}
    basic = data.get("basicData") or {}
    realtime_data = realtime.get("data") or {}
    media = extract_media(core)
    attachments = {"files": bundle.get("attachments") or [], "media": media}

    values: Dict[str, Any] = {}
    results: Dict[str, Dict[str, Any]] = {}

    def set_value(
        field_key: str, field_label: str, value: Any, source_type: str, source_path: str,
        excerpt: Optional[str] = None, method: str = "api", confidence: Optional[float] = None,
    ) -> None:
        """设置字段值（失败时调用 AI 兜底）"""
        if not is_blank(value):
            # ===== Phase 2：字段标准化 =====
            if field_key == "assessment_price_time":
                values[field_key] = compact_text(value)
            elif any(k in field_key for k in ["price", "amount", "money"]):
                standardized = standardizer.money(value)
                if standardized.numeric is not None:
                    values[field_key] = standardized.display
                else:
                    values[field_key] = value
            elif field_key in TIME_FIELD_KEYS:
                values[field_key] = normalize_time_field(field_key, value, excerpt)
            elif "date" in field_key:
                standardized = standardizer.date(value)
                if standardized.iso_date is not None:
                    values[field_key] = standardized.display
                else:
                    values[field_key] = value
            else:
                values[field_key] = value

            results[field_key] = field_result_value(value, source_type, source_path, excerpt, method, confidence)
            logger.debug(
                "field_extracted",
                f"字段提取成功: {field_label}",
                field_key=field_key,
                field_label=field_label,
                paimai_id=paimai_id,
                source=source_type,
                method=method,
            )
        else:
            results[field_key] = field_result_value(
                None, source_type, source_path, excerpt, "not_found", 0.0
            )

    # 字段提取（原有逻辑不变，set_value 新增标准化和 AI 兜底）
    set_value("asset_type", "标的类型", ASSET_GROUP_LABELS[asset_group], "category", category.category_id)
    set_value(
        "asset_location",
        "标的所在地",
        first_non_blank(
            join_address(basic.get("productAddressResult")),
            join_address(extract_extend_info(core).get("claimAddress")),
            list_item.get("address"),
            list_item.get("productAddress"),
            "".join(
                compact_text(list_item.get(key)) or ""
                for key in ("province", "city", "county")
            ),
        ),
        "list_json",
        "productAddress/province/city/county",
    )

    raw_signup_start_value = format_time(deep_find(core, ("applyStartTime", "signupStartTime", "signUpStartTime", "enrollStartTime")))
    raw_signup_end_value = format_time(deep_find(core, ("applyEndTime", "signupEndTime", "signUpEndTime", "enrollEndTime")))
    text_start_value, text_end_value, text_time_excerpt = extract_auction_time_range_text(
        notice_parsed.text,
        parsed.text,
    )
    auction_start_value = format_time(first_non_blank(
        deep_find(core, ("auctionStartTime", "bidStartTime", "paimaiStartTime", "startTime")),
        product_basic_data.get("startTime"),
    ))
    auction_end_value = format_time(first_non_blank(
        deep_find(core, ("auctionEndTime", "bidEndTime", "paimaiEndTime", "endTime")),
        product_basic_data.get("endTime"),
    ))
    signup_start_value = first_non_blank(raw_signup_start_value, text_start_value, auction_start_value)
    signup_end_value = first_non_blank(raw_signup_end_value, text_end_value, auction_end_value)
    signup_start_source_type = "detail_json"
    signup_start_source_path = "deep_find(signup start)"
    signup_start_excerpt = None
    signup_end_source_type = "detail_json"
    signup_end_source_path = "deep_find(signup end)"
    signup_end_excerpt = None
    if is_blank(raw_signup_start_value) and text_start_value:
        signup_start_source_type = "notice_html"
        signup_start_source_path = "notice_html.auction_time_range"
        signup_start_excerpt = text_time_excerpt
    elif is_blank(raw_signup_start_value) and auction_start_value:
        signup_start_source_path = "deep_find(auction start)"
    if is_blank(raw_signup_end_value) and text_end_value:
        signup_end_source_type = "notice_html"
        signup_end_source_path = "notice_html.auction_time_range"
        signup_end_excerpt = text_time_excerpt
    elif is_blank(raw_signup_end_value) and auction_end_value:
        signup_end_source_path = "deep_find(auction end)"
    start_price_value = format_money(
        first_non_blank(basic.get("startPrice"), list_item.get("startPrice"), product_basic_data.get("startPrice")),
        first_non_blank(
            basic.get("startPriceStr"),
            list_item.get("startPriceStr"),
            list_item.get("startPriceCN"),
            product_basic_data.get("startPriceStr"),
        ),
    )
    final_price_value = format_money(
        first_non_blank(realtime_data.get("currentPrice"), list_item.get("currentPrice"), basic.get("currentPrice")),
        first_non_blank(realtime_data.get("currentPriceStr"), list_item.get("currentPriceStr"), list_item.get("currentPriceCN")),
    )

    status_code = first_non_blank(realtime_data.get("auctionStatus"), list_item.get("auctionStatus"), basic.get("auctionStatus"))
    active_realtime = realtime_indicates_active(realtime_data, auction_end_value)
    status_value = compute_project_status(
        auction_status_code=status_code,
        signup_start_time=signup_start_value,
        signup_end_time=signup_end_value,
        auction_start_time=auction_start_value,
        auction_end_time=auction_end_value,
        remain_time=realtime_data.get("remainTime"),
        realtime_active=active_realtime,
        start_price=start_price_value,
        final_price=final_price_value,
    )
    set_value("project_status", "项目状态", status_value, "computed", "time_and_price", method="time_and_price", confidence=0.9)

    stage_code = first_non_blank(list_item.get("paimaiTimes"), basic.get("paimaiTimes"), list_item.get("auctionType"), basic.get("auctionType"))
    stage_value = compute_auction_stage(
        stage_code,
        status_code,
        basic.get("title"),
        list_item.get("title"),
        parsed.text,
        notice_parsed.text,
    )
    set_value("auction_stage", "拍卖阶段", stage_value, "computed", "paimaiTimes/auctionStatus", method="computed", confidence=0.95)

    set_value("bid_records_json", "出价记录", safe_json_dumps(realtime_data.get("bidList") or []), "realtime_json", "data.bidList")
    set_value(
        "data_source",
        "数据来源",
        first_non_blank(basic.get("publishSourceName"), list_item.get("publishSourceName"), "京东拍卖"),
        "detail_json",
        "basicData.publishSourceName",
    )
    set_value("project_name", "项目名称", first_non_blank(basic.get("title"), list_item.get("title")), "detail_json", "basicData.title")
    set_value(
        "signup_start_time",
        "报名开始时间",
        signup_start_value,
        signup_start_source_type,
        signup_start_source_path,
        signup_start_excerpt,
    )
    set_value(
        "signup_end_time",
        "报名截止时间",
        signup_end_value,
        signup_end_source_type,
        signup_end_source_path,
        signup_end_excerpt,
    )
    disposal_value, disposal_result = extract_disposal_party(product_basic_data, basic, vendor, list_item, parsed, notice_parsed)
    if not is_blank(disposal_value):
        values["disposal_party"] = disposal_value
        results["disposal_party"] = disposal_result
    else:
        set_value(
            "disposal_party",
            "处置方",
            None,
            "detail_json",
            "vendor.orgName/basicData.shopName",
        )
    # 处置机构：优先从 extendInfoMap 提取，后备为处置方
    extend = extract_extend_info(core)
    agency_value = first_non_blank(
        compact_text(extend.get("agencyName")) if isinstance(extend, dict) else None,
        compact_text(extend.get("organizationName")) if isinstance(extend, dict) else None,
        compact_text(vendor.get("orgName")),
        compact_text(basic.get("shopName")),
    )
    set_value(
        "disposal_agency",
        "处置机构",
        agency_value,
        "detail_json",
        "extendInfoMap.agencyName/vendor.orgName",
    )
    set_value(
        "start_price_raw",
        "起拍价",
        start_price_value,
        "list_json",
        "startPrice/startPriceStr",
    )
    set_value(
        "final_price_raw",
        "最终价",
        final_price_value,
        "realtime_json",
        "data.currentPrice/currentPriceStr",
    )

    # 联系方式提取（已有逻辑）
    contact_value, contact_result = extract_contact(product_basic_data, core, vendor, parsed, notice_parsed)
    if contact_value and contact_result:
        values["contact_info"] = contact_value
        results["contact_info"] = contact_result

    # 特别告知
    special_aliases = (
        "特别告知",
        "特别提示",
        "特别提醒",
        "特别说明",
        "重要提示",
        "注意事项",
        "重大事项",
        "重大风险提示",
        "风险提示",
    )
    description_special, description_special_excerpt = find_by_alias(parsed, special_aliases)
    description_section, description_section_excerpt = extract_section_after_heading(
        parsed.text,
        SPECIAL_NOTICE_HEADINGS,
    )
    notice_section, notice_section_excerpt = extract_section_after_heading(
        notice_parsed.text,
        SPECIAL_NOTICE_HEADINGS,
    )
    risk_section, risk_section_excerpt = (None, None)
    if asset_group in {"land", "real_estate", "equipment", "vehicle", "goods", "usufruct"}:
        risk_section, risk_section_excerpt = extract_risk_notice_section(
            "\n".join([parsed.text, notice_parsed.text]),
            max_chars=1800,
        )
    for special_value, source_type, source_path, excerpt, method in (
        (
            description_special,
            "description_html",
            "html_table_or_text",
            description_special_excerpt,
            "html_table",
        ),
        (
            description_section,
            "description_html",
            "special_notice_section",
            description_section_excerpt,
            "html_text_regex",
        ),
        (
            notice_section,
            "notice_html",
            "special_notice_section",
            notice_section_excerpt,
            "html_text_regex",
        ),
        (
            risk_section,
            "notice_html",
            "risk_notice_section",
            risk_section_excerpt,
            "html_text_regex",
        ),
        (
            deep_find(core, ("specialNotice", "notice", "importantNotice")),
            "detail_json",
            "deep_find(specialNotice)",
            None,
            "api",
        ),
    ):
        special_value = meaningful_special_notice_value(special_value, source_path)
        if not is_blank(special_value):
            set_value("special_notice", "特别告知", special_value, source_type, source_path, excerpt or special_value, method)
            break

    # 评估价格：0 表示页面未提供，AI/正文只有看到“评估”上下文才使用。
    table_assessment, table_assessment_excerpt = find_by_alias(parsed, ("评估价格及时间", "评估价", "评估价格", "评估价值"))
    notice_assessment, notice_assessment_excerpt = find_by_alias(notice_parsed, ("评估价格及时间", "评估价", "评估价格", "评估价值"))
    text_assessment, text_assessment_excerpt = extract_assessment_text("\n".join([parsed.text, notice_parsed.text]))
    assessment_candidates = (
        (
            list_item.get("assessmentPriceCN"),
            "list_json",
            "assessmentPriceCN",
            None,
            True,
        ),
        (
            basic.get("assessmentPriceCN"),
            "detail_json",
            "basicData.assessmentPriceCN",
            None,
            True,
        ),
        (
            format_money(first_non_blank(list_item.get("assessmentPrice"), basic.get("assessmentPrice"))),
            "list_json",
            "assessmentPrice",
            None,
            True,
        ),
        (
            format_money(
                first_non_blank(product_basic_data.get("assessmentPrice"), product_basic_data.get("assessmentPriceCN")),
                first_non_blank(product_basic_data.get("assessmentPriceCN"), product_basic_data.get("assessmentPriceStr")),
            ),
            "product_basic_json",
            "product_basic.assessmentPrice/assessmentPriceCN",
            None,
            True,
        ),
        (
            format_market_price(
                first_non_blank(
                    list_item.get("marketPrice"),
                    basic.get("marketPrice"),
                    product_basic_data.get("marketPrice"),
                    ((basic.get("judicatureBasicInfoResult") or {}).get("marketPrice") if isinstance(basic.get("judicatureBasicInfoResult"), dict) else None),
                ),
                first_non_blank(
                    list_item.get("marketPriceCN"),
                    basic.get("marketPriceCN"),
                    product_basic_data.get("marketPriceCN"),
                ),
            ),
            "list_json",
            "marketPriceCN/marketPrice/basicData.judicatureBasicInfoResult.marketPrice",
            None,
            True,
        ),
        (
            table_assessment,
            "description_html",
            "html_table_or_text",
            table_assessment_excerpt,
            False,
        ),
        (
            notice_assessment,
            "notice_html",
            "html_table_or_text",
            notice_assessment_excerpt,
            False,
        ),
        (
            text_assessment,
            "notice_html",
            "text_regex",
            text_assessment_excerpt,
            False,
        ),
    )
    assessment_value = None
    assessment_source_type = "list_json"
    assessment_source_path = "assessmentPriceCN"
    assessment_excerpt = None
    for candidate, source_type, source_path, excerpt, structured_source in assessment_candidates:
        normalized_assessment = normalize_assessment_price_time(
            candidate,
            excerpt,
            structured_assessment_field=structured_source,
        )
        if normalized_assessment:
            assessment_value = normalized_assessment
            assessment_source_type = source_type
            assessment_source_path = source_path
            assessment_excerpt = excerpt
            break
    set_value(
        "assessment_price_time",
        "评估价格及时间",
        assessment_value,
        assessment_source_type,
        assessment_source_path,
        assessment_excerpt,
    )

    # 附件
    set_value(
        "attachments_json",
        "附件材料",
        safe_json_dumps(attachments),
        "attachments_json",
        "data",
    )

    apply_common_ai_batch(
        values,
        results,
        parsed=parsed,
        notice_parsed=notice_parsed,
        asset_group=asset_group,
        paimai_id=paimai_id,
        preloaded_ai_results=preloaded_ai_results,
    )
    adjust_project_status_by_time(values, results, active_realtime=active_realtime)

    return values, results


def structured_special_candidates(group: str, key: str, extend: Dict[str, Any], core: Dict[str, Any]) -> Tuple[Any, str] | Tuple[None, None]:
    """获取结构化的特有字段值"""
    basic = ((core.get("data") or {}).get("basicData") or {}) if isinstance(core, dict) else {}
    title = compact_text(basic.get("title"))
    if group == "debt":
        if key == "guarantee_method":
            value = first_non_blank(extend.get("claimGuaranteeTypeName"), extend.get("guaranteeMethod"))
            if isinstance(value, list):
                value = "、".join(compact_text(item) or "" for item in value)
            return value, "extendInfoMap.claimGuaranteeTypeName"
        if key == "collateral":
            value = join_address(extend.get("claimAddress"))
            return value, "extendInfoMap.claimAddress"
        if key == "principal_balance":
            value = first_non_blank(extend.get("principalBalance"), extend.get("claimsPrincipal"))
            return value, "extendInfoMap.principalBalance"
        if key == "debtor_name":
            value = first_non_blank(extend.get("debtorName"), extend.get("borrowerName"))
            return value, "extendInfoMap.debtorName"
        if key == "creditor":
            value = first_non_blank(extend.get("creditor"), deep_find(core, ("creditorName", "rightHolder")))
            return value, "extendInfoMap.creditor"
    if group == "ip" and title:
        ip_keywords = []
        for keyword in ("计算机软件著作权", "软件著作权", "作品著作权", "著作权", "专利权", "商标权", "版权", "使用许可"):
            if keyword not in title or keyword in ip_keywords:
                continue
            if any(keyword in existing for existing in ip_keywords):
                continue
            ip_keywords = [existing for existing in ip_keywords if existing not in keyword]
            ip_keywords.append(keyword)
        if key == "subject_name":
            return title, "basicData.title"
        if key == "right_holder":
            match = re.search(r"(.+?)名下", title)
            if match:
                return compact_text(match.group(1)), "basicData.title_regex"
        if key == "ip_type" and ip_keywords:
            top_types = []
            for keyword in ip_keywords:
                if "专利" in keyword and "专利权" not in top_types:
                    top_types.append("专利权")
                elif "商标" in keyword and "商标权" not in top_types:
                    top_types.append("商标权")
                elif "著作权" in keyword and "著作权" not in top_types:
                    top_types.append("著作权")
                elif keyword == "版权" and "版权" not in top_types:
                    top_types.append("版权")
                elif keyword == "使用许可" and "使用许可" not in top_types:
                    top_types.append("使用许可")
            return "、".join(top_types), "basicData.title_keyword"
        if key == "specific_category" and ip_keywords:
            return "、".join(ip_keywords), "basicData.title_keyword"
        if key == "subject_intro":
            return title, "basicData.title"
    return None, None


def parse_decimal_text(value: Any) -> Any:
    """解析小数字符串"""
    text = compact_text(value)
    if not text:
        return None
    text = text.replace(",", "").replace("，", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def format_decimal(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:,.2f}"


def extract_amount_unit(text: str) -> Optional[str]:
    """提取金额单位"""
    if "人民币元" in text or "元" in text:
        return "人民币元"
    if "万元" in text:
        return "万元"
    return None


def extract_benchmark_date_from_text(text: str) -> Optional[str]:
    """从文本中提取基准日"""
    match = re.search(r"基准日[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", text)
    if match:
        return match.group(1)
    match = re.search(r"截至\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", text)
    if match:
        return match.group(1)
    return None


def parse_debt_package_details(parsed: ParsedHTML) -> List[Dict[str, Any]]:
    """解析债权包明细表"""
    details: List[Dict[str, Any]] = []
    benchmark_date = extract_benchmark_date_from_text(parsed.text)
    amount_unit = extract_amount_unit(parsed.text) or "人民币元"
    active_header: List[str] | None = None

    def header_index(headers: List[str], keywords: Tuple[str, ...]) -> Optional[int]:
        for index, header in enumerate(headers):
            normalized = normalize_label(header)
            if any(keyword in normalized for keyword in keywords):
                return index
        return None

    def cell_at(cells: List[str], index: Optional[int], default: str = "") -> str:
        if index is None or index >= len(cells):
            return default
        return cells[index]

    def header_has_amount_columns(headers: List[str] | None) -> bool:
        if not headers:
            return False
        labels = [normalize_label(header) for header in headers]
        return any("本金余额" in label for label in labels) and any(
            ("债权合计" in label or "债权总额" in label or "合计金额" in label) for label in labels
        )

    for row in parsed.rows:
        cells = [cell for cell in row if not is_blank(cell)]
        if not cells:
            continue
        first = normalize_label(cells[0])
        if first in {"序号", "编号"}:
            active_header = cells
            continue
        if "本金余额" in cells or "债权合计" in cells:
            continue

        has_sequence = bool(re.fullmatch(r"\d+", first))
        if has_sequence and header_has_amount_columns(active_header) and len(cells) >= 5:
            subject = cell_at(active_header and cells, header_index(active_header, ("债务人", "借款人", "客户名称", "名称")), cells[1] if len(cells) > 1 else "")
            related = cell_at(active_header and cells, header_index(active_header, ("担保人", "保证人", "相关人")))
            collateral = cell_at(active_header and cells, header_index(active_header, ("担保物", "抵押物", "质押物", "担保方式")))
            principal = cell_at(active_header and cells, header_index(active_header, ("本金余额", "剩余本金", "接收时本金")))
            interest = cell_at(active_header and cells, header_index(active_header, ("利息余额", "利息金额", "欠息", "剩余利息")))
            fees = cell_at(active_header and cells, header_index(active_header, ("费用", "实现债权费用")))
            total = cell_at(active_header and cells, header_index(active_header, ("债权合计", "债权总额", "合计金额")))
        elif has_sequence and len(cells) >= 8:
            subject = cells[1]
            related = cells[2]
            collateral = cells[3]
            principal = cells[4]
            interest = cells[5]
            fees = cells[6]
            total = cells[7]
        elif len(cells) >= 7:
            subject = cells[0]
            related = cells[1]
            collateral = cells[2]
            principal = cells[3]
            interest = cells[4]
            fees = cells[5]
            total = cells[6]
        else:
            continue

        if not any(parse_decimal_text(v) is not None for v in (principal, interest, total)):
            continue

        details.append(
            {
                "sequence_no": first if has_sequence else None,
                "debtor_name": compact_text(subject),
                "debtor_or_asset": compact_text(subject),
                "guarantor": compact_text(related),
                "guarantor_or_related_party": compact_text(related),
                "collateral": compact_text(collateral),
                "principal_balance": compact_text(principal),
                "interest_balance": compact_text(interest),
                "recovery_fee": compact_text(fees),
                "claim_total": compact_text(total),
                "litigation_status": "",
                "benchmark_date": benchmark_date,
                "amount_unit": amount_unit,
            }
        )
    return details


def debt_detail_household_count(details: List[Dict[str, Any]]) -> Optional[str]:
    """用逐户明细推导户数，不把金额和户名压扁回主表。"""
    if not details:
        return None
    sequence_numbers = {
        compact_text(detail.get("sequence_no"))
        for detail in details
        if compact_text(detail.get("sequence_no"))
    }
    return str(len(sequence_numbers) or len(details))


def debt_detail_first_benchmark_date(details: List[Dict[str, Any]]) -> Optional[str]:
    for detail in details:
        value = compact_text(detail.get("benchmark_date"))
        if value:
            return value
    return None


def debt_detail_primary_debtor_names(details: List[Dict[str, Any]], *, max_names: int = 10) -> Optional[str]:
    """用逐户债权明细汇总主债务人名称，金额仍保留在明细表中。"""
    names: List[str] = []
    seen: set[str] = set()
    for detail in details:
        name = compact_text(detail.get("debtor_name") or detail.get("debtor_or_asset"))
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None
    if len(names) > max_names:
        return "；".join(names[:max_names]) + f"等{len(names)}户"
    return "；".join(names)


def extract_section_after_heading(text: str, headings: Tuple[str, ...], max_chars: int = 1200) -> Tuple[Optional[str], Optional[str]]:
    """提取标题后的内容片段"""
    lines = [compact_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if lines:
        start_index: Optional[int] = None
        for index, line in enumerate(lines):
            if any(heading in line for heading in headings):
                start_index = index
                break
        if start_index is not None:
            parts: List[str] = []
            for index in range(start_index, len(lines)):
                line = lines[index]
                if index > start_index and is_likely_next_section_heading(line):
                    break
                parts.append(line)
                if len("\n".join(parts)) >= max_chars:
                    break
            excerpt = compact_text("\n".join(parts))[:max_chars]
            if excerpt:
                return excerpt, excerpt

    for heading in headings:
        idx = text.find(heading)
        if idx == -1:
            continue
        tail = text[idx:]
        next_match = re.search(
            r"\n[一二三四五六七八九十]{1,3}[、.．]\s*[\u4e00-\u9fff]{2,20}",
            tail[len(heading):],
        )
        end = len(heading) + next_match.start() if next_match else min(len(tail), max_chars)
        excerpt = compact_text(tail[:end])
        return excerpt, excerpt
    return None, None


def is_likely_next_section_heading(line: str) -> bool:
    clean = compact_text(line) or ""
    if len(clean) > 60:
        return False
    if not re.match(r"^[一二三四五六七八九十]{1,3}[、.．]\s*[\u4e00-\u9fff]", clean):
        return False
    return not any(heading in clean for heading in SPECIAL_NOTICE_HEADINGS)


def extract_risk_notice_section(text: str, max_chars: int = 1600) -> Tuple[Optional[str], Optional[str]]:
    """Extract auction risk/defect notice even when the page does not use a fixed heading."""
    lines = [compact_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None, None

    heading_aliases = (
        "\u5176\u4ed6\u8bf4\u660e",  # 其他说明
        "\u98ce\u9669\u63d0\u793a",  # 风险提示
        "\u7455\u75b5\u8bf4\u660e",  # 瑕疵说明
    )
    risk_tokens = (
        "\u7455\u75b5",  # 瑕疵
        "\u98ce\u9669",  # 风险
        "\u4e0d\u6784\u6210\u5bf9\u6807\u7684\u7269\u7684\u4efb\u4f55\u62c5\u4fdd",  # 不构成对标的物的任何担保
        "\u4e0d\u6784\u6210\u62c5\u4fdd",  # 不构成担保
    )
    context_tokens = (
        "\u672c\u6b21\u7ade\u4ef7",  # 本次竞价
        "\u672c\u6b21\u5904\u7f6e",  # 本次处置
        "\u6807\u7684\u7269",  # 标的物
        "\u8d44\u4ea7\u8f6c\u8ba9",  # 资产转让
    )

    start_index: Optional[int] = None
    for index, line in enumerate(lines):
        if any(alias in line for alias in heading_aliases):
            start_index = index
            break
        has_risk = any(token in line for token in risk_tokens)
        has_context = any(token in line for token in context_tokens)
        if has_risk and has_context:
            start_index = index
            break

    if start_index is None:
        return None, None

    parts: List[str] = []
    for index in range(start_index, len(lines)):
        line = lines[index]
        if index > start_index and re.match(r"^[\u4e00-\u9fff\d]{1,3}[、.．]\s*", line):
            break
        parts.append(line)
        if len("\n".join(parts)) >= max_chars:
            break

    value = compact_text("\n".join(parts))[:max_chars]
    if not value:
        return None, None
    return value, value


def extract_special_values(
    *,
    asset_group: str,
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    core: Dict[str, Any],
    paimai_id: str,
    attachments: Any = None,
    preloaded_ai_results: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """提取特有字段值（集成 AI 兜底和标准化）"""
    values: Dict[str, Any] = {}
    results: Dict[str, Dict[str, Any]] = {}
    extend = extract_extend_info(core)
    basic_data = ((core.get("data") or {}).get("basicData") or {}) if isinstance(core, dict) else {}
    project_title = compact_text(basic_data.get("title"))
    debt_details: List[Dict[str, Any]] = []
    ip_details: List[Dict[str, Any]] = []

    def apply_media_images() -> None:
        field_key = MEDIA_SPECIAL_FIELD_BY_GROUP.get(asset_group)
        if not field_key or not is_blank(values.get(field_key)):
            return
        urls = extract_media_urls(core, attachments)
        if not urls:
            return
        value = "；".join(urls)
        values[field_key] = value
        results[field_key] = field_result_value(
            value,
            "detail_json",
            "imageVideoArea",
            value,
            "api",
            0.95,
        )

    def apply_land_assessment_fallback() -> None:
        if asset_group != "land" or not is_blank(values.get("assessment_time_value")):
            return
        value, excerpt = extract_assessment_text("\n".join([parsed.text, notice_parsed.text]))
        if not value:
            return
        values["assessment_time_value"] = value
        results["assessment_time_value"] = field_result_value(
            value,
            "notice_html",
            "text_regex",
            excerpt,
            "html_text_regex",
            0.88,
        )

    def apply_use_term_fallback() -> None:
        if asset_group not in {"land", "real_estate"} or not is_blank(values.get("use_term")):
            return
        value, excerpt = extract_use_term_text("\n".join([parsed.text, notice_parsed.text]))
        if not value:
            return
        values["use_term"] = value
        results["use_term"] = field_result_value(
            value,
            "notice_html",
            "text.use_term",
            excerpt,
            "html_text_regex",
            0.88,
        )

    def apply_real_estate_rental_fallback() -> None:
        if asset_group != "real_estate":
            return
        text = "\n".join([parsed.text, notice_parsed.text])
        def find_rental_value(labels: Tuple[str, ...]) -> Tuple[Optional[str], Optional[str]]:
            value, excerpt = find_by_alias(notice_parsed, labels)
            if is_blank(value):
                value, excerpt = find_by_alias(parsed, labels)
            if is_blank(value):
                value, excerpt = extract_labeled_text_value(text, labels)
            return value, excerpt

        if is_blank(values.get("building_area")):
            value, excerpt = find_rental_value(("出租面积", "租赁面积", "承租面积"))
            if value:
                value = normalize_special_field_value("building_area", value, excerpt)
                values["building_area"] = value
                results["building_area"] = field_result_value(
                    value,
                    "notice_html",
                    "text.rental_area",
                    excerpt,
                    "html_text_regex",
                    0.88,
                )
        if is_blank(values.get("property_status")):
            value, excerpt = find_rental_value(("现状", "使用状态"))
            if value:
                values["property_status"] = value
                results["property_status"] = field_result_value(
                    value,
                    "notice_html",
                    "text.property_status",
                    excerpt,
                    "html_text_regex",
                    0.88,
                )
        if is_blank(values.get("use_term")):
            value, excerpt = find_rental_value(("出租期限", "租赁期限", "承租期限", "租期"))
            if value:
                value = normalize_special_field_value("use_term", value, excerpt)
                values["use_term"] = value
                results["use_term"] = field_result_value(
                    value,
                    "notice_html",
                    "text.rental_term",
                    excerpt,
                    "html_text_regex",
                    0.88,
                )
        if is_blank(values.get("property_use")):
            value, excerpt = find_rental_value(("允许从事行业", "可从事行业", "经营业态", "准入业态"))
            if value:
                values["property_use"] = value
                results["property_use"] = field_result_value(
                    value,
                    "notice_html",
                    "text.allowed_business",
                    excerpt,
                    "html_text_regex",
                    0.88,
                )

    def apply_real_estate_extend() -> None:
        if asset_group not in {"real_estate", "land"}:
            return
        if not isinstance(extend, dict):
            return
        # 从 extendInfoMap 中提取 real_estate/land 特有字段
        _EXTEND_FIELD_MAP: Dict[str, Tuple[str, Tuple[str, ...]]] = {
            "right_certificate_no": ("rightCertificateNo", ("rightCertificateNo", "rightCertificate", "certificateNo")),
            "building_area": ("buildingArea", ("buildingArea", "buildingAreaSqm", "area")),
            "property_use": ("propertyUse", ("propertyUse", "houseUse", "landUse", "useage")),
            "property_location": ("propertyLocation", ("propertyLocation", "location", "address")),
            "property_structure": ("propertyStructure", ("propertyStructure", "buildingStructure", "structure")),
            "property_status": ("propertyStatus", ("propertyStatus", "status", "houseStatus")),
            "property_type": ("propertyType", ("propertyType", "houseType", "type")),
            "asset_highlights": ("assetHighlights", ("assetHighlights", "highlights", "advantage")),
            "use_term": ("useTerm", ("useTerm", "termOfUse", "validTerm")),
            "disclosed_defects": ("disclosedDefects", ("disclosedDefects", "defects", "riskWarning")),
            "land_area": ("landArea", ("landArea", "area")),
            "land_use": ("landUse", ("landUse", "useage")),
            "land_location": ("landLocation", ("landLocation", "location", "address")),
        }
        for field_key, (primary_key, fallback_keys) in _EXTEND_FIELD_MAP.items():
            if not is_blank(values.get(field_key)):
                continue
            val = first_non_blank(
                *(extend.get(k) for k in (primary_key,) + fallback_keys)
            )
            if is_blank(val):
                continue
            val_str = compact_text(str(val))
            if not val_str:
                continue
            values[field_key] = val_str
            results[field_key] = field_result_value(
                val_str,
                "detail_json",
                f"extendInfoMap.{primary_key}",
                val_str,
                "api",
                0.92,
            )

    def apply_debt_debtor_summary(
        *,
        source_type: str,
        source_path: str,
        source_text: Any = None,
        method: str = "derived",
        confidence: float = 0.95,
        override: bool = False,
    ) -> None:
        if asset_group != "debt" or not debt_details:
            return
        if not override and not is_blank(values.get("debtor_name")):
            return
        debtor_names = debt_detail_primary_debtor_names(debt_details)
        if not debtor_names:
            return
        values["debtor_name"] = debtor_names
        results["debtor_name"] = field_result_value(
            debtor_names,
            source_type,
            source_path,
            source_text,
            method,
            confidence,
        )

    def apply_ip_summary_fallback() -> None:
        nonlocal ip_details
        if asset_group != "ip":
            return
        title = project_title
        text_parts: List[str] = []
        seen_parts: set[str] = set()
        for part in (
            title,
            compact_text(values.get("subject_name")),
            compact_text(parsed.text),
            compact_text(notice_parsed.text),
        ):
            if not part or part in seen_parts:
                continue
            seen_parts.add(part)
            text_parts.append(part)
        source_text = "\n".join(text_parts)
        count, summary_details = extract_ip_summary_details_from_text(source_text)
        if count and is_blank(values.get("ip_count")):
            values["ip_count"] = count
            results["ip_count"] = field_result_value(
                count,
                "detail_json" if title else "description_html",
                "basicData.title_count_regex" if title else "text.ip_count_regex",
                source_text[:500],
                "title_regex",
                0.9,
            )
        if summary_details and not ip_details:
            ip_details = summary_details

    def apply_ip_image_detail_fallback() -> None:
        nonlocal ip_details
        if asset_group != "ip" or ai_extractor is None:
            return
        if not getattr(cfg.ai, "enable_vision_ai", False):
            logger.info(
                "ip_image_ai_skipped",
                "快速模式下跳过知识产权图片视觉提取",
                paimai_id=paimai_id,
            )
            return
        if ip_details and not ip_details_look_aggregated(ip_details):
            return
        image_urls: List[str] = []
        for parsed_part in (parsed, notice_parsed):
            for url in parsed_part.image_urls or []:
                if url not in image_urls:
                    image_urls.append(url)
        if not image_urls or not hasattr(ai_extractor, "extract_ip_details_from_images"):
            return
        context = build_ai_context(parsed, notice_parsed, asset_group, paimai_id, project_name=project_title)
        try:
            image_result = ai_extractor.extract_ip_details_from_images(image_urls, context)
        except Exception as exc:
            logger.warning(
                "ip_image_detail_extract_failed",
                f"图片表格知识产权明细提取失败: {exc}",
                paimai_id=paimai_id,
                error=str(exc),
            )
            return
        candidate_details = normalize_ai_ip_details(getattr(image_result, "value", None))
        if not candidate_details:
            return
        ip_details = candidate_details
        count = str(len(candidate_details))
        values["ip_count"] = count
        results["ip_count"] = field_result_value(
            count,
            "ai_extraction",
            "vision_ip_details_count",
            ai_result_source_text(image_result),
            "vision_ai",
            getattr(image_result, "confidence", 0.0),
        )

    def queue_ip_ocr_retry_if_needed(reason: str) -> None:
        if asset_group != "ip":
            return
        if ip_details and not ip_details_look_aggregated(ip_details):
            return
        image_urls: List[str] = []
        for url in extract_media_urls(core, attachments):
            if url not in image_urls:
                image_urls.append(url)
        for parsed_part in (parsed, notice_parsed):
            for url in parsed_part.image_urls or []:
                normalized = normalize_jd_media_url(url)
                if normalized and normalized not in image_urls:
                    image_urls.append(normalized)
        if not image_urls:
            return
        results["_ocr_retry_task"] = {
            "task_type": "ip_image_details",
            "reason": reason,
            "image_urls": image_urls[:50],
            "project_name": project_title,
        }

    apply_media_images()

    if asset_group == "other":
        source_text = parsed.text if not is_blank(parsed.text) else notice_parsed.text
        source_pairs = parsed.key_values if parsed.key_values else notice_parsed.key_values
        source_type = "description_html" if not is_blank(parsed.text) else "notice_html"
        source_path = "text" if source_type == "description_html" else "notice_text"
        values["raw_detail_text"] = source_text
        values["raw_table_pairs_json"] = safe_json_dumps(source_pairs)
        results["raw_detail_text"] = field_result_value(
            source_text, source_type, source_path, compact_text(source_text)[:300], "html_text"
        )
        results["raw_table_pairs_json"] = field_result_value(
            source_pairs, source_type, "table_pairs", method="html_table"
        )
        ai_results = preloaded_ai_results or ai_batch_extract_fields(
            special_ai_field_tuples(asset_group),
            parsed=parsed,
            notice_parsed=notice_parsed,
            asset_group=asset_group,
            paimai_id=paimai_id,
            project_name=project_title,
        )
        ai_result = ai_results.get("extracted_summary") if ai_results else None
        if ai_result and not is_ai_blank(getattr(ai_result, "value", None)):
            value = display_ai_value(getattr(ai_result, "value", None))
            values["extracted_summary"] = value
            results["extracted_summary"] = field_result_value(
                value,
                "ai_extraction",
                "llm_batch",
                ai_result_source_text(ai_result),
                "ai",
                getattr(ai_result, "confidence", 0.0),
            )
        else:
            results["extracted_summary"] = field_result_value(
                None, "missing", "ai_batch_no_value", None, "not_found", 0.0
            )
        return values, results, debt_details

    if ai_extractor is not None:
        for field in SPECIAL_FIELDS[asset_group]:
            structured_value, structured_path = structured_special_candidates(
                asset_group, field.key, extend, core
            )
            if not is_blank(structured_value) and is_blank(values.get(field.key)):
                values[field.key] = structured_value
                results[field.key] = field_result_value(
                    structured_value, "detail_json", structured_path or "extendInfoMap"
                )

        ai_fields = [ai_field_tuple(field) for field in SPECIAL_FIELDS[asset_group]]
        if asset_group == "debt":
            ai_fields.append(
                (
                    "debt_package_details_json",
                    "债权资产包明细",
                    "从表格或正文中逐户提取债权明细，value 必须是 JSON 数组。每一户/每一笔债权单独一行，字段包括 sequence_no、debtor_name、guarantor、collateral、principal_balance、interest_balance、recovery_fee、claim_total、litigation_status、benchmark_date、amount_unit、source_excerpt。不要把表头、合计行、单位说明、风险提示或特别提示当成明细行；不要返回合计行；金额保留页面原文单位，后续由程序标准化。",
                )
            )
        if asset_group == "ip":
            ip_definition = FIELD_DEFINITIONS.get("ip_details", {})
            ai_fields.append(
                (
                    "ip_details",
                    ip_definition.get("label", "知产逐项明细"),
                    ip_definition.get(
                        "description",
                        "JSON数组，每项包含 sequence_no、ip_name、certificate_no、ip_type、application_date、patent_type、status、source_excerpt。逐条完整提取，不要拼接合并。没有某项信息则填 null。",
                    ),
                )
            )
        if preloaded_ai_results is None:
            ai_results = ai_batch_extract_fields(
                ai_fields,
                parsed=parsed,
                notice_parsed=notice_parsed,
                asset_group=asset_group,
                paimai_id=paimai_id,
                project_name=project_title,
            )
        else:
            wanted_keys = {field_key for field_key, _, _ in ai_fields}
            ai_results = {
                field_key: ai_result
                for field_key, ai_result in preloaded_ai_results.items()
                if field_key in wanted_keys
            }

        if asset_group == "debt":
            detail_result = ai_results.get("debt_package_details_json")
            if detail_result and not is_ai_blank(getattr(detail_result, "value", None)):
                debt_details = normalize_ai_debt_details(getattr(detail_result, "value", None))
                count = debt_detail_household_count(debt_details)
                if count and is_blank(values.get("household_count")):
                    values["household_count"] = count
                    results["household_count"] = field_result_value(
                        count,
                        "ai_extraction",
                        "llm_debt_details_count",
                        ai_result_source_text(detail_result),
                        "ai",
                        getattr(detail_result, "confidence", 0.0),
                    )
                benchmark = debt_detail_first_benchmark_date(debt_details)
                if benchmark and is_blank(values.get("benchmark_date")):
                    values["benchmark_date"] = benchmark
                    results["benchmark_date"] = field_result_value(
                        benchmark,
                        "ai_extraction",
                        "llm_debt_details_benchmark_date",
                        ai_result_source_text(detail_result),
                        "ai",
                        getattr(detail_result, "confidence", 0.0),
                    )
                apply_debt_debtor_summary(
                    source_type="ai_extraction",
                    source_path="llm_debt_details_debtor_name",
                    source_text=ai_result_source_text(detail_result),
                    method="ai",
                    confidence=getattr(detail_result, "confidence", 0.0),
                )

        if asset_group == "ip":
            detail_result = ai_results.get("ip_details")
            if detail_result and not is_ai_blank(getattr(detail_result, "value", None)):
                ip_details = normalize_ai_ip_details(getattr(detail_result, "value", None))
                if ip_details:
                    count = str(len(ip_details))
                    values["ip_count"] = count
                    results["ip_count"] = field_result_value(
                        count,
                        "ai_extraction",
                        "llm_ip_details_count",
                        ai_result_source_text(detail_result),
                        "ai",
                        getattr(detail_result, "confidence", 0.0),
                    )

        for field in SPECIAL_FIELDS[asset_group]:
            ai_result = ai_results.get(field.key)
            if field.key in MEDIA_SPECIAL_FIELD_BY_GROUP.values() and not is_blank(values.get(field.key)):
                continue
            if field.key == "ip_count" and not is_blank(values.get(field.key)):
                continue
            if ai_result and not is_ai_blank(getattr(ai_result, "value", None)):
                source_text = ai_result_source_text(ai_result)
                value = normalize_special_field_value(
                    field.key,
                    display_ai_value(getattr(ai_result, "value", None)),
                    source_text,
                )
                compare_values = {
                    **values,
                    "project_name": first_non_blank(
                        ((core.get("data") or {}).get("basicData") or {}).get("title"),
                        values.get("subject_name"),
                        values.get("goods_name"),
                    ),
                }
                if field.key == "right_holder" and right_holder_looks_like_asset_name(value, compare_values):
                    results[field.key] = field_result_value(
                        None,
                        "ai_extraction",
                        "llm_batch",
                        source_text,
                        "not_found",
                        0.0,
                    )
                    results[field.key]["missing_reason"] = "right_holder_looks_like_asset_name"
                    continue
                values[field.key] = value
                results[field.key] = field_result_value(
                    value,
                    "ai_extraction",
                    "llm_batch",
                    source_text or getattr(ai_result, "original_text", None),
                    "ai",
                    getattr(ai_result, "confidence", 0.0),
                )
                logger.info(
                    "batch_ai_special_field_applied",
                    f"批量 AI 提取已应用（特有字段）: {field.label}",
                    field_key=field.key,
                    paimai_id=paimai_id,
                    confidence=getattr(ai_result, "confidence", 0.0),
                )
            elif field.key not in results:
                results[field.key] = field_result_value(
                    None, "missing", "ai_batch_no_value", None, "not_found", 0.0
                )
        apply_real_estate_extend()
        apply_real_estate_rental_fallback()
        apply_use_term_fallback()
        apply_media_images()
        apply_land_assessment_fallback()
        apply_ip_image_detail_fallback()
        apply_ip_summary_fallback()
        queue_ip_ocr_retry_if_needed("ip_details_missing_or_aggregated_after_main_extraction")
        if asset_group == "debt" and attachments:
            attachment_details, attachment_texts = extract_debt_details_from_attachments(attachments, paimai_id)
            if attachment_texts:
                results["_attachment_texts"] = attachment_texts
            if attachment_details:
                debt_details = attachment_details
                attachment_excerpt = "；".join(
                    compact_text(detail.get("source_excerpt")) or "" for detail in attachment_details[:5]
                )
                count = debt_detail_household_count(debt_details)
                if count:
                    values["household_count"] = count
                    results["household_count"] = field_result_value(
                        count,
                        "ai_extraction",
                        "attachment_debt_details_count",
                        attachment_excerpt,
                        "ai",
                        0.91,
                    )
                benchmark = debt_detail_first_benchmark_date(debt_details)
                if benchmark and is_blank(values.get("benchmark_date")):
                    values["benchmark_date"] = benchmark
                    results["benchmark_date"] = field_result_value(
                        benchmark,
                        "ai_extraction",
                        "attachment_debt_details_benchmark_date",
                        attachment_excerpt,
                        "ai",
                        0.91,
                    )
                apply_debt_debtor_summary(
                    source_type="ai_extraction",
                    source_path="attachment_debt_details_debtor_name",
                    source_text=attachment_excerpt,
                    method="ai",
                    confidence=0.91,
                    override=True,
                )
        if asset_group == "debt" and debt_details:
            count = debt_detail_household_count(debt_details)
            if count and is_blank(values.get("household_count")):
                values["household_count"] = count
                results["household_count"] = field_result_value(
                    count, "derived", "from_debt_details_count", None, "derived", 1.0
                )
            apply_debt_debtor_summary(
                source_type="derived",
                source_path="from_debt_details_debtor_name",
                method="derived",
                confidence=1.0,
            )
        if asset_group == "ip":
            return values, results, ip_details
        return values, results, debt_details

    if asset_group == "debt":
        debt_details = parse_debt_package_details(parsed)
        count = debt_detail_household_count(debt_details)
        if count:
            values["household_count"] = count
            results["household_count"] = field_result_value(
                count, "description_html", "debt_package_table_count", None, "html_table"
            )
        benchmark = debt_detail_first_benchmark_date(debt_details)
        if benchmark:
            values["benchmark_date"] = benchmark
            results["benchmark_date"] = field_result_value(
                benchmark, "description_html", "debt_package_table_benchmark_date", benchmark, "html_table"
            )
        apply_debt_debtor_summary(
            source_type="derived",
            source_path="from_debt_details_debtor_name",
            method="derived",
            confidence=1.0,
        )

        # 瑕疵说明
        defect, defect_excerpt = extract_section_after_heading(
            parsed.text, ("特别提示", "特别说明", "特别告知"), max_chars=1600
        )
        if defect:
            values["disclosed_defects"] = defect
            results["disclosed_defects"] = field_result_value(
                defect, "description_html", "special_notice_section", defect_excerpt, "html_text"
            )

    # 遍历所有特有字段
    for field in SPECIAL_FIELDS[asset_group]:
        if field.key in values:
            continue

        aliases = (field.label, *field.aliases)
        structured_value, structured_path = structured_special_candidates(
            asset_group, field.key, extend, core
        )
        if not is_blank(structured_value):
            values[field.key] = structured_value
            results[field.key] = field_result_value(
                structured_value, "detail_json", structured_path or "extendInfoMap"
            )
            continue

        # 从 HTML 中查找
        value, excerpt = find_by_alias(parsed, aliases)
        if is_blank(value):
            value, excerpt = find_by_alias(notice_parsed, aliases)
        if not is_blank(value):
            source_type = "notice_html" if excerpt and excerpt in notice_parsed.text else "description_html"
            value = normalize_special_field_value(field.key, value, excerpt)
            values[field.key] = value
            results[field.key] = field_result_value(value, source_type, "html_table_or_text", excerpt, "html_table_or_text")
            continue

        # 债务资产包特殊规则
        if asset_group == "debt" and field.key == "benchmark_date":
            match = re.search(r"截至\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", parsed.text)
            if match:
                values[field.key] = match.group(1)
                results[field.key] = field_result_value(
                    match.group(1), "description_html", "text_regex", match.group(0), "html_text_regex"
                )
                continue
        if asset_group == "debt" and field.key == "creditor":
            notice_creditor = extract_creditor_from_notice(notice_parsed.text)
            if notice_creditor:
                values[field.key] = notice_creditor
                results[field.key] = field_result_value(
                    notice_creditor, "notice_html", "creditor_text_regex", notice_creditor, "html_text_regex"
                )
                continue

        # 最后尝试 AI 兜底
        if ai_extractor is not None:
            ai_value, ai_confidence = ai_extract_field(
                field_key=field.key,
                field_label=field.label,
                html_key_values=parsed.key_values,
                detail_text=parsed.text,
                notice_text=notice_parsed.text,
                asset_group=asset_group,
                paimai_id=paimai_id,
            )
            if not is_blank(ai_value):
                ai_value = normalize_special_field_value(
                    field.key,
                    ai_value,
                    parsed.text + "\n" + notice_parsed.text,
                )
                compare_values = {
                    **values,
                    "project_name": ((core.get("data") or {}).get("basicData") or {}).get("title"),
                }
                if field.key == "right_holder" and right_holder_looks_like_asset_name(ai_value, compare_values):
                    results[field.key] = field_result_value(
                        None,
                        "ai_extraction",
                        "llm_fallback",
                        None,
                        "not_found",
                        0.0,
                    )
                    results[field.key]["missing_reason"] = "right_holder_looks_like_asset_name"
                    continue
                values[field.key] = ai_value
                results[field.key] = field_result_value(
                    ai_value, "ai_extraction", "llm_fallback", None, "ai", ai_confidence
                )
                logger.info(
                    "ai_extraction_success_special",
                    f"AI 兜底提取成功（特有字段）: {field.label}",
                    field_key=field.key,
                    paimai_id=paimai_id,
                    confidence=ai_confidence,
                )

    apply_real_estate_extend()
    apply_real_estate_rental_fallback()
    apply_use_term_fallback()
    apply_media_images()
    apply_land_assessment_fallback()
    apply_ip_summary_fallback()
    queue_ip_ocr_retry_if_needed("ip_details_missing_or_aggregated_without_main_ai")
    return values, results, debt_details


def sync_common_special_values(
    asset_group: str,
    common_values: Dict[str, Any],
    common_results: Dict[str, Dict[str, Any]],
    special_values: Dict[str, Any],
    special_results: Dict[str, Dict[str, Any]],
) -> None:
    if asset_group != "land" or not is_blank(special_values.get("assessment_time_value")):
        return
    assessment = common_values.get("assessment_price_time")
    if is_blank(assessment):
        return
    special_values["assessment_time_value"] = assessment
    common_result = common_results.get("assessment_price_time") or {}
    special_results["assessment_time_value"] = field_result_value(
        assessment,
        common_result.get("source_payload_type", "common"),
        f"common.assessment_price_time/{common_result.get('source_path', '')}".rstrip("/"),
        common_result.get("source_excerpt") or compact_text(assessment),
        common_result.get("method", "api"),
        min(float(common_result.get("confidence", 0.9) or 0.9), 0.9),
    )


def format_time(value: Any) -> Optional[str]:
    """格式化时间"""
    if is_blank(value):
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        return compact_text(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return compact_text(value)
    if numeric <= 0:
        return None
    if numeric > 10_000_000_000:
        numeric = numeric / 1000
    return dt.datetime.fromtimestamp(numeric).strftime("%Y-%m-%d %H:%M:%S")


def format_money(value: Any, display: Any = None) -> Optional[str]:
    """格式化金额"""
    if not is_blank(display):
        return compact_text(display)
    if is_blank(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return compact_text(value)
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.2f}"


def deep_find(obj: Any, keys: Iterable[str]) -> Any:
    """深度查找字典中的值"""
    key_set = set(keys)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in key_set and not is_blank(value):
                return value
        for value in obj.values():
            found = deep_find(value, key_set)
            if not is_blank(found):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = deep_find(value, key_set)
            if not is_blank(found):
                return found
    return None


def join_address(value: Any) -> Optional[str]:
    """拼接地址"""
    def join_one(item: Dict[str, Any]) -> str:
        province = compact_text(item.get("province")) or ""
        city = compact_text(item.get("city")) or ""
        county = compact_text(item.get("county")) or ""
        address = compact_text(item.get("address")) or ""
        municipalities = {"北京", "北京 市", "上海", "上海市", "天津", "天津市", "重庆", "重庆市"}
        if province in municipalities:
            municipality = province if province.endswith("市") else province + "市"
            if city and city not in {province, municipality}:
                district_prefix = city + county
            else:
                district_prefix = county
            full_prefix = municipality + district_prefix
        else:
            city_prefix = province + city
            full_prefix = city_prefix + county
        for duplicate_prefix in (full_prefix, city + county, county, city):
            if duplicate_prefix and address.startswith(duplicate_prefix):
                address = address[len(duplicate_prefix):]
                break
        return full_prefix + address

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(join_one(item))
            else:
                parts.append(compact_text(item) or "")
        return compact_text("；".join(filter(None, parts)))
    if isinstance(value, dict):
        return compact_text(join_one(value))
    return compact_text(value)


class JDAuctionScraper:
    """京东拍卖数据采集器"""

    def __init__(self, db: Any, client: JDClient) -> None:
        self.db = db
        self.client = client

    def crawl_sample(
        self,
        *,
        per_category_limit: int,
        output_dir: Path,
        categories: set[str] | None = None,
        total_limit: Optional[int] = None,
        mode: str = "sample",
        ai_mode: str = "async",
    ) -> Dict[str, Any]:
        """采集样本数据"""
        self.db.init_schema()
        self.db.seed_field_catalog()
        mode = mode if mode in ("sample", "full", "incremental") else "sample"
        batch_id = self.db.start_batch(
            {
                "per_category_limit": per_category_limit,
                "categories": sorted(categories or []),
                "total_limit": total_limit,
                "mode": mode,
            }
        )
        logger.info("batch_started", f"开始采集批次: {batch_id}", batch_id=batch_id)
        seen: set[str] = set()
        category_counts: Dict[str, int] = {}
        errors: List[Dict[str, str]] = []
        skipped_existing = 0
        existing_ids: set[str] = set()
        known_fps: dict[str, str] = {}
        fingerprint_rows: list[dict[str, Any]] = []
        if mode == "incremental":
            query_existing = getattr(self.db, "query_existing_source_item_ids", None)
            query_fps = getattr(self.db, "query_list_fingerprints", None)
            existing_ids = set(query_existing("jd") or []) if callable(query_existing) else set()
            known_fps = dict(query_fps("jd") or {}) if callable(query_fps) else {}
        has_baseline = bool(known_fps)
        try:
            for category in self.client.get_categories():
                if total_limit is not None and len(seen) >= total_limit:
                    break
                if categories and category.category_id not in categories:
                    continue
                # 翻页采集：从第1页开始，直到无更多数据或达到每类上限
                cat_items: list[Dict[str, Any]] = []
                for page in range(1, 200):
                    page_items, total = self.client.search_items(category.category_id, page=page, page_size=per_category_limit)
                    if not page_items:
                        break
                    cat_items.extend(page_items)
                    category_counts[f"{category.category_id}-{category.name}"] = len(page_items)
                    logger.info(
                        "category_processing",
                        f"处理类目: {category.name} ({category.category_id}), "
                        f"第{page}页获{len(page_items)}条, 累计{len(cat_items)}条",
                        category_id=category.category_id,
                        category_name=category.name,
                        item_count=len(cat_items),
                    )
                    if total is not None and len(cat_items) >= total:
                        break
                    if page >= 5 and page_items and len(page_items) < per_category_limit:
                        break
                items = cat_items
                for list_item in items[:per_category_limit]:
                    if total_limit is not None and len(seen) >= total_limit:
                        break
                    paimai_id = compact_text(first_non_blank(list_item.get("id"), list_item.get("paimaiId")))
                    if not paimai_id or paimai_id in seen:
                        continue
                    seen.add(paimai_id)
                    list_fp = self._list_fingerprint(list_item)
                    fingerprint_rows.append(
                        {
                            "source_platform": "jd",
                            "source_item_id": paimai_id,
                            "fingerprint": list_fp,
                            "updated_at": now_text(),
                        }
                    )
                    if mode == "incremental" and paimai_id in existing_ids:
                        known_fp = known_fps.get(paimai_id)
                        unchanged = (known_fp is not None and known_fp == list_fp) or (
                            known_fp is None and not has_baseline
                        )
                        if unchanged:
                            skipped_existing += 1
                            try:
                                self.db.write_crawl_queue_item(
                                    batch_id=batch_id,
                                    source_platform="jd",
                                    source_item_id=paimai_id,
                                    project_name=first_non_blank(
                                        list_item.get("title"),
                                        list_item.get("name"),
                                        list_item.get("projectName"),
                                    ),
                                    status="skipped",
                                )
                            except Exception:
                                pass
                            continue
                    try:
                        self._crawl_one(batch_id, category, list_item, paimai_id, ai_mode=ai_mode)
                    except Exception as exc:  # noqa: BLE001 - 单条失败不影响整个批次
                        logger.error(
                            "item_crawl_failed",
                            f"采集失败: {paimai_id}",
                            paimai_id=paimai_id,
                            category_id=category.category_id,
                            error=str(exc),
                        )
                        errors.append({"paimai_id": paimai_id, "category": category.category_id, "error": str(exc)})
                        # 采集失败也写入标级队列, 供全量采集追溯与重跑
                        try:
                            self.db.write_crawl_queue_item(
                                batch_id=batch_id,
                                source_platform="jd",
                                source_item_id=paimai_id,
                                status="failed",
                                error_message=str(exc)[:2000],
                            )
                        except Exception:
                            pass
            upsert_fps = getattr(self.db, "upsert_list_fingerprints", None)
            if callable(upsert_fps):
                upsert_fps(fingerprint_rows)
            status = "success" if not errors else "partial_success"
            summary_payload = {
                "errors": errors[:20],
                "skipped_existing": skipped_existing,
                "mode": mode,
            }
            self.db.finish_batch(batch_id, status, safe_json_dumps(summary_payload))
            logger.info(
                "batch_finished",
                f"批次完成: {batch_id}, 成功 {len(seen) - len(errors)}, 失败 {len(errors)}",
                batch_id=batch_id,
                items_count=len(seen),
                errors_count=len(errors),
                status=status,
            )
        except Exception as exc:
            self.db.finish_batch(batch_id, "failed", str(exc))
            logger.error(
                "batch_failed",
                f"批次失败: {batch_id}",
                batch_id=batch_id,
                error=str(exc),
            )
            raise

        exports = self.db.export_csvs(output_dir)
        return {
            "batch_id": batch_id,
            "items_seen": len(seen),
            "skipped_existing": skipped_existing,
            "category_counts": category_counts,
            "errors": errors,
            "exports": {key: str(path) for key, path in exports.items()},
        }

    @staticmethod
    def _list_fingerprint(list_item: Dict[str, Any]) -> str:
        payload = {
            key: list_item.get(key)
            for key in (
                "id",
                "paimaiId",
                "title",
                "name",
                "projectName",
                "startPrice",
                "startPriceStr",
                "currentPrice",
                "currentPriceStr",
                "assessmentPriceCN",
                "auctionStatus",
                "auctionStartTime",
                "auctionEndTime",
                "productAddress",
                "paimaiTimes",
            )
            if key in list_item
        }
        text = json.dumps(payload or list_item, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _crawl_one(self, batch_id: str, category: JDCategory, list_item: Dict[str, Any],
                   paimai_id: str, ai_mode: str = "async") -> None:
        """采集单个标的"""
        logger.info("crawl_item_start", f"开始采集: {paimai_id}", paimai_id=paimai_id)
        async_ai = ai_mode == "async"
        if not async_ai:
            start_ai_batch_budget(paimai_id)
        asset_group = classify_category(category)
        bundle = self.client.fetch_detail_bundle(paimai_id, list_item)
        core = bundle.get("core") or {}
        basic_data = ((core.get("data") or {}).get("basicData") or {}) if isinstance(core, dict) else {}
        project_name = first_non_blank(
            basic_data.get("title"),
            list_item.get("title"),
            list_item.get("name"),
            list_item.get("projectName"),
        )
        resource_payload = build_resource_payload(bundle.get("attachments") or [], core)
        description_html = bundle.get("description_html") or ""
        notice_html = bundle.get("notice_html") or ""
        announcement_html = bundle.get("announcement_html") or ""
        parsed = extract_key_values_from_html(description_html)
        notice_parsed = extract_key_values_from_html("\n".join([notice_html, announcement_html]))
        if async_ai:
            combined_ai_results = {}
        else:
            combined_ai_results = prefetch_combined_ai_results(
                asset_group=asset_group,
                parsed=parsed,
                notice_parsed=notice_parsed,
                paimai_id=paimai_id,
                project_name=project_name,
            )

        self.db.upsert_raw_payloads(
            paimai_id=paimai_id,
            batch_id=batch_id,
            source_url=f"https://paimai.jd.com/{paimai_id}",
            list_json=list_item,
            detail_json=core,
            realtime_json=bundle.get("realtime") or {},
            description_html=description_html,
            product_basic_json=bundle.get("product_basic") or {},
            notice_html=notice_html,
            announcement_html=announcement_html,
            attachments_json=resource_payload,
            vendor_json=bundle.get("vendor") or {},
        )

        common_values, common_results = extract_common_values(
            category=category,
            asset_group=asset_group,
            list_item=list_item,
            bundle=bundle,
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id=paimai_id,
            preloaded_ai_results=combined_ai_results,
        )
        special_values, special_results, asset_details = extract_special_values(
            asset_group=asset_group,
            parsed=parsed,
            notice_parsed=notice_parsed,
            core=core,
            paimai_id=paimai_id,
            attachments=resource_payload,
            preloaded_ai_results=combined_ai_results,
        )
        attachment_texts = special_results.pop("_attachment_texts", None)
        if attachment_texts:
            self.db.update_attachment_texts(paimai_id, attachment_texts)
        ocr_retry_task = special_results.pop("_ocr_retry_task", None)

        sync_common_special_values(
            asset_group,
            common_values,
            common_results,
            special_values,
            special_results,
        )

        self.db.upsert_common_item(
            paimai_id=paimai_id,
            batch_id=batch_id,
            asset_group=asset_group,
            jd_category_id=category.category_id,
            jd_category_name=category.name,
            values=common_values,
            field_results=common_results,
            special_values=special_values,
        )
        self.db.upsert_special_item(
            paimai_id=paimai_id,
            asset_group=asset_group,
            values=special_values,
            field_results=special_results,
        )
        if asset_group == "debt":
            self.db.upsert_debt_details(paimai_id=paimai_id, details=asset_details)
        elif asset_group == "ip":
            self.db.upsert_ip_details(paimai_id=paimai_id, details=asset_details)
        if ocr_retry_task and hasattr(self.db, "enqueue_ocr_retry_task"):
            self.db.enqueue_ocr_retry_task(paimai_id=paimai_id, task=ocr_retry_task)

        # 异步 AI 模式：将标的入队到 ai_enrichment_queue，不做同步 AI
        if async_ai and hasattr(self.db, "enqueue_ai_enrichment_task"):
            from multi_platform_runner import ai_context_to_payload
            context = AIExtractionContext(
                html_key_values={str(k): str(v) for k, v in (parsed.key_values or {}).items() if v},
                detail_text=re.sub(r"<[^>]+>", " ", description_html or "")[:AI_DETAIL_TEXT_LIMIT],
                notice_text=re.sub(r"<[^>]+>", " ", (notice_html or "") + "\n" + (announcement_html or ""))[:30000],
                image_urls=[],
                asset_group=asset_group,
                paimai_id=paimai_id,
            )
            self.db.enqueue_ai_enrichment_task(
                paimai_id=paimai_id,
                source_platform="jd",
                source_item_id=paimai_id,
                asset_group=asset_group,
                context=ai_context_to_payload(context),
                task_type="field_enrichment",
                priority=100,
                reason="main crawl wrote item first; queued AI enrichment",
            )

        logger.info("crawl_item_success", f"采集完成: {paimai_id}", paimai_id=paimai_id)

        if not async_ai:
            clear_ai_batch_budget(paimai_id)


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="京东资产拍卖采集器 v2.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl = subparsers.add_parser("crawl", help="采集样本或正式数据")
    crawl.add_argument("--per-category-limit", type=int, default=2, help="每个京东一级类目最多采集多少条 (默认: 2)")
    crawl.add_argument("--output-dir", type=Path, default=Path("outputs") / "latest", help="输出目录 (默认: outputs/latest)")
    crawl.add_argument("--mysql-host", default="127.0.0.1", help="MySQL 主机 (默认: 127.0.0.1)")
    crawl.add_argument("--mysql-port", type=int, default=3306, help="MySQL 端口 (默认: 3306)")
    crawl.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", "root"), help="MySQL 用户 (默认: 环境变量 MYSQL_USER 或 root)")
    crawl.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", "root"), help="MySQL 密码 (默认: 环境变量 MYSQL_PASSWORD 或 root)")
    crawl.add_argument("--mysql-database", default=os.getenv("MYSQL_DATABASE", "auction_data"), help="MySQL 数据库 (默认: 环境变量 MYSQL_DATABASE 或 auction_data)")
    crawl.add_argument("--reset-db", action="store_true", help="采集前删除并重建当前 MySQL 正式表；仅用于测试环境")
    crawl.add_argument("--confirm-reset-db", action="store_true", help="与 --reset-db 同时使用，确认执行删除重建表")
    crawl.add_argument("--categories", default="", help="只采集指定一级类目 ID，逗号分隔")
    crawl.add_argument("--throttle", type=float, default=cfg.crawl.default_throttle, help=f"请求间隔秒数 (默认: {cfg.crawl.default_throttle})")
    # 日志配置参数
    crawl.add_argument("--log-level", default=cfg.log.log_level, help="日志级别 (DEBUG, INFO, WARNING, ERROR) (默认: INFO)")
    crawl.add_argument("--log-file", type=Path, default=None, help="日志文件路径 (可选)")
    # AI 配置参数
    crawl.add_argument("--ai-model", default="", help="AI 提取模型 (deepseek, openai, qwen)")
    crawl.add_argument("--ai-profile", default="", help="AI profile name from .env or MySQL ai_model_profiles")
    crawl.add_argument("--ai-provider", default="", help="AI provider override: qwen/deepseek/openai")
    crawl.add_argument("--ai-model-name", default="", help="Concrete AI model name, e.g. deepseek-chat")
    crawl.add_argument("--ai-api-key", default="", help="AI API Key")
    crawl.add_argument("--ai-base-url", default="", help="AI API Base URL")
    crawl.add_argument("--ai-timeout", type=int, default=cfg.ai.timeout, help=f"AI 单次请求超时秒数 (默认: {cfg.ai.timeout})")
    crawl.add_argument("--ai-max-retries", type=int, default=cfg.ai.max_retries, help=f"AI 请求失败重试次数 (默认: {cfg.ai.max_retries})")
    crawl.add_argument("--ai-single-fallback", action="store_true", help="批量 AI 失败后启用逐字段兜底重试（质量模式，较慢）")
    crawl.add_argument("--enable-vision-ai", action="store_true", help="启用图片/OCR 视觉 AI 兜底（质量模式，较慢）")
    crawl.add_argument("--ai-circuit-breaker-failures", type=int, default=cfg.ai.circuit_breaker_failures, help=f"AI 连续失败熔断阈值 (默认: {cfg.ai.circuit_breaker_failures})")
    crawl.add_argument("--ai-circuit-breaker-cooldown", type=int, default=cfg.ai.circuit_breaker_cooldown_seconds, help=f"AI 熔断冷却秒数 (默认: {cfg.ai.circuit_breaker_cooldown_seconds})")
    crawl.add_argument("--ai-max-batches-per-item", type=int, default=cfg.ai.max_batches_per_item, help=f"每个标的最大批量 AI 请求次数，0=不限 (默认: {cfg.ai.max_batches_per_item})")
    return parser.parse_args()


def main() -> None:
    """主函数"""
    args = parse_args()
    if args.command == "crawl":
        # 更新配置
        cfg.log.log_level = args.log_level.upper()
        if args.log_file:
            cfg.log.log_file = args.log_file
        cfg.ai.timeout = args.ai_timeout
        cfg.ai.max_retries = args.ai_max_retries
        cfg.ai.enable_single_field_fallback = bool(args.ai_single_fallback)
        cfg.ai.enable_vision_ai = bool(args.enable_vision_ai)
        cfg.ai.max_batches_per_item = args.ai_max_batches_per_item
        cfg.ai.circuit_breaker_failures = args.ai_circuit_breaker_failures
        cfg.ai.circuit_breaker_cooldown_seconds = args.ai_circuit_breaker_cooldown
        # 重新初始化日志
        global logger
        logger = get_logger(cfg.log)
        sync_module_loggers(logger)
        # 初始化 AI 提取器（如果配置了）
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        categories = {part.strip() for part in args.categories.split(",") if part.strip()} or None
        storage_info: Dict[str, Any]
        from jd_mysql_store import MySQLConfig, MySQLJDScraperDatabase, reset_mysql_tables

        mysql_config = MySQLConfig(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_database,
        )
        if args.reset_db and not args.confirm_reset_db:
            raise SystemExit("--reset-db 会删除并重建 MySQL 表，请同时添加 --confirm-reset-db 确认")
        if args.reset_db:
            reset_mysql_tables(mysql_config)
        db = MySQLJDScraperDatabase(mysql_config)
        storage_info = {
            "storage_backend": "mysql",
            "mysql_host": args.mysql_host,
            "mysql_port": args.mysql_port,
            "mysql_database": args.mysql_database,
        }
        init_ai_extractor(
            model=args.ai_provider or args.ai_model,
            api_key=args.ai_api_key,
            base_url=args.ai_base_url,
            model_name=args.ai_model_name,
            profile=args.ai_profile,
            mysql_config=mysql_config,
        )
        client = JDClient(throttle_seconds=args.throttle, timeout=cfg.crawl.default_timeout)
        scraper = JDAuctionScraper(db, client)
        logger.info(
            "crawl_start",
            "开始采集",
            output_dir=str(output_dir),
            storage_backend=args.storage_backend,
            per_category_limit=args.per_category_limit,
            ai_enabled=ai_extractor is not None,
        )
        summary = scraper.crawl_sample(
            per_category_limit=args.per_category_limit,
            output_dir=output_dir,
            categories=categories,
        )
        print(safe_json_dumps({**storage_info, **summary}))
        logger.info(
            "crawl_finish",
            "采集完成",
            storage_backend=args.storage_backend,
            batch_id=summary.get("batch_id"),
            items_seen=summary.get("items_seen"),
            errors_count=len(summary.get("errors", [])),
        )


if __name__ == "__main__":
    main()
