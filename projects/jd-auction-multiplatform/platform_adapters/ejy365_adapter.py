
import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from jd.ai_extractor import AIExtractionContext


DEFAULT_BASE_URL = "https://www.ejy365.com"
JMJL_API_URL = f"{DEFAULT_BASE_URL}/jmjl_detail"
DETAIL_BASE_URL = f"{DEFAULT_BASE_URL}/info/"
PROJECT_NO_RE = re.compile(r"\b[A-Z]\d{3,6}[A-Z]{1,4}\d{4,}\b")
FILE_LINK_RE = re.compile(r"\.(?:(?:pdf|docx)?|xlsx?|(?:zip|rar))(?:$|[?#])", re.IGNORECASE)
LIST_LABEL_STOP_TOKENS = (
    "项目编号",
    "地区",
    "项目所在地",
    "所在地",
    "挂牌价",
    "挂牌价格",
    "转让底价",
    "转让价格",
    "价格",
    "保证金",
    "交易保证金",
    "状态",
    "项目状态",
    "报名截止",
    "报名截止时间",
    "报名结束时间",
)

EJY365_PROJECT_TYPE_LABELS: Dict[str, Tuple[str, str]] = {
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
    "GGJYQ": ("usufruct", "广告经营权"),
    "ZJGC": ("other", "在建工程"),
    "MTQCNZB": ("other", "煤炭去产能指标"),
    "PFQJY": ("usufruct", "排放权交易"),
    "STZY": ("usufruct", "生态资源"),
    "KCPZR": ("goods", "矿产品转让"),
    "QYTPH": ("other", "区域碳普惠"),
    "SJCPL": ("other", "数据产品类"),
    "SLL": ("other", "算力类"),
    "SB": ("equipment", "设备"),
    "CZ": ("vehicle", "船舶"),
    "GYYSQ": ("usufruct", "国有土地使用权"),
    "TDFWQ": ("usufruct", "土地服务权"),
    "QTKQ": ("usufruct", "其他矿权"),
    "TZQ": ("usufruct", "投资权"),
    "JYQ": ("usufruct", "经营权"),
    "CSSYQ": ("usufruct", "场所使用权"),
    "ZRZY": ("usufruct", "自然资源"),
    "PWS": ("usufruct", "排污权"),
    "PWFQ": ("usufruct", "排放权"),
    "HJSYQ": ("usufruct", "环境使用权"),
    "JN": ("other", "节能"),
    "SJ": ("other", "数据资产"),
    "QT": ("other", "其他"),
}

EJY365_ASSET_KEYWORDS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("real_estate", "房地产", ("房产", "房地产", "不动产", "住宅", "商铺", "厂房", "车位", "车库")),
    ("debt", "债权", ("债权", "债务", "应收", "不良资产")),
    ("land", "土地", ("土地", "地块", "建设用地", "工业用地")),
    ("vehicle", "车辆", ("车辆", "机动车", "汽车", "客车", "货车", "轿车")),
    ("equipment", "设备", ("设备", "机器", "机械", "生产线")),
    ("equity", "股权", ("股权", "股份", "出资")),
    ("ip", "知识产权", ("知识产权", "专利", "商标", "著作权", "许可")),
    ("goods", "物资产品", ("物资", "存货", "商品", "原材料")),
    ("usufruct", "用益物权", ("使用权", "收益权", "经营权", "租赁权")),
)


def ejy365_asset_for_project_type(project_type: Any) -> Tuple[str, str]:
    code = compact_text(project_type).upper()
    if not code:
        return "other", "其他"
    if code in EJY365_PROJECT_TYPE_LABELS:
        return EJY365_PROJECT_TYPE_LABELS[code]
    for marker in sorted(EJY365_PROJECT_TYPE_LABELS, key=len, reverse=True):
        if marker in code:
            return EJY365_PROJECT_TYPE_LABELS[marker]
    return "other", "其他"


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return compact_text(text)


def field_result_value(
    value: Any,
    source_payload_type: str,
    source_path: str,
    source_excerpt: Optional[str] = None,
    method: str = "html_rule",
    confidence: Optional[float] = None,
) -> Dict[str, Any]:
    if confidence is None:
        confidence = 0.95 if compact_text(value) else 0.0
    return {
        "value": value,
        "status": "extracted" if compact_text(value) else "missing_on_page",
        "method": method if compact_text(value) else "not_found",
        "confidence": confidence,
        "source_payload_type": source_payload_type,
        "source_path": source_path,
        "source_excerpt": source_excerpt or compact_text(value),
    }


