from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests


JD_API = "https://api.m.jd.com/api"


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class JDCategory:
    category_id: str
    name: str


@dataclass
class ParsedHTML:
    key_values: dict[str, str]
    text: str
    rows: list[list[str]]


COMMON_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("asset_type", "标的类型", ("资产类型", "类别")),
    FieldDef("asset_location", "标的所在地", ("所在地", "项目所在地", "资产所在地")),
    FieldDef("project_status", "项目状态", ("拍卖状态", "当前状态", "交易状态")),
    FieldDef("auction_stage", "拍卖阶段", ("拍卖轮次", "阶段")),
    FieldDef("bid_records_json", "出价记录", ("竞价记录", "出价信息")),
    FieldDef("data_source", "数据来源", ("来源", "采集来源")),
    FieldDef("project_name", "项目名称", ("标的名称", "标题", "拍品名称")),
    FieldDef("signup_start_time", "报名开始时间", ("报名起始时间", "报名开始")),
    FieldDef("signup_end_time", "报名截止时间", ("报名结束时间", "报名截止")),
    FieldDef("disposal_party", "处置方", ("委托方", "处置机构", "拍卖机构", "机构名称")),
    FieldDef("start_price_raw", "起拍价", ("挂牌价", "初始价格", "转让底价")),
    FieldDef("final_price_raw", "最终价", ("当前价", "成交价", "最新价")),
    FieldDef("contact_info", "联系方式", ("联系人", "联系电话", "咨询电话", "联系方式")),
    FieldDef("special_notice", "特别告知", ("注意事项", "特别提醒", "瑕疵说明")),
    FieldDef("assessment_price_time", "评估价格及时间", ("评估价", "评估价格", "评估时间")),
    FieldDef("attachments_json", "附件材料", ("附件", "材料", "相关附件")),
)


SPECIAL_FIELDS: dict[str, tuple[FieldDef, ...]] = {
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
        FieldDef("building_area", "建筑面积", ("房屋建筑面积", "套内建筑面积", "面积")),
        FieldDef("property_use", "房产用途", ("规划用途", "用途")),
        FieldDef("use_term", "使用年限", ("使用期限", "土地使用期限")),
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
        FieldDef("debtor_name", "主债务人名称", ("主债务人", "借款人", "债务人", "客户名称")),
        FieldDef("principal_balance", "本金余额", ("本金余额", "剩余本金", "接收时本金", "本金金额", "贷款本金")),
        FieldDef("interest_balance", "利息余额", ("利息余额", "剩余利息", "待偿还利息", "欠息金额", "利息金额")),
        FieldDef("benchmark_date", "基准日", ("截至日", "截止日", "债权基准日")),
        FieldDef("disclosed_defects", "公示瑕疵", ("瑕疵", "风险提示", "特别提示", "特别说明", "特别告知")),
        FieldDef("guarantee_method", "担保方式", ("担保类型", "保证方式", "抵押顺位")),
        FieldDef("guarantors", "保证人", ("担保人", "保证方")),
        FieldDef("collateral", "抵质押物", ("抵押物", "质押物", "抵押资产")),
        FieldDef("litigation_status", "诉讼状态", ("诉讼进展", "执行情况")),
        FieldDef("creditor", "债权人", ("权利人", "转让方", "出让方", "委托方", "委托人", "中国东方")),
        FieldDef("household_count", "户数", ("债权笔数", "户")),
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
        FieldDef("certificate_no", "标的证号", ("专利号", "作品号", "证书号")),
        FieldDef("ip_type", "知产类型", ("知识产权类型", "类型")),
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
    ),
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


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def compact_text(value: Any) -> str | None:
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
    label = label.strip("：:；;，,。.【】[]（）() ")
    return label


def looks_like_label(text: str) -> bool:
    text = normalize_label(text)
    if not text:
        return False
    if re.search(r"\d{4}年|\d+[,.]?\d*|https?://", text):
        return False
    label_words = (
        "金额",
        "余额",
        "合计",
        "日期",
        "时间",
        "序号",
        "名称",
        "姓名",
        "方式",
        "状态",
        "类型",
        "面积",
        "用途",
        "位置",
        "权利",
        "担保",
        "债权",
        "债务",
        "资产",
        "借款人",
        "保证人",
        "抵押物",
        "质押物",
        "本金",
        "利息",
        "费用",
    )
    return any(word in text for word in label_words) or len(text) <= 6


def is_likely_key_value_row(cells: list[str]) -> bool:
    if len(cells) < 2 or len(cells) % 2 != 0:
        return False
    if normalize_label(cells[0]) in {"序号", "编号"}:
        return False
    keys = cells[0::2]
    values = cells[1::2]
    if not all(looks_like_label(key) for key in keys):
        return False
    if values and all(looks_like_label(value) for value in values):
        return False
    return True


def format_time(value: Any) -> str | None:
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


def format_money(value: Any, display: Any = None) -> str | None:
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


def join_address(value: Any) -> str | None:
    def join_one(item: dict[str, Any]) -> str:
        province = compact_text(item.get("province")) or ""
        city = compact_text(item.get("city")) or ""
        county = compact_text(item.get("county")) or ""
        address = compact_text(item.get("address")) or ""
        municipalities = {
            "\u5317\u4eac",
            "\u5317\u4eac\u5e02",
            "\u4e0a\u6d77",
            "\u4e0a\u6d77\u5e02",
            "\u5929\u6d25",
            "\u5929\u6d25\u5e02",
            "\u91cd\u5e86",
            "\u91cd\u5e86\u5e02",
        }
        if province in municipalities:
            municipality = province if province.endswith("\u5e02") else province + "\u5e02"
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
        return compact_text("\uff1b".join(filter(None, parts)))
    if isinstance(value, dict):
        return compact_text(join_one(value))
    return compact_text(value)


class KVHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
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


def extract_key_values_from_html(html_text: str | None) -> ParsedHTML:
    parser = KVHTMLParser()
    parser.feed(html_text or "")
    key_values: dict[str, str] = {}

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

    return ParsedHTML(key_values=key_values, text=parser.text, rows=parser.rows)


def classify_category(category: JDCategory) -> str:
    return CATEGORY_GROUPS.get(category.category_id, "other")


def find_by_alias(parsed: ParsedHTML, aliases: Iterable[str]) -> tuple[str | None, str | None]:
    normalized = {normalize_label(key): value for key, value in parsed.key_values.items()}
    for alias in aliases:
        key = normalize_label(alias)
        if key in normalized and not is_blank(normalized[key]):
            return normalized[key], f"{alias}：{normalized[key]}"

    for line in parsed.text.splitlines():
        for alias in aliases:
            pattern = rf"{re.escape(alias)}\s*[：:]\s*(.+?)(?:$|；|;)"
            match = re.search(pattern, line)
            if match:
                value = compact_text(match.group(1))
                if value:
                    return value, line[:300]
    return None, None


def parse_decimal_text(value: Any) -> Decimal | None:
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


def format_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:,.2f}"


def extract_amount_unit(text: str) -> str | None:
    if "人民币元" in text or "元" in text:
        return "人民币元"
    if "万元" in text:
        return "万元"
    return None


def extract_benchmark_date_from_text(text: str) -> str | None:
    match = re.search(r"基准日[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", text)
    if match:
        return match.group(1)
    match = re.search(r"截至\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", text)
    if match:
        return match.group(1)
    return None


def parse_debt_package_details(parsed: ParsedHTML) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    benchmark_date = extract_benchmark_date_from_text(parsed.text)
    amount_unit = extract_amount_unit(parsed.text) or "人民币元"
    current_sequence: str | None = None

    for row in parsed.rows:
        cells = [cell for cell in row if not is_blank(cell)]
        if not cells:
            continue
        first = normalize_label(cells[0])
        if first in {"序号", "编号"} or "本金余额" in cells or "债权合计" in cells:
            continue

        has_sequence = bool(re.fullmatch(r"\d+", first))
        if has_sequence and len(cells) >= 8:
            current_sequence = first
            subject = cells[1]
            related_party = cells[2]
            collateral = cells[3]
            principal = cells[4]
            interest = cells[5]
            fees = cells[6]
            total = cells[7]
        elif current_sequence and len(cells) >= 7:
            subject = cells[0]
            related_party = cells[1]
            collateral = cells[2]
            principal = cells[3]
            interest = cells[4]
            fees = cells[5]
            total = cells[6]
        else:
            continue

        if not any(parse_decimal_text(value) is not None for value in (principal, interest, total)):
            continue

        details.append(
            {
                "sequence_no": current_sequence,
                "debtor_or_asset": compact_text(subject),
                "guarantor_or_related_party": compact_text(related_party),
                "collateral": compact_text(collateral),
                "principal_balance": compact_text(principal),
                "interest_balance": compact_text(interest),
                "recovery_fee": compact_text(fees),
                "claim_total": compact_text(total),
                "benchmark_date": benchmark_date,
                "amount_unit": amount_unit,
                "is_continuation": 0 if has_sequence else 1,
            }
        )
    return details


def summarize_debt_details(details: list[dict[str, Any]]) -> dict[str, str]:
    if not details:
        return {}

    sequence_count = len({detail["sequence_no"] for detail in details if detail.get("sequence_no")})
    principal_total = sum((parse_decimal_text(detail.get("principal_balance")) or Decimal("0")) for detail in details)
    interest_total = sum((parse_decimal_text(detail.get("interest_balance")) or Decimal("0")) for detail in details)
    claim_total = sum((parse_decimal_text(detail.get("claim_total")) or Decimal("0")) for detail in details)
    debtors = []
    guarantors = []
    collaterals = []
    benchmark_dates = []
    units = []
    for detail in details:
        for bucket, key in (
            (debtors, "debtor_or_asset"),
            (guarantors, "guarantor_or_related_party"),
            (collaterals, "collateral"),
            (benchmark_dates, "benchmark_date"),
            (units, "amount_unit"),
        ):
            value = compact_text(detail.get(key))
            if value and value not in bucket:
                bucket.append(value)

    suffix = f"（明细{len(details)}行，{sequence_count}户）"
    unit = units[0] if units else ""
    return {
        "debtor_name": f"多户资产包（{sequence_count}户）：{'；'.join(debtors[:8])}",
        "principal_balance": f"合计 {format_decimal(principal_total)} {unit}{suffix}",
        "interest_balance": f"合计 {format_decimal(interest_total)} {unit}{suffix}",
        "household_count": str(sequence_count),
        "guarantors": "；".join(guarantors[:12]),
        "collateral": "；".join(collaterals[:12]),
        "benchmark_date": benchmark_dates[0] if benchmark_dates else "",
        "claim_total": f"合计 {format_decimal(claim_total)} {unit}{suffix}",
    }