@dataclass
class Ejy365ListItem:
    title: str
    detail_url: str
    slug: str
    project_no: Optional[str] = None
    region: Optional[str] = None
    price_raw: Optional[str] = None
    deposit_raw: Optional[str] = None
    status: Optional[str] = None
    signup_deadline: Optional[str] = None
    source_excerpt: str = ""
    raw_html: str = ""


@dataclass
class Ejy365DetailBundle:
    url: str
    html: str
    detail_text: str
    key_values: Dict[str, str]
    attachments: List[Dict[str, Any]]
    image_urls: List[str] = field(default_factory=list)
    auxiliary_json: Optional[Dict[str, Any]] = None
    status_json: Optional[Dict[str, Any]] = None
    list_item: Optional[Ejy365ListItem] = None
    title: Optional[str] = None
    source_item_id: Optional[str] = None
    raw_payloads: Dict[str, Any] = field(default_factory=dict)
    bid_records_json: Optional[List[Dict[str, Any]]] = None
    jmjl_detail: Optional[Dict[str, Any]] = None


class _ParsedHTML:
    def __init__(self) -> None:
        self.text = ""
        self.rows: List[List[str]] = []
        self.links: List[Dict[str, str]] = []


class _SimpleHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: List[str] = []
        self.rows: List[List[str]] = []
        self.links: List[Dict[str, str]] = []
        self._skip_depth = 0
        self._current_link: Optional[Dict[str, Any]] = None
        self._current_row: Optional[List[str]] = None
        self._current_cell: Optional[List[str]] = None
        # dl/dt/dd pair tracking (e交易 uses <dl><dt>key</dt><dd>value</dd></dl>)
        self._last_dt_text: Optional[str] = None
        self._current_dd: Optional[List[str]] = None
        self._dl_depth: int = 0  # nested dl counter, pairs dt/dd only within same dl

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "a":
            self._current_link = {"href": attrs_dict.get("href", ""), "text_parts": []}
        elif tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"}:
            self._current_cell = []
        elif tag == "dl":
            self._dl_depth += 1
        elif tag == "dt":
            # <dt>key</dt> — text will be collected via handle_data
            pass
        elif tag == "dd":
            # <dd>value</dd> — only pair if inside a <dl>
            if self._dl_depth > 0:
                self._current_dd = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in {"td", "th"} and self._current_cell is not None:
            cell = compact_text("".join(self._current_cell))
            if self._current_row is not None and cell:
                self._current_row.append(cell)
            self._current_cell = None
        elif tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None
        elif tag == "a" and self._current_link is not None:
            text = compact_text("".join(self._current_link.get("text_parts", [])))
            href = compact_text(self._current_link.get("href"))
            if href or text:
                self.links.append({"href": href, "text": text})
            self._current_link = None
        elif tag == "dt":
            # After collecting dt text from handle_data, scan text_parts backwards
            # to find the last dt line (ends with : or ：)
            for part in reversed(self.text_parts):
                stripped = compact_text(part)
                if stripped.rstrip(":：") and (stripped.endswith("：") or stripped.endswith(":")):
                    self._last_dt_text = stripped.rstrip(":：")
                    break
                if stripped:
                    break
        elif tag == "dl":
            self._dl_depth = max(0, self._dl_depth - 1)
            if self._dl_depth == 0:
                self._last_dt_text = None  # reset on dl exit to avoid cross-block pairing
        elif tag == "dd" and self._current_dd is not None:
            dd_text = compact_text("".join(self._current_dd))
            self._current_dd = None
            if dd_text and self._last_dt_text and _looks_like_key(self._last_dt_text) and self._dl_depth > 0:
                self.rows.append([self._last_dt_text, dd_text])
                self._last_dt_text = None  # consumed
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.text_parts.append(data)
        if self._current_link is not None:
            self._current_link["text_parts"].append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)
        if self._current_dd is not None:
            self._current_dd.append(data)

    def parsed(self) -> _ParsedHTML:
        parsed = _ParsedHTML()
        parsed.text = compact_text("\n".join(part for part in self.text_parts if part is not None))
        parsed.rows = self.rows
        parsed.links = self.links
        return parsed


def parse_html(html: str) -> _ParsedHTML:
    parser = _SimpleHTMLParser()
    parser.feed(html or "")
    parser.close()
    return parser.parsed()


def extract_key_values(html: str) -> Dict[str, str]:
    parsed = parse_html(html)
    key_values: Dict[str, str] = {}

    # Regex to detect form/noise content in values
    _form_noise_re = re.compile(r"(?:输入|placeholder)|value\s*=|<(?:input|textarea)|select", re.IGNORECASE)

    for row in parsed.rows:
        if len(row) < 2:
            continue
        for index in range(0, len(row) - 1, 2):
            key = compact_text(row[index]).rstrip(":：")
            value = compact_text(row[index + 1])
            if _looks_like_key(key) and value and not _form_noise_re.search(value):
                key_values.setdefault(key, value)

    for line in re.split(r"[\n\r]+|(?<=。)\s*", parsed.text):
        line = compact_text(line)
        if not line or len(line) > 240 or _form_noise_re.search(line):
            continue
        match = re.match(r"^([^:：]{2,30})[:：]\s*(.+)$", line)
        if not match:
            continue
        key = compact_text(match.group(1)).rstrip(":：")
        value = compact_text(match.group(2))
        if _looks_like_key(key) and value:
            key_values.setdefault(key, value)
    return key_values


def _looks_like_key(key: str) -> bool:
    if not key or len(key) > 30:
        return False
    noisy_tokens = {"序号", "文件", "下载", "附件", "请输入", "会员", "退出", "登录", "注册"}
    return key not in noisy_tokens


def first_non_blank(*values: Any) -> Optional[str]:
    for value in values:
        text = compact_text(value)
        if text:
            return text
    return None


def is_masked_identifier(value: Any) -> bool:
    text = compact_text(value)
    if not text:
        return True
    normalized = re.sub(r"[\s\-_/]+", "", text)
    return not normalized or "*" in normalized or "＊" in normalized


def first_stable_source_id(*values: Any) -> Optional[str]:
    for value in values:
        text = compact_text(value)
        if text and not is_masked_identifier(text):
            return text
    return None