def extract_section_after_heading(text: str, headings: tuple[str, ...], max_chars: int = 1200) -> tuple[str | None, str | None]:
    for heading in headings:
        idx = text.find(heading)
        if idx == -1:
            continue
        tail = text[idx:]
        next_match = re.search(r"\n[一二三四五六七八九十\d]+[、.．]\s*[\u4e00-\u9fff]{2,20}", tail[len(heading):])
        end = len(heading) + next_match.start() if next_match else min(len(tail), max_chars)
        excerpt = compact_text(tail[:end])
        return excerpt, excerpt
    return None, None


def extract_creditor_from_notice(text: str) -> str | None:
    candidates: list[str] = []
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


def field_result(value: Any, source_type: str, source_path: str, excerpt: str | None = None, method: str = "api") -> dict[str, Any]:
    return {
        "value": value,
        "status": "extracted" if not is_blank(value) else "missing_on_page",
        "method": method,
        "confidence": 0.95 if not is_blank(value) else 0.0,
        "source_payload_type": source_type,
        "source_path": source_path,
        "source_excerpt": excerpt or compact_text(value),
    }


class JDScraperDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawl_batches (
                  batch_id TEXT PRIMARY KEY,
                  started_at TEXT,
                  finished_at TEXT,
                  parameters_json TEXT,
                  status TEXT,
                  message TEXT
                );

                CREATE TABLE IF NOT EXISTS raw_payloads (
                  paimai_id TEXT PRIMARY KEY,
                  batch_id TEXT,
                  source_url TEXT,
                  list_json TEXT,
                  detail_json TEXT,
                  realtime_json TEXT,
                  description_html TEXT,
                  notice_html TEXT,
                  announcement_html TEXT,
                  attachments_json TEXT,
                  vendor_json TEXT,
                  crawled_at TEXT
                );

                CREATE TABLE IF NOT EXISTS field_catalog (
                  field_namespace TEXT,
                  asset_group TEXT,
                  field_key TEXT,
                  field_label TEXT,
                  field_scope TEXT,
                  data_type TEXT,
                  required_for_display INTEGER,
                  aliases_json TEXT,
                  source_priority_json TEXT,
                  export_order INTEGER,
                  PRIMARY KEY (field_namespace, field_key)
                );

                CREATE TABLE IF NOT EXISTS field_extractions (
                  paimai_id TEXT,
                  field_namespace TEXT,
                  asset_group TEXT,
                  field_key TEXT,
                  field_label TEXT,
                  raw_value TEXT,
                  normalized_value TEXT,
                  status TEXT,
                  method TEXT,
                  confidence REAL,
                  source_payload_type TEXT,
                  source_path TEXT,
                  source_excerpt TEXT,
                  missing_reason TEXT,
                  extracted_at TEXT,
                  PRIMARY KEY (paimai_id, field_namespace, field_key)
                );
                """
            )
            self._ensure_columns(
                conn,
                "raw_payloads",
                {
                    "notice_html": "TEXT",
                    "announcement_html": "TEXT",
                },
            )
            common_columns = ",\n".join(f"{field.key} TEXT" for field in COMMON_FIELDS)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS auction_items_common (
                  paimai_id TEXT PRIMARY KEY,
                  batch_id TEXT,
                  source_url TEXT,
                  asset_group TEXT NOT NULL,
                  asset_group_label TEXT,
                  jd_category_id TEXT,
                  jd_category_name TEXT,
                  {common_columns},
                  common_fields_json TEXT,
                  updated_at TEXT
                )
                """
            )

            for group, table in ASSET_TABLES.items():
                field_columns = ",\n".join(f"{field.key} TEXT" for field in SPECIAL_FIELDS[group])
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                      paimai_id TEXT PRIMARY KEY,
                      {field_columns},
                      special_fields_json TEXT,
                      updated_at TEXT
                    )
                    """
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_debt_details (
                  paimai_id TEXT,
                  detail_index INTEGER,
                  sequence_no TEXT,
                  debtor_or_asset TEXT,
                  guarantor_or_related_party TEXT,
                  collateral TEXT,
                  principal_balance TEXT,
                  interest_balance TEXT,
                  recovery_fee TEXT,
                  claim_total TEXT,
                  benchmark_date TEXT,
                  amount_unit TEXT,
                  is_continuation INTEGER,
                  source_excerpt TEXT,
                  updated_at TEXT,
                  PRIMARY KEY (paimai_id, detail_index)
                )
                """
            )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed_field_catalog(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM field_catalog")
            rows = []
            for order, field in enumerate(COMMON_FIELDS, start=1):
                rows.append(
                    (
                        "common",
                        "ALL",
                        field.key,
                        field.label,
                        "common",
                        "TEXT",
                        1,
                        safe_json_dumps((field.label, *field.aliases)),
                        safe_json_dumps(["structured_api", "html_table", "html_text"]),
                        order,
                    )
                )
            for group, fields in SPECIAL_FIELDS.items():
                for order, field in enumerate(fields, start=1):
                    rows.append(
                        (
                            f"special.{group}",
                            group,
                            field.key,
                            field.label,
                            "special",
                            "TEXT",
                            1,
                            safe_json_dumps((field.label, *field.aliases)),
                            safe_json_dumps(["structured_api", "html_table", "html_text"]),
                            order,
                        )
                    )
            conn.executemany(
                """
                INSERT OR REPLACE INTO field_catalog
                (field_namespace, asset_group, field_key, field_label, field_scope, data_type,
                 required_for_display, aliases_json, source_priority_json, export_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def start_batch(self, parameters: dict[str, Any]) -> str:
        batch_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_batches (batch_id, started_at, parameters_json, status)
                VALUES (?, ?, ?, ?)
                """,
                (batch_id, now_text(), safe_json_dumps(parameters), "running"),
            )
        return batch_id

    def finish_batch(self, batch_id: str, status: str, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE crawl_batches
                SET finished_at=?, status=?, message=?
                WHERE batch_id=?
                """,
                (now_text(), status, message, batch_id),
            )

    def upsert_raw_payloads(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        source_url: str,
        list_json: Any,
        detail_json: Any,
        realtime_json: Any,
        description_html: str | None,
        notice_html: str | None = None,
        announcement_html: str | None = None,
        attachments_json: Any = None,
        vendor_json: Any = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO raw_payloads
                (paimai_id, batch_id, source_url, list_json, detail_json, realtime_json,
                 description_html, notice_html, announcement_html, attachments_json, vendor_json, crawled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paimai_id) DO UPDATE SET
                  batch_id=excluded.batch_id,
                  source_url=excluded.source_url,
                  list_json=excluded.list_json,
                  detail_json=excluded.detail_json,
                  realtime_json=excluded.realtime_json,
                  description_html=excluded.description_html,
                  notice_html=excluded.notice_html,
                  announcement_html=excluded.announcement_html,
                  attachments_json=excluded.attachments_json,
                  vendor_json=excluded.vendor_json,
                  crawled_at=excluded.crawled_at
                """,
                (
                    paimai_id,
                    batch_id,
                    source_url,
                    safe_json_dumps(list_json),
                    safe_json_dumps(detail_json),
                    safe_json_dumps(realtime_json),
                    description_html or "",
                    notice_html or "",
                    announcement_html or "",
                    safe_json_dumps(attachments_json),
                    safe_json_dumps(vendor_json or {}),
                    now_text(),
                ),
            )

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
    ) -> None:
        full_values = {field.key: compact_text(values.get(field.key)) for field in COMMON_FIELDS}
        data = {
            "paimai_id": paimai_id,
            "batch_id": batch_id,
            "source_url": f"https://paimai.jd.com/{paimai_id}",
            "asset_group": asset_group,
            "asset_group_label": ASSET_GROUP_LABELS[asset_group],
            "jd_category_id": jd_category_id,
            "jd_category_name": jd_category_name,
            **full_values,
            "common_fields_json": safe_json_dumps(full_values),
            "updated_at": now_text(),
        }
        self._upsert_row("auction_items_common", "paimai_id", data)
        self._upsert_field_extractions(
            paimai_id=paimai_id,
            namespace="common",
            asset_group=asset_group,
            fields=COMMON_FIELDS,
            values=full_values,
            field_results=field_results,
        )

    def upsert_special_item(
        self,
        *,
        paimai_id: str,
        asset_group: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        fields = SPECIAL_FIELDS[asset_group]
        full_values = {field.key: compact_text(values.get(field.key)) for field in fields}
        table = ASSET_TABLES[asset_group]
        data = {
            "paimai_id": paimai_id,
            **full_values,
            "special_fields_json": safe_json_dumps(full_values),
            "updated_at": now_text(),
        }
        self._upsert_row(table, "paimai_id", data)
        self._upsert_field_extractions(
            paimai_id=paimai_id,
            namespace=f"special.{asset_group}",
            asset_group=asset_group,
            fields=fields,
            values=full_values,
            field_results=field_results,
        )

    def upsert_debt_details(self, *, paimai_id: str, details: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM asset_debt_details WHERE paimai_id=?", (paimai_id,))
            rows = []
            for index, detail in enumerate(details, start=1):
                rows.append(
                    (
                        paimai_id,
                        index,
                        compact_text(detail.get("sequence_no")),
                        compact_text(detail.get("debtor_or_asset")),
                        compact_text(detail.get("guarantor_or_related_party")),
                        compact_text(detail.get("collateral")),
                        compact_text(detail.get("principal_balance")),
                        compact_text(detail.get("interest_balance")),
                        compact_text(detail.get("recovery_fee")),
                        compact_text(detail.get("claim_total")),
                        compact_text(detail.get("benchmark_date")),
                        compact_text(detail.get("amount_unit")),
                        int(detail.get("is_continuation") or 0),
                        compact_text(detail.get("source_excerpt")),
                        now_text(),
                    )
                )
            conn.executemany(
                """
                INSERT INTO asset_debt_details
                (paimai_id, detail_index, sequence_no, debtor_or_asset, guarantor_or_related_party,
                 collateral, principal_balance, interest_balance, recovery_fee, claim_total,
                 benchmark_date, amount_unit, is_continuation, source_excerpt, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _upsert_row(self, table: str, primary_key: str, data: dict[str, Any]) -> None:
        columns = list(data)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != primary_key)
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {table} ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT({primary_key}) DO UPDATE SET {updates}
                """,
                [data[column] for column in columns],
            )

    def _upsert_field_extractions(
        self,
        *,
        paimai_id: str,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        rows = []
        for field in fields:
            value = values.get(field.key)
            result = field_results.get(field.key, {})
            status = result.get("status")
            if not status:
                status = "extracted" if not is_blank(value) else "missing_on_page"
            rows.append(
                (
                    paimai_id,
                    namespace,
                    asset_group,
                    field.key,
                    field.label,
                    compact_text(value),
                    compact_text(value),
                    status,
                    result.get("method", "not_found" if is_blank(value) else "api_or_html"),
                    float(result.get("confidence", 0.95 if not is_blank(value) else 0.0)),
                    result.get("source_payload_type", ""),
                    result.get("source_path", ""),
                    compact_text(result.get("source_excerpt")),
                    "" if not is_blank(value) else result.get("missing_reason", "页面或接口未提供该字段"),
                    now_text(),
                )
            )
        with self.connect() as conn:
            valid_keys = [field.key for field in fields]
            placeholders = ", ".join("?" for _ in valid_keys)
            conn.execute(
                f"""
                DELETE FROM field_extractions
                WHERE paimai_id=?
                  AND field_namespace=?
                  AND field_key NOT IN ({placeholders})
                """,
                [paimai_id, namespace, *valid_keys],
            )
            conn.executemany(
                """
                INSERT INTO field_extractions
                (paimai_id, field_namespace, asset_group, field_key, field_label, raw_value,
                 normalized_value, status, method, confidence, source_payload_type,
                 source_path, source_excerpt, missing_reason, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paimai_id, field_namespace, field_key) DO UPDATE SET
                  asset_group=excluded.asset_group,
                  field_label=excluded.field_label,
                  raw_value=excluded.raw_value,
                  normalized_value=excluded.normalized_value,
                  status=excluded.status,
                  method=excluded.method,
                  confidence=excluded.confidence,
                  source_payload_type=excluded.source_payload_type,
                  source_path=excluded.source_path,
                  source_excerpt=excluded.source_excerpt,
                  missing_reason=excluded.missing_reason,
                  extracted_at=excluded.extracted_at
                """,
                rows,
            )

    def export_csvs(self, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        exports: dict[str, Path] = {}
        with self.connect() as conn:
            common_path = output_dir / "auction_items_common.csv"
            self._write_query_csv(conn, "SELECT * FROM auction_items_common ORDER BY jd_category_id, paimai_id", common_path)
            exports["common"] = common_path

            field_path = output_dir / "field_extractions.csv"
            self._write_query_csv(
                conn,
                "SELECT * FROM field_extractions ORDER BY paimai_id, field_namespace, field_key",
                field_path,
            )
            exports["field_extractions"] = field_path

            for group, table in ASSET_TABLES.items():
                label = ASSET_GROUP_LABELS[group]
                path = output_dir / f"{group}_{label}.csv"
                query = f"""
                    SELECT c.*, s.*
                    FROM auction_items_common c
                    JOIN {table} s ON c.paimai_id = s.paimai_id
                    WHERE c.asset_group = ?
                    ORDER BY c.jd_category_id, c.paimai_id
                """
                self._write_query_csv(conn, query, path, (group,))
                exports[group] = path

            debt_detail_path = output_dir / "debt_details_债权明细.csv"
            self._write_query_csv(
                conn,
                "SELECT * FROM asset_debt_details ORDER BY paimai_id, detail_index",
                debt_detail_path,
            )
            exports["debt_details"] = debt_detail_path

            qa_path = output_dir / "qa_field_coverage.csv"
            qa_query = """
                SELECT asset_group, field_namespace, field_key, field_label,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='extracted' THEN 1 ELSE 0 END) AS extracted,
                       SUM(CASE WHEN status='missing_on_page' THEN 1 ELSE 0 END) AS missing_on_page,
                       SUM(CASE WHEN status='empty_on_page' THEN 1 ELSE 0 END) AS empty_on_page,
                       SUM(CASE WHEN status='parse_error' THEN 1 ELSE 0 END) AS parse_error,
                       SUM(CASE WHEN status='conflict' THEN 1 ELSE 0 END) AS conflict
                FROM field_extractions
                GROUP BY asset_group, field_namespace, field_key, field_label
                ORDER BY asset_group, field_namespace, field_key
            """
            self._write_query_csv(conn, qa_query, qa_path)
            exports["qa"] = qa_path
        return exports

    @staticmethod
    def _write_query_csv(conn: sqlite3.Connection, query: str, path: Path, params: tuple[Any, ...] = ()) -> None:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        fieldnames = [description[0] for description in cursor.description]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fieldnames)
            for row in rows:
                writer.writerow([row[field] for field in fieldnames])


class JDClient:
    def __init__(self, throttle_seconds: float = 0.35, timeout: int = 25) -> None:
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

    def api(self, function_id: str, body: dict[str, Any], appid: str = "paimai", referer: str | None = None) -> dict[str, Any]:
        if referer:
            self.session.headers["Referer"] = referer
        url = f"{JD_API}?appid={appid}&functionId={function_id}&body={quote(safe_json_dumps(body))}"
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(url, data="null", timeout=self.timeout)
                response.raise_for_status()
                time.sleep(self.throttle_seconds)
                return json.loads(response.content.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001 - keep retry diagnostics simple for crawler use.
                last_error = exc
                time.sleep(self.throttle_seconds * attempt * 2)
        raise RuntimeError(f"JD API request failed: {function_id}: {last_error}")

    def get_categories(self) -> list[JDCategory]:
        try:
            data = self.api("paimai_getPublicSearchCategory", {}, referer="https://pmsearch.jd.com/?projectType=1")
            items = data.get("datas") or data.get("data") or []
            categories = [JDCategory(str(item["id"]), str(item["name"])) for item in items if item.get("id") and item.get("name")]
            return categories or list(FALLBACK_CATEGORIES)
        except Exception:
            return list(FALLBACK_CATEGORIES)

    def search_items(self, category_id: str, page: int = 1, page_size: int = 2) -> tuple[list[dict[str, Any]], int]:
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

    def fetch_detail_bundle(self, paimai_id: str, list_item: dict[str, Any]) -> dict[str, Any]:
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
        description = self.api("queryProductDescription", {"paimaiId": int(paimai_id), "source": 5}, referer=referer)
        notice = self.api("queryNotice", {"paimaiId": int(paimai_id)}, referer=referer)
        announcement = self.api("queryAnnouncement", {"paimaiId": int(paimai_id)}, referer=referer)
        attachments = self.api("queryAttachFilesForIntro", {"paimaiId": int(paimai_id), "custom": 9}, referer=referer)

        vendor = {}
        basic = ((core.get("data") or {}).get("basicData") or {})
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
            except Exception as exc:  # Querying vendor info is useful, but not critical.
                vendor = {"error": str(exc)}

        return {
            "core": core,
            "realtime": realtime,
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


def extract_extend_info(core: dict[str, Any]) -> dict[str, Any]:
    basic = ((core.get("data") or {}).get("basicData") or {})
    return parse_json_object(basic.get("extendInfoMap"))


def extract_media(core: dict[str, Any]) -> list[Any]:
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


def extract_contact(core: dict[str, Any], vendor: dict[str, Any], parsed: ParsedHTML, notice_parsed: ParsedHTML) -> tuple[str | None, dict[str, Any] | None]:
    api_value = deep_find(core, ("contactPhone", "contactTel", "consultTel", "phone", "mobile", "telephone"))
    name = deep_find(core, ("contactName", "consultName", "linkMan", "contacts"))
    if api_value or name:
        value = " ".join(filter(None, [compact_text(name), compact_text(api_value)]))
        notice_contacts = extract_contact_lines(notice_parsed.text)
        if notice_contacts:
            value = "；".join(dict.fromkeys([value, *notice_contacts]))
        return value, field_result(value, "detail_json+notice_html", "deep_find(contact*) + notice_text")
    vendor_phone = deep_find(vendor, ("phone", "mobile", "telephone"))
    if vendor_phone:
        return compact_text(vendor_phone), field_result(vendor_phone, "vendor_json", "deep_find(phone)")
    value, excerpt = find_by_alias(parsed, ("联系方式", "咨询电话", "联系电话", "联系人"))
    if value:
        return value, field_result(value, "description_html", "html_text", excerpt, "html_text")
    notice_contacts = extract_contact_lines(notice_parsed.text)
    if notice_contacts:
        value = "；".join(notice_contacts)
        return value, field_result(value, "notice_html", "contact_lines", value, "html_text_regex")
    return None, None


def extract_contact_lines(text: str) -> list[str]:
    contacts: list[str] = []
    for line in text.splitlines():
        clean = compact_text(line)
        if not clean:
            continue
        if len(clean) > 220:
            continue
        if any(word in clean for word in ("举报", "监督", "开户银行", "账号", "保证金归", "缴入法院指定账户")):
            continue
        has_phone = re.search(r"(?:0\d{2,4}-?\d{6,8}|1[3-9]\d{9})", clean)
        if not has_phone:
            continue
        if re.search(r"(咨询电话|联系电话|联系方式|联系人|法院咨询电话|京东平台咨询电话|中国东方咨询电话|电话\d?|电话[一二]?|经理)", clean):
            contacts.append(clean)
    return list(dict.fromkeys(contacts))


def extract_common_values(
    *,
    category: JDCategory,
    asset_group: str,
    list_item: dict[str, Any],
    bundle: dict[str, Any],
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    core = bundle["core"]
    realtime = bundle["realtime"]
    vendor = bundle.get("vendor") or {}
    data = core.get("data") or {}
    basic = data.get("basicData") or {}
    realtime_data = realtime.get("data") or {}
    media = extract_media(core)
    attachments = {"files": bundle.get("attachments") or [], "media": media}

    values: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}

    def set_value(key: str, value: Any, source_type: str, source_path: str, excerpt: str | None = None, method: str = "api") -> None:
        if not is_blank(value):
            values[key] = value
            results[key] = field_result(value, source_type, source_path, excerpt, method)

    set_value("asset_type", ASSET_GROUP_LABELS[asset_group], "category", category.category_id)
    set_value(
        "asset_location",
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
    status_code = first_non_blank(realtime_data.get("auctionStatus"), list_item.get("auctionStatus"), basic.get("auctionStatus"))
    status_value = STATUS_MAP.get(status_code, compact_text(status_code))
    set_value("project_status", status_value, "realtime_json", "data.auctionStatus")
    stage_code = first_non_blank(list_item.get("paimaiTimes"), basic.get("paimaiTimes"), list_item.get("auctionType"), basic.get("auctionType"))
    stage_value = STAGE_MAP.get(stage_code, compact_text(stage_code))
    set_value("auction_stage", stage_value, "list_json", "paimaiTimes/auctionType")
    set_value("bid_records_json", safe_json_dumps(realtime_data.get("bidList") or []), "realtime_json", "data.bidList")
    set_value(
        "data_source",
        first_non_blank(basic.get("publishSourceName"), list_item.get("publishSourceName"), "京东拍卖"),
        "detail_json",
        "basicData.publishSourceName",
    )
    set_value("project_name", first_non_blank(basic.get("title"), list_item.get("title")), "detail_json", "basicData.title")
    set_value(
        "signup_start_time",
        format_time(deep_find(core, ("applyStartTime", "signupStartTime", "signUpStartTime", "enrollStartTime"))),
        "detail_json",
        "deep_find(signup start)",
    )
    set_value(
        "signup_end_time",
        format_time(deep_find(core, ("applyEndTime", "signupEndTime", "signUpEndTime", "enrollEndTime"))),
        "detail_json",
        "deep_find(signup end)",
    )
    set_value(
        "disposal_party",
        first_non_blank(
            basic.get("shopName"),
            list_item.get("shopName"),
            basic.get("vendorName"),
            deep_find(vendor, ("orgName", "vendorName", "shopName", "name")),
        ),
        "detail_json",
        "basicData.shopName/vendorName",
    )
    set_value(
        "start_price_raw",
        format_money(
            first_non_blank(basic.get("startPrice"), list_item.get("startPrice")),
            first_non_blank(basic.get("startPriceStr"), list_item.get("startPriceStr"), list_item.get("startPriceCN")),
        ),
        "list_json",
        "startPrice/startPriceStr",
    )
    set_value(
        "final_price_raw",
        format_money(
            first_non_blank(realtime_data.get("currentPrice"), list_item.get("currentPrice"), basic.get("currentPrice")),
            first_non_blank(realtime_data.get("currentPriceStr"), list_item.get("currentPriceStr"), list_item.get("currentPriceCN")),
        ),
        "realtime_json",
        "data.currentPrice/currentPriceStr",
    )
    contact_value, contact_result = extract_contact(core, vendor, parsed, notice_parsed)
    if contact_value and contact_result:
        values["contact_info"] = contact_value
        results["contact_info"] = contact_result
    special_aliases = (
        "\u7279\u522b\u544a\u77e5",
        "\u7279\u522b\u63d0\u9192",
        "\u6ce8\u610f\u4e8b\u9879",
        "\u7279\u522b\u63d0\u793a",
        "\u7279\u522b\u8bf4\u660e",
    )
    description_special, description_special_excerpt = find_by_alias(parsed, special_aliases)
    description_section, description_section_excerpt = extract_section_after_heading(
        parsed.text,
        ("\u7279\u522b\u63d0\u793a", "\u7279\u522b\u8bf4\u660e", "\u7279\u522b\u544a\u77e5"),
    )
    notice_section, notice_section_excerpt = extract_section_after_heading(
        notice_parsed.text,
        ("\u7279\u522b\u8bf4\u660e", "\u7279\u522b\u63d0\u793a", "\u7279\u522b\u544a\u77e5"),
    )
    for special_value, source_type, source_path, excerpt, method in (
        (
            deep_find(core, ("specialNotice", "notice", "importantNotice")),
            "detail_json",
            "deep_find(specialNotice)",
            None,
            "api",
        ),
        (
            description_special,
            "description_html",
            "html_table_or_text",
            description_special_excerpt,
            "html_table_or_text",
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
    ):
        if not is_blank(special_value):
            set_value("special_notice", special_value, source_type, source_path, excerpt, method)
            break
    assessment = first_non_blank(
        list_item.get("assessmentPriceCN"),
        basic.get("assessmentPriceCN"),
        format_money(first_non_blank(list_item.get("assessmentPrice"), basic.get("assessmentPrice"))),
        find_by_alias(parsed, ("评估价格及时间", "评估价", "评估价格"))[0],
    )
    set_value("assessment_price_time", assessment, "list_json", "assessmentPriceCN")
    set_value("attachments_json", safe_json_dumps(attachments), "attachments_json", "data")
    return values, results


def structured_special_candidates(group: str, key: str, extend: dict[str, Any], core: dict[str, Any]) -> tuple[Any, str] | tuple[None, None]:
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
    return None, None


def extract_special_values(
    *,
    asset_group: str,
    parsed: ParsedHTML,
    notice_parsed: ParsedHTML,
    core: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    values: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}
    extend = extract_extend_info(core)
    debt_details: list[dict[str, Any]] = []

    if asset_group == "other":
        values["raw_detail_text"] = parsed.text
        values["raw_table_pairs_json"] = safe_json_dumps(parsed.key_values)
        results["raw_detail_text"] = field_result(parsed.text, "description_html", "text", parsed.text[:300], "html_text")
        results["raw_table_pairs_json"] = field_result(parsed.key_values, "description_html", "table_pairs", method="html_table")
        return values, results, debt_details

    if asset_group == "debt":
        debt_details = parse_debt_package_details(parsed)
        summary = summarize_debt_details(debt_details)
        for key, value in summary.items():
            if key == "claim_total":
                continue
            values[key] = value
            results[key] = field_result(value, "description_html", "debt_package_table", value, "html_table")
        defect, defect_excerpt = extract_section_after_heading(parsed.text, ("特别提示", "特别说明", "特别告知"), max_chars=1600)
        if defect:
            values["disclosed_defects"] = defect
            results["disclosed_defects"] = field_result(defect, "description_html", "special_notice_section", defect_excerpt, "html_text")

    for field in SPECIAL_FIELDS[asset_group]:
        if field.key in values:
            continue
        aliases = (field.label, *field.aliases)
        structured_value, structured_path = structured_special_candidates(asset_group, field.key, extend, core)
        if not is_blank(structured_value):
            values[field.key] = structured_value
            results[field.key] = field_result(structured_value, "detail_json", structured_path or "extendInfoMap")
            continue

        value, excerpt = find_by_alias(parsed, aliases)
        if is_blank(value):
            value, excerpt = find_by_alias(notice_parsed, aliases)
        if not is_blank(value):
            source_type = "notice_html" if excerpt and excerpt in notice_parsed.text else "description_html"
            values[field.key] = value
            results[field.key] = field_result(value, source_type, "html_table_or_text", excerpt, "html_table_or_text")
            continue

        if asset_group == "debt" and field.key == "benchmark_date":
            match = re.search(r"截至\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2})", parsed.text)
            if match:
                values[field.key] = match.group(1)
                results[field.key] = field_result(match.group(1), "description_html", "text_regex", match.group(0), "html_text_regex")
                continue
        if asset_group == "debt" and field.key == "creditor":
            notice_creditor = extract_creditor_from_notice(notice_parsed.text)
            if notice_creditor:
                values[field.key] = notice_creditor
                results[field.key] = field_result(notice_creditor, "notice_html", "creditor_text_regex", notice_creditor, "html_text_regex")

    return values, results, debt_details


class JDAuctionScraper:
    def __init__(self, db: JDScraperDatabase, client: JDClient) -> None:
        self.db = db
        self.client = client

    def crawl_sample(self, *, per_category_limit: int, output_dir: Path, categories: set[str] | None = None) -> dict[str, Any]:
        self.db.init_schema()
        self.db.seed_field_catalog()
        batch_id = self.db.start_batch({"per_category_limit": per_category_limit, "categories": sorted(categories or [])})
        seen: set[str] = set()
        category_counts: dict[str, int] = {}
        errors: list[dict[str, str]] = []
        try:
            for category in self.client.get_categories():
                if categories and category.category_id not in categories:
                    continue
                items, _total = self.client.search_items(category.category_id, page=1, page_size=per_category_limit)
                category_counts[f"{category.category_id}-{category.name}"] = len(items)
                for list_item in items[:per_category_limit]:
                    paimai_id = compact_text(first_non_blank(list_item.get("id"), list_item.get("paimaiId")))
                    if not paimai_id or paimai_id in seen:
                        continue
                    seen.add(paimai_id)
                    try:
                        self._crawl_one(batch_id, category, list_item, paimai_id)
                    except Exception as exc:  # noqa: BLE001 - keep crawling other categories.
                        errors.append({"paimai_id": paimai_id, "category": category.category_id, "error": str(exc)})
            status = "success" if not errors else "partial_success"
            self.db.finish_batch(batch_id, status, safe_json_dumps(errors[:20]))
        except Exception as exc:
            self.db.finish_batch(batch_id, "failed", str(exc))
            raise

        exports = self.db.export_csvs(output_dir)
        return {
            "batch_id": batch_id,
            "items_seen": len(seen),
            "category_counts": category_counts,
            "errors": errors,
            "exports": {key: str(path) for key, path in exports.items()},
        }

    def _crawl_one(self, batch_id: str, category: JDCategory, list_item: dict[str, Any], paimai_id: str) -> None:
        asset_group = classify_category(category)
        bundle = self.client.fetch_detail_bundle(paimai_id, list_item)
        description_html = bundle.get("description_html") or ""
        notice_html = bundle.get("notice_html") or ""
        announcement_html = bundle.get("announcement_html") or ""
        parsed = extract_key_values_from_html(description_html)
        notice_parsed = extract_key_values_from_html("\n".join([notice_html, announcement_html]))

        self.db.upsert_raw_payloads(
            paimai_id=paimai_id,
            batch_id=batch_id,
            source_url=f"https://paimai.jd.com/{paimai_id}",
            list_json=list_item,
            detail_json=bundle.get("core") or {},
            realtime_json=bundle.get("realtime") or {},
            description_html=description_html,
            notice_html=notice_html,
            announcement_html=announcement_html,
            attachments_json=bundle.get("attachments") or [],
            vendor_json=bundle.get("vendor") or {},
        )

        common_values, common_results = extract_common_values(
            category=category,
            asset_group=asset_group,
            list_item=list_item,
            bundle=bundle,
            parsed=parsed,
            notice_parsed=notice_parsed,
        )
        special_values, special_results, debt_details = extract_special_values(
            asset_group=asset_group,
            parsed=parsed,
            notice_parsed=notice_parsed,
            core=bundle.get("core") or {},
        )

        self.db.upsert_common_item(
            paimai_id=paimai_id,
            batch_id=batch_id,
            asset_group=asset_group,
            jd_category_id=category.category_id,
            jd_category_name=category.name,
            values=common_values,
            field_results=common_results,
        )
        self.db.upsert_special_item(
            paimai_id=paimai_id,
            asset_group=asset_group,
            values=special_values,
            field_results=special_results,
        )
        if asset_group == "debt":
            self.db.upsert_debt_details(paimai_id=paimai_id, details=debt_details)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="京东资产拍卖采集器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl = subparsers.add_parser("crawl", help="采集样例或正式数据")
    crawl.add_argument("--per-category-limit", type=int, default=2, help="每个京东一级类目最多采集多少条")
    crawl.add_argument("--output-dir", type=Path, default=Path("outputs") / "latest", help="输出目录")
    crawl.add_argument("--db-path", type=Path, default=None, help="SQLite 路径，默认在输出目录下")
    crawl.add_argument("--categories", default="", help="只采集指定一级类目 ID，逗号分隔")
    crawl.add_argument("--throttle", type=float, default=0.35, help="请求间隔秒数")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "crawl":
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        db_path = args.db_path or output_dir / "jd_auction.sqlite"
        categories = {part.strip() for part in args.categories.split(",") if part.strip()} or None
        db = JDScraperDatabase(db_path)
        client = JDClient(throttle_seconds=args.throttle)
        scraper = JDAuctionScraper(db, client)
        summary = scraper.crawl_sample(
            per_category_limit=args.per_category_limit,
            output_dir=output_dir,
            categories=categories,
        )
        print(safe_json_dumps({"db_path": str(db_path), **summary}))


if __name__ == "__main__":
    main()