def find_by_labels(key_values: Dict[str, str], labels: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    normalized = {compact_text(key).replace(" ", ""): key for key in key_values}
    for label in labels:
        wanted = compact_text(label).replace(" ", "")
        if wanted in normalized:
            key = normalized[wanted]
            return key_values[key], key
    for label in labels:
        wanted = compact_text(label).replace(" ", "")
        for normalized_key, original_key in normalized.items():
            if wanted and wanted in normalized_key:
                return key_values[original_key], original_key
    return None, None


def find_label_value(text: str, labels: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    compact = compact_text(text)
    stop_pattern = "|".join(re.escape(token) for token in LIST_LABEL_STOP_TOKENS)
    for label in labels:
        pattern = rf"{re.escape(label)}\s*[:：]\s*(.+?)(?=\s*(?:{stop_pattern})\s*[:：]|$)"
        match = re.search(pattern, compact)
        if match:
            return compact_text(match.group(1)), label
    return None, None


def deep_find(data: Any, keys: Iterable[str]) -> Optional[Any]:
    key_set = {key.lower() for key in keys}
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in key_set and compact_text(value):
                return value
        for value in data.values():
            found = deep_find(value, keys)
            if compact_text(found):
                return found
    elif isinstance(data, list):
        for item in data:
            found = deep_find(item, keys)
            if compact_text(found):
                return found
    return None


def first_project_no(*values: Any) -> Optional[str]:
    for value in values:
        text = compact_text(value)
        if not text:
            continue
        match = PROJECT_NO_RE.search(text)
        if match:
            return match.group(0)
    return None


class Ejy365Adapter:
    source_platform = "ejy365"

    def infer_asset_type(self, bundle: Ejy365DetailBundle) -> Tuple[str, str, str]:
        list_item = bundle.list_item
        candidates = [
            getattr(list_item, "project_type_code", "") if list_item else "",
            getattr(list_item, "project_type", "") if list_item else "",
            bundle.source_item_id,
            list_item.project_no if list_item else "",
            list_item.slug if list_item else "",
        ]
        for candidate in candidates:
            asset_group, asset_label = ejy365_asset_for_project_type(candidate)
            if asset_group != "other":
                return asset_group, asset_label, compact_text(candidate)

        haystack = compact_text(" ".join([
            bundle.title or "",
            list_item.title if list_item else "",
            bundle.detail_text[:2000],
        ]))
        for asset_group, asset_label, keywords in EJY365_ASSET_KEYWORDS:
            if any(keyword in haystack for keyword in keywords):
                return asset_group, asset_label, "title_or_detail_keyword"
        return "other", "其他", ""

    def parse_list_html(self, html: str, base_url: str = DEFAULT_BASE_URL) -> List[Ejy365ListItem]:
        items: List[Ejy365ListItem] = []
        for href, title, block_html in self._iter_detail_anchor_blocks(html):
            detail_url = urljoin(base_url, href)
            slug = self._slug_from_url(detail_url)
            block_text = parse_html(block_html).text
            project_no = first_project_no(block_text, title, href)
            region, _ = find_label_value(block_text, ("地区", "项目所在地", "所在地"))
            price, _ = find_label_value(block_text, ("挂牌价", "挂牌价格", "转让底价", "转让价格", "价格"))
            deposit, _ = find_label_value(block_text, ("保证金", "交易保证金"))
            status, _ = find_label_value(block_text, ("状态", "项目状态"))
            signup_deadline, _ = find_label_value(block_text, ("报名截止", "报名截止时间", "报名结束时间"))

            items.append(
                Ejy365ListItem(
                    title=title,
                    detail_url=detail_url,
                    slug=slug,
                    project_no=project_no,
                    region=region,
                    price_raw=price,
                    deposit_raw=deposit,
                    status=status,
                    signup_deadline=signup_deadline,
                    source_excerpt=block_text,
                    raw_html=block_html,
                )
            )
        return items

    def parse_detail_html(
        self,
        html: str,
        url: str = "",
        list_item: Optional[Ejy365ListItem] = None,
        auxiliary_json: Optional[Dict[str, Any]] = None,
        status_json: Optional[Dict[str, Any]] = None,
        jmjl_detail: Optional[Dict[str, Any]] = None,
    ) -> Ejy365DetailBundle:
        parsed = parse_html(html)
        key_values = extract_key_values(html)
        title = self._extract_title(html) or first_non_blank(
            key_values.get("项目名称"),
            key_values.get("标的名称"),
            list_item.title if list_item else None,
        )
        project_no_value, _ = find_by_labels(key_values, ("项目编号", "项目代码", "项目号"))
        url_slug = self._slug_from_url(url) if url else None
        source_item_id = first_stable_source_id(
            project_no_value,
            list_item.project_no if list_item else None,
            first_project_no(parsed.text),
            compact_text(deep_find(auxiliary_json, ("projectNo", "project_no", "projectid", "infoid"))),
            list_item.slug if list_item else None,
            url_slug,
        )

        attachments = self._extract_attachments(parsed.links, url or DEFAULT_BASE_URL)
        image_urls = self._extract_image_urls(html, url or DEFAULT_BASE_URL)
        raw_payloads = {
            "detail_html": html,
            "auxiliary_json": auxiliary_json,
            "status_json": status_json,
        }
        if list_item:
            raw_payloads["list_html"] = list_item.raw_html
        if jmjl_detail:
            raw_payloads["jmjl_detail"] = jmjl_detail

        bid_records_json = self._extract_bid_records(jmjl_detail) if jmjl_detail else None

        return Ejy365DetailBundle(
            url=url,
            html=html,
            detail_text=parsed.text,
            key_values=key_values,
            attachments=attachments,
            image_urls=image_urls,
            auxiliary_json=auxiliary_json,
            status_json=status_json,
            list_item=list_item,
            title=title,
            source_item_id=source_item_id,
            raw_payloads=raw_payloads,
            bid_records_json=bid_records_json,
            jmjl_detail=jmjl_detail,
        )

    def build_ai_context(self, bundle: Ejy365DetailBundle) -> AIExtractionContext:
        asset_group, _, _ = self.infer_asset_type(bundle)
        raw_payloads = {
            key: value for key, value in bundle.raw_payloads.items() if value is not None
        }
        detail_sections = [
            f"source_platform: {self.source_platform}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.url}",
            "detail_text:\n" + bundle.detail_text,
        ]
        if bundle.attachments:
            detail_sections.append("attachments:\n" + json.dumps(bundle.attachments, ensure_ascii=False))
        if raw_payloads:
            detail_sections.append("raw_payloads:\n" + json.dumps(raw_payloads, ensure_ascii=False, default=str))

        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(detail_sections)[:12000],
            notice_text="",
            image_urls=list(bundle.image_urls),
            asset_group=asset_group,
            paimai_id=f"ejy365:{bundle.source_item_id}" if bundle.source_item_id else "",
        )

    def map_common_candidates(self, bundle: Ejy365DetailBundle) -> Dict[str, Any]:
        common: Dict[str, Any] = {}
        results: Dict[str, Dict[str, Any]] = {}

        def set_field(
            field_key: str,
            value: Any,
            source_payload_type: str,
            source_path: str,
            source_excerpt: Optional[str] = None,
            method: str = "html_rule",
            confidence: Optional[float] = None,
        ) -> None:
            common[field_key] = value
            results[field_key] = field_result_value(
                value,
                source_payload_type,
                source_path,
                source_excerpt,
                method,
                confidence,
            )

        list_item = bundle.list_item
        asset_group, asset_label, asset_source = self.infer_asset_type(bundle)
        source_item_id = first_stable_source_id(
            bundle.source_item_id,
            list_item.project_no if list_item else None,
            list_item.slug if list_item else None,
            self._slug_from_url(bundle.url) if bundle.url else None,
        )
        title_value, title_label = find_by_labels(bundle.key_values, ("项目名称", "标的名称", "转让标的名称"))
        title = first_non_blank(title_value, bundle.title, list_item.title if list_item else None)
        location_value, location_label = find_by_labels(bundle.key_values, ("项目所在地", "地区", "标的所在地", "所在地", "咨询地址", "单位地址"))
        location = first_non_blank(location_value, list_item.region if list_item else None)
        status_value, status_label = find_by_labels(bundle.key_values, ("项目状态", "状态", "交易状态"))
        status = first_non_blank(
            status_value,
            compact_text(deep_find(bundle.status_json, ("status", "projectStatus", "jyzt"))),
            list_item.status if list_item else None,
        )
        contact_value, contact_label = find_by_labels(bundle.key_values, ("联系人", "联系方式", "联系电话", "咨询电话"))
        contact = first_non_blank(contact_value, self._extract_contact_line(bundle.detail_text))
        notice = self._extract_special_notice(bundle.detail_text)
        disposal_party_value, disposal_party_label = find_by_labels(
            bundle.key_values, ("转让方名称", "转让人名称", "转让方", "委托方", "出让方", "处置方", "招商主体")
        )

        start_price, start_label = find_by_labels(bundle.key_values, ("起拍价", "起始价", "拍卖底价", "转让底价", "转让底价（元）", "挂牌价", "挂牌价格"))
        final_price, final_label = find_by_labels(
            bundle.key_values,
            ("成交价", "当前价", "最高报价", "挂牌价格", "挂牌价", "转让底价", "转让价格", "价格"),
        )
        if start_price and not self._is_price_value(start_price):
            start_price = None
            start_label = None
        if final_price and not self._is_price_value(final_price):
            final_price = None
            final_label = None
        if not final_price and list_item and list_item.price_raw:
            final_price = list_item.price_raw
            final_label = "挂牌价"
        if not final_price:
            aux_price = deep_find(
                bundle.auxiliary_json,
                ("price", "offer", "listingPrice", "gpjg", "bjjg", "currentPrice", "finalPrice"),
            )
            if compact_text(aux_price):
                aux_price_text = compact_text(aux_price)
                if self._is_price_value(aux_price_text):
                    final_price = aux_price_text
                    final_label = "auxiliary_json.price"

        price_basis = self._price_basis(final_label)
        attachments_json = json.dumps(bundle.attachments, ensure_ascii=False)

        set_field("source_platform", self.source_platform, "computed", "adapter.source_platform", self.source_platform, "constant", 1.0)
        set_field(
            "source_item_id",
            source_item_id,
            "detail_html" if bundle.source_item_id else "list_html",
            "key_values.项目编号" if bundle.source_item_id else "list_item.project_no",
            f"项目编号：{source_item_id}" if source_item_id else None,
        )
        set_field("source_url", bundle.url, "detail_html", "request.url", bundle.url, "request", 1.0)
        set_field(
            "asset_group",
            asset_group,
            "computed",
            "adapter.asset_group",
            asset_source or asset_group,
            "category_mapping",
            0.9,
        )
        set_field(
            "asset_type",
            asset_label,
            "computed",
            "adapter.asset_type",
            asset_source or asset_label,
            "category_mapping",
            0.9,
        )
        set_field(
            "project_name",
            title,
            "detail_html",
            f"key_values.{title_label}" if title_label else "h1",
            f"{title_label}：{title}" if title_label else title,
        )
        set_field(
            "asset_location",
            location,
            "detail_html" if location_label else "list_html",
            f"key_values.{location_label}" if location_label else "list_item.region",
            f"{location_label}：{location}" if location_label else (list_item.source_excerpt if list_item else location),
        )
        set_field(
            "project_status",
            status,
            "detail_html" if status_label else "list_html",
            f"key_values.{status_label}" if status_label else "list_item.status",
            f"{status_label}：{status}" if status_label else (list_item.source_excerpt if list_item else status),
        )
        set_field(
            "start_price_raw",
            start_price,
            "detail_html",
            f"key_values.{start_label}" if start_label else "price_candidates.start_price",
            f"{start_label}：{start_price}" if start_label else None,
        )
        set_field(
            "final_price_raw",
            final_price,
            "detail_html" if final_label != "挂牌价" or not list_item else "list_html",
            f"key_values.{final_label}" if final_label and final_label != "auxiliary_json.price" else str(final_label or "price_candidates.final_price"),
            f"{final_label}：{final_price}" if final_label and final_label != "auxiliary_json.price" else None,
        )
        set_field(
            "contact_info",
            contact,
            "detail_html",
            f"key_values.{contact_label}" if contact_label else "detail_text.contact_line",
            f"{contact_label}：{contact}" if contact_label else contact,
        )
        set_field("special_notice", notice, "detail_html", "detail_text.special_notice", notice)
        set_field("disposal_party", disposal_party_value, "detail_html", f"key_values.{disposal_party_label}" if disposal_party_label else "detail_text", disposal_party_value)
        set_field("attachments_json", attachments_json, "detail_html", "attachments", attachments_json)
        set_field("data_source", "e交易", "computed", "adapter.data_source", "e交易", "constant", 1.0)
        set_field("price_basis", price_basis, "detail_html", f"key_values.{final_label}" if final_label else "price_candidates.final_price", price_basis)

        common["field_results"] = results
        return common

    def map_special_candidates(self, bundle: Ejy365DetailBundle, asset_group: str) -> Dict[str, Any]:
        if asset_group != "debt":
            return {}
        details = self.extract_debt_details(bundle)
        if not details:
            return {}
        first_detail = details[0]
        creditor, _ = find_by_labels(
            bundle.key_values,
            ("债权人", "权利人", "转让方", "出让方", "委托方", "委托人"),
        )
        values: Dict[str, Any] = {}
        for field_key, detail_key in (
            ("debtor_name", "debtor_name"),
            ("guarantee_method", "guarantee_method"),
            ("litigation_status", "litigation_status"),
            ("benchmark_date", "benchmark_date"),
        ):
            value = compact_text(first_detail.get(detail_key))
            if value:
                values[field_key] = value
        if creditor:
            values["creditor"] = creditor
        values["household_count"] = str(len(details))
        return values

    def extract_debt_details(self, bundle: Ejy365DetailBundle) -> List[Dict[str, Any]]:
        def value_for(labels: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
            return find_by_labels(bundle.key_values, labels)

        debtor_name, debtor_label = value_for(("主债务人名称", "债务人名称", "主债务人", "债务人", "借款人", "客户名称", "贷款主体"))
        claim_total, claim_total_label = value_for(("债权总额", "债权合计", "债权金额", "债权余额", "转让债权金额", "合计金额"))
        principal, principal_label = value_for(("本金余额", "本金", "债权本金", "贷款本金", "借款本金", "剩余本金", "接收时本金"))
        interest, interest_label = value_for(("利息余额", "利息", "债权利息", "欠息", "罚息", "剩余利息", "待偿还利息"))
        guarantee, guarantee_label = value_for(("担保方式", "担保情况", "保证人", "担保人", "抵押物", "质押物", "抵质押物", "债权抵押情况"))
        collateral, collateral_label = value_for(("抵押物", "质押物", "抵质押物", "担保物", "质押物详情", "抵押物详情"))
        litigation, litigation_label = value_for(("诉讼状态", "执行状态", "案件状态", "债权状态", "诉讼执行情况"))
        benchmark, benchmark_label = value_for(("基准日", "债权基准日", "截止日", "截至日", "截止日期"))

        if not any((debtor_name, claim_total, principal, interest, guarantee, collateral, litigation)):
            return []

        detail: Dict[str, Any] = {
            "sequence_no": "1",
            "debtor_name": compact_text(debtor_name),
            "debtor_or_asset": compact_text(debtor_name),
            "claim_total": self._debt_value_with_unit(claim_total, claim_total_label),
            "principal_balance": self._debt_value_with_unit(principal, principal_label),
            "interest_balance": self._debt_value_with_unit(interest, interest_label),
            "guarantee_method": compact_text(guarantee),
            "guarantor_or_related_party": compact_text(guarantee),
            "collateral": compact_text(collateral),
            "litigation_status": compact_text(litigation),
            "benchmark_date": compact_text(benchmark),
            "amount_unit": self._label_unit(claim_total_label or principal_label or interest_label),
            "source_excerpt": json.dumps(bundle.key_values, ensure_ascii=False),
        }
        return [{key: value for key, value in detail.items() if compact_text(value)}]

    def _iter_detail_anchor_blocks(self, html: str) -> Iterable[Tuple[str, str, str]]:
        anchor_re = re.compile(
            r"(?is)<a\b(?P<attrs>[^>]*\bhref\s*=\s*['\"](?P<href>[^'\"]*/info/[^'\"]+)['\"][^>]*)>(?P<title>.*?)</a>"
        )
        matches = list(anchor_re.finditer(html or ""))
        for index, match in enumerate(matches):
            block_start = self._find_item_block_start(html, match.start())
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(html)
            block_end = self._find_item_block_end(html, match.end(), next_start)
            block_html = html[block_start:block_end]
            title = compact_text(strip_tags(match.group("title")))
            if not title:
                continue
            yield match.group("href"), title, block_html

    def _find_item_block_start(self, html: str, anchor_start: int) -> int:
        candidates = [html.rfind(token, 0, anchor_start) for token in ("<li", "<tr", "<div")]
        candidates = [candidate for candidate in candidates if candidate >= 0]
        return max(candidates) if candidates else max(0, anchor_start - 800)

    def _find_item_block_end(self, html: str, anchor_end: int, next_anchor_start: int) -> int:
        search_end = min(next_anchor_start, len(html))
        candidates: List[int] = []
        for token in ("</li>", "</tr>", "</div>"):
            position = html.find(token, anchor_end, search_end)
            if position >= 0:
                candidates.append(position + len(token))
        return min(candidates) if candidates else search_end

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        if "/info/" in path:
            return path.rsplit("/info/", 1)[-1]
        return path.rsplit("/", 1)[-1]

    def _extract_title(self, html: str) -> Optional[str]:
        for tag in ("h1", "title"):
            match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", html or "")
            if match:
                title = compact_text(strip_tags(match.group(1)))
                if title:
                    return title
        return None

    def _extract_attachments(self, links: List[Dict[str, str]], base_url: str) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        seen = set()
        for link in links:
            href = link.get("href", "")
            text = compact_text(link.get("text"))
            if not href:
                continue
            is_file = bool(FILE_LINK_RE.search(href)) or "附件" in text or "下载" in text
            if not is_file or "/info/" in href:
                continue
            absolute_url = urljoin(base_url or DEFAULT_BASE_URL, href)
            if self._is_generic_site_attachment(absolute_url, text):
                continue
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            name = text or absolute_url.rsplit("/", 1)[-1]
            attachments.append(
                {
                    "name": name,
                    "url": absolute_url,
                    "source_payload_type": "detail_html",
                    "source_path": "a[href]",
                    "source_excerpt": name,
                }
            )
        return attachments

    def _is_generic_site_attachment(self, url: str, name: str = "") -> bool:
        lower_url = (url or "").lower()
        lower_name = compact_text(name).lower()
        if "/static/html/" in lower_url:
            return True
        generic_name_tokens = (
            "产权贷",
            "借款人条件",
            "借款产品",
            "真实性承诺",
            "贷款申请资料",
            "cdqdksqzlqd",
            "jkrtjjjkcpnr",
            "zsxcns",
        )
        return any(token.lower() in lower_name or token.lower() in lower_url for token in generic_name_tokens)

    def _extract_bid_records(self, jmjl_detail: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(jmjl_detail, dict):
            return []
        raw_records: List[Any] = []
        for key in ("baojiaHis", "his", "bidList", "records", "data"):
            value = jmjl_detail.get(key)
            if isinstance(value, list):
                raw_records.extend(value)

        records: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for entry in raw_records:
            if not isinstance(entry, dict):
                continue
            price = first_non_blank(
                entry.get("price"),
                entry.get("bidPrice"),
                entry.get("offer"),
                entry.get("amount"),
                entry.get("bjjg"),
                entry.get("报价"),
                entry.get("报价金额"),
            )
            bid_time = first_non_blank(
                entry.get("bidTime"),
                entry.get("time"),
                entry.get("createTime"),
                entry.get("bjsj"),
                entry.get("报价时间"),
                entry.get("出价时间"),
            )
            bidder = first_non_blank(
                entry.get("username"),
                entry.get("userName"),
                entry.get("buyer"),
                entry.get("memberName"),
                entry.get("报价人"),
                entry.get("竞买人"),
            )
            if not any((price, bid_time, bidder)):
                continue
            record: Dict[str, Any] = {}
            if bid_time:
                record["bid_time"] = bid_time
            if price:
                record["price"] = price
            if bidder:
                record["bidder"] = bidder
            for extra_key in ("isPrior", "whetherMyBid", "status", "rank"):
                if extra_key in entry:
                    record[extra_key] = entry.get(extra_key)
            dedupe_key = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(record)
        return records

    def _extract_image_urls(self, html: str, base_url: str) -> List[str]:
        image_urls: List[str] = []
        seen: set[str] = set()
        for match in re.finditer(
            r"""(?is)<img\b[^>]*\b(?:src|data-src|data-original|data-lazy-src|data-url)\s*=\s*['"]([^'"]+)['"]""",
            html or "",
        ):
            raw_url = compact_text(match.group(1))
            if not raw_url or raw_url.startswith("data:"):
                continue
            absolute_url = urljoin(base_url or DEFAULT_BASE_URL, raw_url)
            if not self._is_item_image_url(absolute_url):
                continue
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            image_urls.append(absolute_url)
        return image_urls

    def _is_item_image_url(self, url: str) -> bool:
        parsed = urlparse(url)
        lower_url = url.lower()
        lower_path = (parsed.path or "").lower()
        if not re.search(r"\.(?:(?:jpg|jpeg)|(?:png|webp)|gif)(?:$|[?#])", lower_url):
            return False
        if any(
            token in lower_url
            for token in (
                "qrcode",
                "qr-code",
                "captcha",
                "valcode",
                "verify",
                "logo",
                "icon",
                "kefu",
                "/upload/ad/",
                "/ad/",
                "banner",
                "advert",
            )
        ):
            return False
        if "pic.ejy365.com" in parsed.netloc.lower():
            return True
        return any(token in lower_path for token in ("/upload/", "/uploads/", "/file/", "/files/", "/project/", "/cqjy/"))

    def _label_unit(self, label: Optional[str]) -> Optional[str]:
        text = compact_text(label)
        if "亿元" in text or "人民币亿元" in text:
            return "亿元"
        if "万元" in text or "人民币万元" in text:
            return "万元"
        if "元" in text or "人民币元" in text:
            return "元"
        return None

    def _debt_value_with_unit(self, value: Any, label: Optional[str]) -> Optional[str]:
        text = compact_text(value)
        if not text:
            return None
        if re.search(r"((?:元|万元)|(?:亿元|人民币))", text):
            return text
        unit = self._label_unit(label)
        return f"{text}{unit}" if unit else text

    def _extract_infoid(self, html: str) -> Optional[str]:
        """从详情页 HTML 中提取项目内部 ID (infoid)，用于竞买记录 API 调用"""
        if not html:
            return None
        # 1. 从 iframe url 中提取: jmjl?infoid=XXX
        m = re.search(r'jmjl\?infoid=([^"&]+)', html)
        if m:
            return m.group(1)
        # 2. 从 JS 变量中提取: projectguid = "XXX" 或 infoid = "XXX"
        m = re.search(r'(?:(?:projectguid|infoid))\s*[=:]\s*["\']([^"\']+)["\']', html)
        if m:
            return m.group(1)
        return None

    def fetch_jmjl_detail(self, infoid: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
        """调用 e交易竞买记录 API 获取竞买历史数据"""
        if not infoid:
            return None
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{DEFAULT_BASE_URL}/jmjl?infoid={infoid}",
            }
            resp = requests.get(
                f"{JMJL_API_URL}?infoid={infoid}",
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                if isinstance(data, dict) and data.get("gg"):
                    return data
            return None
        except Exception:
            return None

    def _extract_contact_line(self, text: str) -> Optional[str]:
        # Regex to detect form-like / input placeholder noise
        form_noise = re.compile(r"(?:输入|placeholder)|value\s*=|<(?:input|textarea)|(?:select|class)\s*=", re.IGNORECASE)
        for line in re.split(r"[\n\r。；;]+", text or ""):
            line = compact_text(line)
            if not line or form_noise.search(line):
                continue
            if any(token in line for token in ("联系人", "联系电话", "联系方式", "咨询电话")):
                # Skip if it looks like a generic form label with no actual contact info
                if len(line) > 80 or not re.search(r"\d", line):
                    continue
                return line
        return None

    def _extract_special_notice(self, text: str) -> Optional[str]:
        match = re.search(r"((?:特别提示|特别告知)|重要提示)\s*[:：]\s*([^。；;\n\r]+(?:。)?)", text or "")
        if match:
            return compact_text(match.group(0))
        return None

    def _price_basis(self, label: Optional[str]) -> Optional[str]:
        if not label:
            return None
        if label.startswith("auxiliary_json"):
            return "辅助JSON价格"
        if "挂牌" in label:
            return "挂牌价"
        if "当前" in label or "报价" in label:
            return "当前价"
        if "成交" in label:
            return "成交价"
        if "转让" in label:
            return "转让价"
        return label

    def _is_price_value(self, value: Any) -> bool:
        text = compact_text(value)
        if not text:
            return False
        if re.search(r"\d", text):
            return True
        return any(token in text for token in ("面议", "详见", "无底价", "免费", "以公告为准", "另行通知"))
