
import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from jd.ai_extractor import AIExtractionContext, AI_DETAIL_TEXT_LIMIT


CQUAE_BASE_URL = "https://www.cquae.com"
CQUAE_LIST_PATH = "/Project"
CQUAE_DATA_SOURCE = "重庆联合产权交易所/重庆产权交易网"
CQUAE_PLATFORM = "cquae"

WAF_MARKERS = (
    "__jsl_clearance_s",
    "knownsec",
    "创宇盾",
    "知道创宇",
    "chaitin",
    "521 web server",
)


@dataclass
class CquaeListItem:
    source_item_id: str
    source_url: str
    title: str
    project_type: Optional[str] = None
    project_status: Optional[str] = None
    price_raw: Optional[str] = None
    deposit_raw: Optional[str] = None
    date_text: Optional[str] = None
    contact_info: Optional[str] = None
    raw_fields: Dict[str, str] = field(default_factory=dict)
    raw_text: str = ""


@dataclass
class CquaeDetailBundle:
    source_item_id: str
    source_url: str
    title: str
    key_values: Dict[str, str]
    attachments: List[Dict[str, str]]
    detail_text: str
    list_item: Optional[CquaeListItem] = None
    image_urls: List[str] = field(default_factory=list)
    raw_html: str = ""


class _CquaeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.reset()
        self.text = ""
        self.headings: List[str] = []
        self.rows: List[List["_Cell"]] = []
        self.current_row: List["_Cell"] = []
        self.current_cell: Optional["_Cell"] = None
        self.links: List["_Link"] = []
        self.image_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "h1" or tag == "h2" or tag == "h3" or tag == "h4" or tag == "h5" or tag == "h6":
            self.headings.append("")
        elif tag == "tr":
            self.current_row = []
        elif tag == "td" or tag == "th":
            self.current_cell = _Cell(text="", links=[], images=[])
            self.current_row.append(self.current_cell)
        elif tag == "a" and self.current_cell:
            href = dict(attrs).get("href", "")
            text = ""
            self.current_cell.links.append(_Link(href=href, text=text))
        elif tag == "img" and self.current_cell:
            src = dict(attrs).get("src", "")
            self.current_cell.images.append(src)
        elif tag == "a":
            href = dict(attrs).get("href", "")
            text = ""
            self.links.append(_Link(href=href, text=text))
        elif tag == "img":
            src = dict(attrs).get("src", "")
            self.image_urls.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" or tag == "h2" or tag == "h3" or tag == "h4" or tag == "h5" or tag == "h6":
            if self.headings:
                self.headings[-1] = self.text
        elif tag == "tr":
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
        elif tag == "td" or tag == "th":
            if self.current_cell:
                self.current_cell.text = self.text
                self.current_cell = None
        elif tag == "a":
            pass
        elif tag == "img":
            pass

    def handle_data(self, data: str) -> None:
        self.text += data

    def reset(self) -> None:
        super().reset()
        self.text = ""
        self.headings = []
        self.rows = []
        self.current_row = []
        self.current_cell = None
        self.links = []
        self.image_urls = []


@dataclass
class _Cell:
    text: str
    links: List["_Link"]
    images: List[str]


@dataclass
class _Link:
    href: str
    text: str


def extract_key_values(parser: _CquaeHTMLParser) -> Dict[str, str]:
    key_values: Dict[str, str] = {}
    for row in parser.rows:
        cells = [cell.text for cell in row if cell.text]
        if len(cells) < 2:
            continue
        if len(cells) == 2 and looks_like_label(cells[0]):
            add_key_value(key_values, cells[0], cells[1])
            continue
        if len(cells) % 2 == 0 and all(looks_like_label(cell) for cell in cells[0::2]):
            for key, value in zip(cells[0::2], cells[1::2]):
                add_key_value(key_values, key, value)

    for line in parser.text.splitlines():
        match = re.match(r"^\s*([^:：]{2,30})[:：]\s*(.+?)\s*$", line)
        if not match:
            continue
        add_key_value(key_values, match.group(1), match.group(2))
    return key_values


def add_key_value(values: Dict[str, str], key: str, value: str) -> None:
    clean_key = normalize_label(key)
    clean_value = compact_text(value)
    if clean_key and clean_value and clean_key not in values:
        values[clean_key] = clean_value


def extract_attachments(links: Iterable[_Link], page_url: str) -> List[Dict[str, str]]:
    attachments: List[Dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        if not is_attachment_link(link):
            continue
        full_url = urljoin(page_url, link.href)
        if full_url in seen:
            continue
        seen.add(full_url)
        name = link.text or attachment_name_from_url(full_url)
        attachments.append({"name": name, "url": full_url})
    return attachments


def extract_attachments_from_raw_html(html: str, page_url: str) -> List[Dict[str, str]]:
    """从原始 HTML 的附件区域中提取附件链接，避免采集导航/页脚等无关区域的链接。

    CQUAE 页面使用 <tr> 表格布局，附件行通常包含“附件材料”等标签。
    优先从附件行提取，兜底再从整个页面中只按 URL 扩展名匹配。
    """
    if not html:
        return []

    # 方案一：从包含“附件”标签的表格行中提取链接（仅含 dw.ashx / 文件扩展名链接）
    # 匹配 <tr>...<td>附件*</td><td>...<a>...</a>...</td>...</tr>
    attachment_row_re = re.compile(
        r'<tr[^>]*>(?=.*?附件)(?>.*?<t[dh][^>]*>(?:[^<]*附件[^<]*)</t[dh]>)(?>.*?<t[dh][^>]*>)(.*?)</t[dh]>.*?</tr>',
        re.I | re.S,
    )
    match = attachment_row_re.search(html)
    if match:
        cell_html = match.group(1)
        links: List[_Link] = []
        for m in re.finditer(r'<a\s+[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', cell_html, re.I):
            href = m.group(1)
            text = m.group(2)
            link = _Link(href=href, text=text)
            # 只保留真正的文件链接（dw.ashx 或文件扩展名）
            if href and (is_attachment_href(link) or any(ext in href.lower() for ext in ("dw.ashx", "/attachment/"))):
                links.append(link)
        if links:
            return extract_attachments(links, page_url)

    # 方案二：从整个页面中找到 dw.ashx / /attachment/ 等明确的文件下载链接
    all_links: List[_Link] = []
    for m in re.finditer(r'<a\s+[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', html, re.I):
        href = m.group(1)
        text = m.group(2)
        href_lower = href.lower()
        ext_match = ATTACHMENT_EXT_RE.search(href_lower)
        is_file_link = (
            ext_match is not None
            or "dw.ashx" in href_lower
            or "/attachment/" in href_lower
            or "noauthorizefiles" in href_lower
        )
        if not is_file_link:
            continue
        # 排除导航页面的路径（CQUAE 导航链接格式）
        if re.match(r'^/(?:(?:Project|Home)|(?:Customer|News)|(?:About|Help)|Search)', href_lower):
            continue
        all_links.append(_Link(href=href, text=text))
    if all_links:
        return extract_attachments(all_links, page_url)

    return []


def is_attachment_href(link: _Link) -> bool:
    """仅通过 URL 扩展名判断是否为附件（不依赖文本关键词）。"""
    href = compact_text(link.href).lower()
    if not href or href.startswith(("javascript:", "about:", "#")):
        return False
    return ATTACHMENT_EXT_RE.search(href) is not None


ATTACHMENT_EXT_RE = re.compile(r"\.(?:(?:pdf|docx)?|xlsx?|(?:xls|zip)|(?:rar|pptx)?|(?:jpg|jpeg)|png)(?:$|[?#])", re.I)


def is_attachment_link(link: _Link) -> bool:
    """判断链接是否为附件链接，使用 URL 扩展名和固定关键词匹配。

    仅保留 dw.ashx 等系统下载链接和常见文件扩展名匹配，
    移除了宽泛的文本关键词（如"报告""承诺""清单"）以减少误采。
    """
    href = compact_text(link.href).lower()
    text = compact_text(link.text)
    if not href or href.startswith(("javascript:", "about:", "#")):
        return False
    if "dw.ashx" in href or "/attachment/" in href or "noauthorizefiles" in href:
        return True
    if ATTACHMENT_EXT_RE.search(href):
        return True
    return False


def attachment_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    for key in ("filename", "fileName", "name"):
        value = parse_qs(parsed.query).get(key)
        if value and value[0]:
            return compact_text(value[0])
    return compact_text(parsed.path.rsplit("/", 1)[-1]) or "attachment"


def best_title(link_text: str, cells: List[_Cell]) -> str:
    text = compact_text(link_text)
    if text and text not in {"详情", "查看", "查看详情"}:
        return text
    values = [cell.text for cell in cells if cell.text and not looks_like_label(cell.text)]
    return max(values, key=len, default=text)


def inline_label_value(key: str, alias: str) -> Optional[str]:
    match = re.search(re.escape(alias) + r"\s*[:：]\s*(.+)$", compact_text(key))
    if not match:
        return None
    return compact_text(match.group(1))


def find_by_alias(fields: Dict[str, str], aliases: Tuple[str, ...]) -> Optional[str]:
    for alias in aliases:
        for key, value in fields.items():
            label = normalize_label(key)
            if alias in label:
                inline_value = inline_label_value(key, alias)
                if inline_value:
                    return inline_value
                clean_value = compact_text(value)
                if clean_value:
                    return clean_value
    return None


def find_by_alias_filtered(
    fields: Dict[str, str],
    aliases: Tuple[str, ...],
    *,
    forbidden: Tuple[str, ...] = (),
) -> Optional[str]:
    for alias in aliases:
        for key, value in fields.items():
            label = normalize_label(key)
            if alias not in label:
                continue
            if any(token in label for token in forbidden):
                continue
            inline_value = inline_label_value(key, alias)
            clean_value = compact_text(inline_value or value)
            if clean_value:
                return clean_value
    return None


def infer_asset_type_from_sources(
    fields: Dict[str, str],
    list_item: Optional[CquaeListItem],
    project_name: Optional[str],
    detail_text: str,
) -> Optional[str]:
    title_type = infer_project_type_from_title(project_name or "")
    exact = first_non_blank(
        find_by_alias_filtered(
            fields,
            ("项目类型", "标的类型", "资产类型", "资产类别"),
            forbidden=("企业", "公司", "法人", "经济", "机构"),
        ),
        list_item.project_type if list_item else None,
    )
    if title_type and is_generic_or_entity_asset_type(exact):
        return title_type
    if exact and not is_generic_or_entity_asset_type(exact):
        return exact
    if title_type:
        return title_type
    return infer_project_type(" ".join([project_name or "", detail_text or ""]))


def clean_certificate_no(value: Optional[str]) -> Optional[str]:
    clean = compact_text(value)
    if not clean:
        return None
    if re.search(r"(ICP|备案|许可证|营业执照)", clean, flags=re.I):
        return None
    if len(clean) < 4:
        return None
    return clean


def assessment_price_from_key_values(fields: Dict[str, str]) -> Optional[str]:
    unit_aliases = (
        "评估结果（万元）",
        "评估结果(万元)",
        "评估价值（万元）",
        "评估价值(万元)",
        "评估价（万元）",
        "评估价(万元)",
        "市场价值（万元）",
        "市场价值(万元)",
    )
    for alias in unit_aliases:
        value = find_by_alias(fields, (alias,))
        if not value:
            continue
        clean = compact_text(value)
        if not re.search(r"\d", clean):
            continue
        if clean and not re.search(r"[元万亿]", clean):
            return f"{clean}万元"
        return clean
    fallback = find_by_alias(fields, ("评估结果", "评估价值", "评估价", "评估价格", "市场价值", "市场价"))
    if fallback and re.search(r"\d", fallback):
        return fallback
    return None


def first_non_blank(*values: Optional[str]) -> Optional[str]:
    for value in values:
        clean = compact_text(value)
        if clean:
            return clean
    return None


def join_non_blank(*values: Optional[str]) -> Optional[str]:
    parts: List[str] = []
    seen: set[str] = set()
    for value in values:
        clean = compact_text(value)
        if clean and clean not in seen:
            parts.append(clean)
            seen.add(clean)
    return " ".join(parts) if parts else None


def extract_plate_number(text: str) -> Optional[str]:
    match = re.search(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{4,6}", text or "")
    return match.group(0) if match else None


def extract_area(text: str) -> Optional[str]:
    match = re.search(r"(?:(?:建筑面积|土地面积)|(?:出租面积|面积)|宗地面积)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:平方米|㎡|(?:平米|m²)))", text or "", flags=re.I)
    return compact_text(match.group(1)) if match else None


TYPE_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("债权", ("债权", "不良资产包", "债务", "应收账款")),
    ("股权", ("股权", "企业增资", "增资扩股")),
    ("知识产权", ("知识产权", "专利", "商标", "著作权", "软件著作权")),
    ("车辆", ("车辆", "机动车", "汽车", "轿车", "客车", "货车")),
    ("房地产", ("房地产", "房产", "房屋", "商铺", "车位", "车库", "不动产", "住宅", "门面", "写字楼")),
    ("土地", ("土地", "宗地", "建设用地", "土地使用权")),
    ("设备", ("设备", "机器", "机械", "生产线")),
    ("物资", ("物资", "存货", "货物", "实物资产", "紫砂", "茶具", "瓷器", "工艺品", "艺术品", "收藏品", "酒")),
    ("用益物权", ("租赁", "经营权", "使用权", "收益权", "采矿权", "林权")),
    ("其他", ("其他", "产权转让", "资产转让", "项目转让")),
)


GENERIC_OR_ENTITY_ASSET_TYPES = {
    "产权转让",
    "资产转让",
    "项目转让",
    "正式披露",
    "预披露",
    "挂牌项目",
    "其他",
}


def is_generic_or_entity_asset_type(value: Optional[str]) -> bool:
    clean = compact_text(value)
    if not clean:
        return True
    if clean in GENERIC_OR_ENTITY_ASSET_TYPES:
        return True
    if clean in {"企业增资"}:
        return False
    entity_tokens = ("有限责任公司", "股份有限公司", "国有企业", "集体企业", "私营企业", "企业类型", "公司性质")
    return any(token in clean for token in entity_tokens)


def infer_project_type_from_title(text: str) -> Optional[str]:
    haystack = compact_text(text)
    if not haystack:
        return None
    for asset_type, keywords in TYPE_KEYWORDS:
        for keyword in keywords:
            if keyword not in haystack:
                continue
            if asset_type == "房地产" and keyword in {"房产", "房地产"}:
                return keyword
            if asset_type == "用益物权" and keyword in {"租赁", "经营权", "使用权", "收益权", "采矿权", "林权"}:
                return keyword
            return asset_type
    return None


def infer_project_type(text: str) -> Optional[str]:
    haystack = compact_text(text)
    for asset_type, keywords in TYPE_KEYWORDS:
        for keyword in keywords:
            if keyword not in haystack:
                continue
            if asset_type == "房地产" and keyword in {"房产", "房地产"}:
                return keyword
            if asset_type == "用益物权" and keyword in {"租赁", "经营权", "使用权", "收益权", "采矿权", "林权"}:
                return keyword
            return asset_type
    return None


def infer_project_status(text: str) -> Optional[str]:
    haystack = compact_text(text)
    for keyword in ("正式披露", "预披露", "报名中", "已成交", "已结束", "挂牌中", "进行中"):
        if keyword in haystack:
            return keyword
    return None


def classify_asset_group(asset_type: Optional[str], project_name: Optional[str], detail_text: str) -> str:
    category_text = compact_text(asset_type or "")
    title_text = compact_text(project_name or "")
    detail_compact = compact_text(detail_text or "")
    checks = (
        ("debt", ("债权",)),
        ("vehicle", ("车辆", "机动车", "汽车")),
        ("real_estate", ("房产", "房地产", "房屋", "商铺", "车位")),
        ("land", ("土地", "宗地")),
        ("equipment", ("设备", "机器", "机械")),
        ("goods", ("物资", "存货", "货物", "实物资产", "紫砂", "茶具", "瓷器", "工艺品", "艺术品", "收藏品", "酒")),
        ("ip", ("知识产权", "专利", "商标", "著作权", "软件著作权")),
        ("usufruct", ("租赁", "经营权", "使用权")),
        ("equity", ("股权", "企业增资", "产权转让")),
    )

    def group_from_text(text: str) -> Optional[str]:
        for group, keywords in checks:
            if any(keyword in text for keyword in keywords):
                return group
        return None

    title_group = group_from_text(title_text)
    category_group = group_from_text(category_text)
    if title_group and (is_generic_or_entity_asset_type(category_text) or category_group != title_group):
        return title_group
    if category_group and not is_generic_or_entity_asset_type(category_text):
        return category_group

    for group, keywords in checks:
        if any(keyword in category_text for keyword in keywords):
            return group
    for group, keywords in checks:
        if any(keyword in title_text for keyword in keywords):
            return group
    haystack = compact_text(" ".join(value for value in (category_text, title_text, detail_compact) if value))
    for group, keywords in checks:
        if any(keyword in haystack for keyword in keywords):
            return group
    return "other"


def list_items_from_links(links: Iterable[_Link], base_url: str) -> List[CquaeListItem]:
    items: List[CquaeListItem] = []
    seen_ids: set[str] = set()
    for link in links:
        source_item_id = extract_project_id(link.href)
        if not source_item_id or source_item_id in seen_ids:
            continue
        seen_ids.add(source_item_id)
        title = compact_text(link.text)
        items.append(
            CquaeListItem(
                source_item_id=source_item_id,
                source_url=urljoin(base_url, link.href),
                title=title,
                project_type=infer_project_type(title),
                raw_text=title,
            )
        )
    return items


def format_pairs(values: Dict[str, str]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in values.items())


def compact_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def dedupe_texts(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    results: List[str] = []
    for value in values:
        text = compact_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        results.append(text)
    return results


def normalize_label(value: Optional[str]) -> str:
    text = compact_text(value)
    text = re.sub(r"^[：:\s]+|[：:\s]+$", "", text)
    return text


def looks_like_label(value: Optional[str]) -> bool:
    text = normalize_label(value)
    if not text:
        return False
    if len(text) > 30:
        return False
    if re.search(r"https?://|dw\.ashx", text, flags=re.I):
        return False
    if re.search(r"\d{4}[-年/]\d{1,2}|[0-9]+(?:\.[0-9]+)?\s*[万亿]?元", text):
        return False
    return True


def extract_project_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(unescape(url))
    query_id = parse_qs(parsed.query).get("id")
    if query_id and query_id[0]:
        return compact_text(query_id[0])
    match = re.search(r"(?:Project/Show\?id=|[?&]id=)([^&#\"'\s<>]+)", url, flags=re.I)
    if match:
        return compact_text(match.group(1))
    return None


def fields_from_cells(cells: List[_Cell], headers: Optional[List[str]]) -> Dict[str, str]:
    texts = [cell.text for cell in cells]
    fields: Dict[str, str] = {}
    if headers and len(headers) == len(texts):
        for header, value in zip(headers, texts):
            clean_header = normalize_label(header)
            clean_value = compact_text(value)
            if clean_header and clean_value:
                fields[clean_header] = clean_value
    return fields


def price_display_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    price = compact_text(str(payload.get("pubPrice") or payload.get("pubAmount") or payload.get("price") or ""))
    if not price:
        return None
    if re.search(r"\d", price):
        return price
    return None


def is_blank(value: str) -> bool:
    return not compact_text(value)


def normalize_amount(value: str) -> str:
    """Normalize amount string to remove common formatting."""
    if not value:
        return ""
    # Remove common currency symbols and whitespace
    clean = re.sub(r"[￥$,，]", "", value)
    # Remove extra whitespace
    clean = re.sub(r"\s+", "", clean)
    return clean


def normalize_date(value: str) -> str:
    """Normalize date string to standard format."""
    if not value:
        return ""
    # Handle various date formats
    date_patterns = [
        (r"(\d{4})[-年/](\d{1,2})[-年/](\d{1,2})", r"\1-\2-\3"),
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", r"\1-\2-\3"),
    ]
    for pattern, replacement in date_patterns:
        match = re.search(pattern, value)
        if match:
            return re.sub(pattern, replacement, value)
    return value


def normalize_area(value: str) -> str:
    """Normalize area string to standard format."""
    if not value:
        return ""
    # Extract numeric value and unit
    match = re.search(r"(\d+(?:\.\d+)?)\s*(㎡|(?:平方米|平米)|m²)", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value


def normalize_phone(value: str) -> str:
    """Extract phone number from text."""
    if not value:
        return ""
    # Match common phone number patterns
    phone_pattern = r"1[3-9]\d{9}|\d{3,4}-?\d{7,8}|\d{7,8}"
    matches = re.findall(phone_pattern, value)
    if matches:
        return matches[0]
    return value


def normalize_email(value: str) -> str:
    """Extract email address from text."""
    if not value:
        return ""
    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    match = re.search(email_pattern, value)
    if match:
        return match.group(0)
    return value


def normalize_currency(value: str) -> str:
    """Normalize currency string to numeric value."""
    if not value:
        return ""
    # Remove currency symbols and units
    clean = re.sub(r"[￥$,，元万]", "", value)
    # Remove extra whitespace
    clean = re.sub(r"\s+", "", clean)
    return clean


def normalize_percentage(value: str) -> str:
    """Normalize percentage string."""
    if not value:
        return ""
    # Remove percentage sign
    clean = re.sub(r"%", "", value)
    # Remove extra whitespace
    clean = re.sub(r"\s+", "", clean)
    return clean


def normalize_unit(value: str, unit: str) -> str:
    """Normalize value with specific unit."""
    if not value:
        return ""
    # Remove unit from value
    clean = re.sub(re.escape(unit), "", value)
    # Remove extra whitespace
    clean = re.sub(r"\s+", "", clean)
    return clean


def normalize_numeric(value: str) -> str:
    """Normalize numeric string."""
    if not value:
        return ""
    # Remove non-numeric characters except decimal point
    clean = re.sub(r"[^0-9.]", "", value)
    return clean


def normalize_text(value: str) -> str:
    """Normalize text string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_url(value: str) -> str:
    """Normalize URL string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", "", value)
    return clean.strip()


def normalize_boolean(value: str) -> str:
    """Normalize boolean string."""
    if not value:
        return ""
    # Convert common boolean representations
    value_lower = value.lower()
    if value_lower in ("是", "yes", "true", "1", "y"):
        return "是"
    elif value_lower in ("否", "no", "false", "0", "n"):
        return "否"
    return value


def normalize_status(value: str) -> str:
    """Normalize status string."""
    if not value:
        return ""
    # Normalize common status values
    status_map = {
        "正式披露": "正式披露",
        "预披露": "预披露",
        "报名中": "报名中",
        "已成交": "已成交",
        "已结束": "已结束",
        "挂牌中": "挂牌中",
        "进行中": "进行中",
    }
    return status_map.get(value, value)


def normalize_asset_type(value: str) -> str:
    """Normalize asset type string."""
    if not value:
        return ""
    # Normalize common asset type values
    asset_type_map = {
        "债权": "债权",
        "股权": "股权",
        "知识产权": "知识产权",
        "车辆": "车辆",
        "房地产": "房地产",
        "土地": "土地",
        "设备": "设备",
        "物资": "物资",
        "用益物权": "用益物权",
        "其他": "其他",
    }
    return asset_type_map.get(value, value)


def normalize_project_type(value: str) -> str:
    """Normalize project type string."""
    if not value:
        return ""
    # Normalize common project type values
    project_type_map = {
        "债权": "债权",
        "股权": "股权",
        "知识产权": "知识产权",
        "车辆": "车辆",
        "房地产": "房地产",
        "土地": "土地",
        "设备": "设备",
        "物资": "物资",
        "用益物权": "用益物权",
        "其他": "其他",
    }
    return project_type_map.get(value, value)


def normalize_location(value: str) -> str:
    """Normalize location string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_address(value: str) -> str:
    """Normalize address string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_name(value: str) -> str:
    """Normalize name string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_description(value: str) -> str:
    """Normalize description string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_notes(value: str) -> str:
    """Normalize notes string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_comment(value: str) -> str:
    """Normalize comment string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_note(value: str) -> str:
    """Normalize note string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_remark(value: str) -> str:
    """Normalize remark string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_info(value: str) -> str:
    """Normalize info string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_data(value: str) -> str:
    """Normalize data string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_content(value: str) -> str:
    """Normalize content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_text_content(value: str) -> str:
    """Normalize text content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_html_content(value: str) -> str:
    """Normalize HTML content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_markdown_content(value: str) -> str:
    """Normalize Markdown content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_json_content(value: str) -> str:
    """Normalize JSON content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xml_content(value: str) -> str:
    """Normalize XML content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_csv_content(value: str) -> str:
    """Normalize CSV content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tsv_content(value: str) -> str:
    """Normalize TSV content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_yaml_content(value: str) -> str:
    """Normalize YAML content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_sql_content(value: str) -> str:
    """Normalize SQL content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_js_content(value: str) -> str:
    """Normalize JS content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_css_content(value: str) -> str:
    """Normalize CSS content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_html(value: str) -> str:
    """Normalize HTML string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_css(value: str) -> str:
    """Normalize CSS string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_js(value: str) -> str:
    """Normalize JS string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_json(value: str) -> str:
    """Normalize JSON string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xml(value: str) -> str:
    """Normalize XML string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_yaml(value: str) -> str:
    """Normalize YAML string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_csv(value: str) -> str:
    """Normalize CSV string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tsv(value: str) -> str:
    """Normalize TSV string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_sql(value: str) -> str:
    """Normalize SQL string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_markdown(value: str) -> str:
    """Normalize Markdown string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_text_file(value: str) -> str:
    """Normalize text file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_html_file(value: str) -> str:
    """Normalize HTML file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_css_file(value: str) -> str:
    """Normalize CSS file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_js_file(value: str) -> str:
    """Normalize JS file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_json_file(value: str) -> str:
    """Normalize JSON file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xml_file(value: str) -> str:
    """Normalize XML file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_yaml_file(value: str) -> str:
    """Normalize YAML file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_csv_file(value: str) -> str:
    """Normalize CSV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tsv_file(value: str) -> str:
    """Normalize TSV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_sql_file(value: str) -> str:
    """Normalize SQL file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_markdown_file(value: str) -> str:
    """Normalize Markdown file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_pdf_file(value: str) -> str:
    """Normalize PDF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_doc_file(value: str) -> str:
    """Normalize DOC file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_docx_file(value: str) -> str:
    """Normalize DOCX file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xls_file(value: str) -> str:
    """Normalize XLS file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xlsx_file(value: str) -> str:
    """Normalize XLSX file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ppt_file(value: str) -> str:
    """Normalize PPT file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_pptx_file(value: str) -> str:
    """Normalize PPTX file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_zip_file(value: str) -> str:
    """Normalize ZIP file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_rar_file(value: str) -> str:
    """Normalize RAR file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_jpg_file(value: str) -> str:
    """Normalize JPG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_png_file(value: str) -> str:
    """Normalize PNG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_gif_file(value: str) -> str:
    """Normalize GIF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_bmp_file(value: str) -> str:
    """Normalize BMP file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tiff_file(value: str) -> str:
    """Normalize TIFF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mp4_file(value: str) -> str:
    """Normalize MP4 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mov_file(value: str) -> str:
    """Normalize MOV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_avi_file(value: str) -> str:
    """Normalize AVI file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_wmv_file(value: str) -> str:
    """Normalize WMV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_flv_file(value: str) -> str:
    """Normalize FLV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mkv_file(value: str) -> str:
    """Normalize MKV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_webm_file(value: str) -> str:
    """Normalize WEBM file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mp3_file(value: str) -> str:
    """Normalize MP3 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_wav_file(value: str) -> str:
    """Normalize WAV file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_aac_file(value: str) -> str:
    """Normalize AAC file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ogg_file(value: str) -> str:
    """Normalize OGG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_flac_file(value: str) -> str:
    """Normalize FLAC file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_m4a_file(value: str) -> str:
    """Normalize M4A file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_3gp_file(value: str) -> str:
    """Normalize 3GP file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg_file(value: str) -> str:
    """Normalize MPEG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg2_file(value: str) -> str:
    """Normalize MPEG2 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg4_file(value: str) -> str:
    """Normalize MPEG4 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_h264_file(value: str) -> str:
    """Normalize H.264 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_h265_file(value: str) -> str:
    """Normalize H.265 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_hevc_file(value: str) -> str:
    """Normalize HEVC file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_avc_file(value: str) -> str:
    """Normalize AVC file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vp8_file(value: str) -> str:
    """Normalize VP8 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vp9_file(value: str) -> str:
    """Normalize VP9 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vpx_file(value: str) -> str:
    """Normalize VPX file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_webp_file(value: str) -> str:
    """Normalize WebP file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_apng_file(value: str) -> str:
    """Normalize APNG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_svg_file(value: str) -> str:
    """Normalize SVG file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_eps_file(value: str) -> str:
    """Normalize EPS file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ttf_file(value: str) -> str:
    """Normalize TTF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_otf_file(value: str) -> str:
    """Normalize OTF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalizewoff_file(value: str) -> str:
    """Normalize WOFF file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalizewoff2_file(value: str) -> str:
    """Normalize WOFF2 file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_eot_file(value: str) -> str:
    """Normalize EOT file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ico_file(value: str) -> str:
    """Normalize ICO file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_cur_file(value: str) -> str:
    """Normalize CUR file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ani_file(value: str) -> str:
    """Normalize ANI file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_cursor_file(value: str) -> str:
    """Normalize cursor file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_icon_file(value: str) -> str:
    """Normalize icon file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_font_file(value: str) -> str:
    """Normalize font file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_binary_file(value: str) -> str:
    """Normalize binary file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_executable_file(value: str) -> str:
    """Normalize executable file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_archive_file(value: str) -> str:
    """Normalize archive file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_compressed_file(value: str) -> str:
    """Normalize compressed file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_encrypted_file(value: str) -> str:
    """Normalize encrypted file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_temporary_file(value: str) -> str:
    """Normalize temporary file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_cache_file(value: str) -> str:
    """Normalize cache file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_log_file(value: str) -> str:
    """Normalize log file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_backup_file(value: str) -> str:
    """Normalize backup file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_config_file(value: str) -> str:
    """Normalize config file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_database_file(value: str) -> str:
    """Normalize database file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_script_file(value: str) -> str:
    """Normalize script file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_template_file(value: str) -> str:
    """Normalize template file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_document_file(value: str) -> str:
    """Normalize document file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_spreadsheet_file(value: str) -> str:
    """Normalize spreadsheet file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_presentation_file(value: str) -> str:
    """Normalize presentation file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_image_file(value: str) -> str:
    """Normalize image file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_video_file(value: str) -> str:
    """Normalize video file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_audio_file(value: str) -> str:
    """Normalize audio file string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_text_file_content(value: str) -> str:
    """Normalize text file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_html_file_content(value: str) -> str:
    """Normalize HTML file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_css_file_content(value: str) -> str:
    """Normalize CSS file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_js_file_content(value: str) -> str:
    """Normalize JS file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_json_file_content(value: str) -> str:
    """Normalize JSON file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xml_file_content(value: str) -> str:
    """Normalize XML file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_yaml_file_content(value: str) -> str:
    """Normalize YAML file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_csv_file_content(value: str) -> str:
    """Normalize CSV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tsv_file_content(value: str) -> str:
    """Normalize TSV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_sql_file_content(value: str) -> str:
    """Normalize SQL file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_markdown_file_content(value: str) -> str:
    """Normalize Markdown file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_pdf_file_content(value: str) -> str:
    """Normalize PDF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_doc_file_content(value: str) -> str:
    """Normalize DOC file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_docx_file_content(value: str) -> str:
    """Normalize DOCX file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xls_file_content(value: str) -> str:
    """Normalize XLS file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_xlsx_file_content(value: str) -> str:
    """Normalize XLSX file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ppt_file_content(value: str) -> str:
    """Normalize PPT file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_pptx_file_content(value: str) -> str:
    """Normalize PPTX file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_zip_file_content(value: str) -> str:
    """Normalize ZIP file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_rar_file_content(value: str) -> str:
    """Normalize RAR file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_jpg_file_content(value: str) -> str:
    """Normalize JPG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_png_file_content(value: str) -> str:
    """Normalize PNG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_gif_file_content(value: str) -> str:
    """Normalize GIF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_bmp_file_content(value: str) -> str:
    """Normalize BMP file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_tiff_file_content(value: str) -> str:
    """Normalize TIFF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mp4_file_content(value: str) -> str:
    """Normalize MP4 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mov_file_content(value: str) -> str:
    """Normalize MOV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_avi_file_content(value: str) -> str:
    """Normalize AVI file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_wmv_file_content(value: str) -> str:
    """Normalize WMV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_flv_file_content(value: str) -> str:
    """Normalize FLV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mkv_file_content(value: str) -> str:
    """Normalize MKV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_webm_file_content(value: str) -> str:
    """Normalize WEBM file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mp3_file_content(value: str) -> str:
    """Normalize MP3 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_wav_file_content(value: str) -> str:
    """Normalize WAV file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_aac_file_content(value: str) -> str:
    """Normalize AAC file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ogg_file_content(value: str) -> str:
    """Normalize OGG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_flac_file_content(value: str) -> str:
    """Normalize FLAC file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_m4a_file_content(value: str) -> str:
    """Normalize M4A file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_3gp_file_content(value: str) -> str:
    """Normalize 3GP file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg_file_content(value: str) -> str:
    """Normalize MPEG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg2_file_content(value: str) -> str:
    """Normalize MPEG2 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_mpeg4_file_content(value: str) -> str:
    """Normalize MPEG4 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_h264_file_content(value: str) -> str:
    """Normalize H.264 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_h265_file_content(value: str) -> str:
    """Normalize H.265 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_hevc_file_content(value: str) -> str:
    """Normalize HEVC file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_avc_file_content(value: str) -> str:
    """Normalize AVC file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vp8_file_content(value: str) -> str:
    """Normalize VP8 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vp9_file_content(value: str) -> str:
    """Normalize VP9 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_vpx_file_content(value: str) -> str:
    """Normalize VPX file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_webp_file_content(value: str) -> str:
    """Normalize WebP file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_apng_file_content(value: str) -> str:
    """Normalize APNG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_svg_file_content(value: str) -> str:
    """Normalize SVG file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_eps_file_content(value: str) -> str:
    """Normalize EPS file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ttf_file_content(value: str) -> str:
    """Normalize TTF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_otf_file_content(value: str) -> str:
    """Normalize OTF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalizewoff_file_content(value: str) -> str:
    """Normalize WOFF file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalizewoff2_file_content(value: str) -> str:
    """Normalize WOFF2 file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_eot_file_content(value: str) -> str:
    """Normalize EOT file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ico_file_content(value: str) -> str:
    """Normalize ICO file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_cur_file_content(value: str) -> str:
    """Normalize CUR file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_ani_file_content(value: str) -> str:
    """Normalize ANI file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_cursor_file_content(value: str) -> str:
    """Normalize cursor file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_icon_file_content(value: str) -> str:
    """Normalize icon file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_font_file_content(value: str) -> str:
    """Normalize font file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_binary_file_content(value: str) -> str:
    """Normalize binary file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_executable_file_content(value: str) -> str:
    """Normalize executable file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_archive_file_content(value: str) -> str:
    """Normalize archive file content string."""
    if not value:
        return ""
    # Remove extra whitespace
    clean = re.sub(r"\s+", " ", value)
    return clean.strip()


def normalize_compressed_file_content(value: str) -> str:
    return value


@dataclass
class _LiteCell:
    text: str = ""
    links: List[_Link] = field(default_factory=list)
    images: List[str] = field(default_factory=list)


class _LiteHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: List[str] = []
        self.rows: List[List[_LiteCell]] = []
        self.current_row: List[_LiteCell] | None = None
        self.current_cell: Optional[_LiteCell] = None
        self.cell_parts: List[str] = []
        self.links: List[_Link] = []
        self.current_link_href: Optional[str] = None
        self.current_link_parts: List[str] = []
        self.image_urls: List[str] = []
        self.heading_parts: List[str] | None = None
        self.headings: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = _LiteCell()
            self.cell_parts = []
        elif tag == "a":
            self.current_link_href = attrs_dict.get("href", "")
            self.current_link_parts = []
        elif tag == "img":
            src = (
                attrs_dict.get("src")
                or attrs_dict.get("data-src")
                or attrs_dict.get("data-original")
                or attrs_dict.get("data-lazyload")
                or ""
            )
            if src:
                self.image_urls.append(src)
                if self.current_cell:
                    self.current_cell.images.append(src)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self.current_link_href is not None:
            link = _Link(href=self.current_link_href, text=compact_text(" ".join(self.current_link_parts)))
            if link.href:
                self.links.append(link)
                if self.current_cell:
                    self.current_cell.links.append(link)
            self.current_link_href = None
            self.current_link_parts = []
        elif tag in {"td", "th"} and self.current_cell is not None:
            self.current_cell.text = compact_text(" ".join(self.cell_parts))
            if self.current_row is not None:
                self.current_row.append(self.current_cell)
            self.current_cell = None
            self.cell_parts = []
        elif tag == "tr":
            if self.current_cell is not None:
                self.handle_endtag("td")
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_parts is not None:
            heading = compact_text(" ".join(self.heading_parts))
            if heading:
                self.headings.append(heading)
            self.heading_parts = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = unescape(data)
        if not compact_text(text):
            return
        self.text_parts.append(text)
        if self.current_cell is not None:
            self.cell_parts.append(text)
        if self.current_link_href is not None:
            self.current_link_parts.append(text)
        if self.heading_parts is not None:
            self.heading_parts.append(text)

    @property
    def text(self) -> str:
        return "\n".join(compact_text(part) for part in self.text_parts if compact_text(part))


def _repair_common_bad_html(html: str) -> str:
    text = html or ""
    return re.sub(r"(?<!<)/((?:td|th)|(?:tr|h)[1-6]|a)>", r"</\1>", text, flags=re.I)


def _parse_lite_html(html: str) -> _LiteHTMLParser:
    parser = _LiteHTMLParser()
    parser.feed(_repair_common_bad_html(html or ""))
    parser.close()
    return parser


def _cell_texts(cells: Iterable[_LiteCell | _Cell]) -> List[str]:
    return [compact_text(cell.text) for cell in cells if compact_text(cell.text)]


def _first_link(cells: Iterable[_LiteCell | _Cell]) -> Optional[_Link]:
    for cell in cells:
        for link in cell.links:
            if compact_text(link.href):
                return link
    return None


def _find_by_alias(fields: Dict[str, str], aliases: Iterable[str]) -> Optional[str]:
    for alias in aliases:
        value = find_by_alias(fields, (alias,))
        if value:
            return value
    return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _collect_payload_dicts(value: Any) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        title = first_non_blank(
            value.get("title"),
            value.get("projectName"),
            value.get("projectname"),
            value.get("proName"),
            value.get("name"),
            value.get("projName"),
        )
        item_id = first_non_blank(
            value.get("id"),
            value.get("projectId"),
            value.get("projectID"),
            value.get("proNo"),
            value.get("code"),
            value.get("projectCode"),
        )
        if title or item_id:
            collected.append(value)
        for child in value.values():
            collected.extend(_collect_payload_dicts(child))
    elif isinstance(value, list):
        for child in value:
            collected.extend(_collect_payload_dicts(child))
    return collected


def _payload_title(payload: Dict[str, Any]) -> str:
    return compact_text(
        first_non_blank(
            payload.get("title"),
            payload.get("projectName"),
            payload.get("projectname"),
            payload.get("proName"),
            payload.get("projName"),
            payload.get("name"),
        )
    )


def _payload_source_id(payload: Dict[str, Any]) -> str:
    return compact_text(
        first_non_blank(
            payload.get("id"),
            payload.get("projectId"),
            payload.get("projectID"),
            payload.get("proNo"),
            payload.get("code"),
            payload.get("projectCode"),
        )
    )


class CquaeBrowserFetcher:
    """浏览器渲染方案，用于解决 CQUAE 的 WAF/JS 挑战

    优化说明：
    - 复用浏览器上下文，避免每次请求都启动/关闭浏览器（耗时 3-5s 每次）
    - 用 domcontentloaded 替代 networkidle（耗时 10-30s 每次）
    - 调用 close() 释放浏览器资源
    """

    SESSION_ENDPOINT = f"{CQUAE_BASE_URL}/Customer/Head/GetSession.ashx"

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 0,
        profile_path: Optional[str] = None,
        settle_ms: int = 800,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.profile_path = profile_path
        self.settle_ms = max(0, int(settle_ms or 0))
        # Reusable browser resources
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._selenium_driver: Any = None

    @property
    def last_waf_cookies(self) -> Dict[str, str]:
        """Return WAF clearance cookies extracted from the last browser render."""
        return dict(getattr(self, "_last_cookies", {}))

    def _save_browser_cookies(self, context: Any) -> None:
        """Extract WAF cookies from a Playwright context and store them."""
        import re
        cookies: Dict[str, str] = {}
        try:
            for c in context.cookies():
                if c["name"] in ("__jsl_clearance_s", "ASP.NET_SessionId", "__jsluid_s"):
                    cookies[c["name"]] = c["value"]
        except Exception:
            pass
        self._last_cookies = cookies

    def close(self) -> None:
        """Release browser resources.  Call this after a batch is done."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._selenium_driver:
            try:
                self._selenium_driver.quit()
            except Exception:
                pass
            self._selenium_driver = None

    def fetch_html(self, url: str) -> str:
        # 先尝试 Playwright（复用已有浏览器）
        try:
            html = self._fetch_with_playwright(url)
            if html and len(html) > 10000 and not self._is_waf_page(html):
                return html
        except Exception:
            pass
        # Playwright 失败或返回 WAF 页面，降级到 Selenium
        return self._fetch_with_selenium(url)

    def _is_waf_page(self, html: str) -> bool:
        if not html or len(html) < 500:
            return True
        return any(marker in html for marker in WAF_MARKERS)

    def _ensure_playwright(self) -> Any:
        """Lazy-init & reuse a Playwright browser context."""
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Playwright is required for CQUAE browser fallback") from exc

        self._playwright = sync_playwright().start()
        p = self._playwright
        if self.profile_path:
            self._context = p.chromium.launch_persistent_context(
                self.profile_path,
                headless=self.headless,
                viewport={"width": 1365, "height": 900},
            )
        else:
            self._browser = p.chromium.launch(
                headless=self.headless,
                channel="chrome",
            )
            self._context = self._browser.new_context(
                viewport={"width": 1365, "height": 900},
            )
        return self._context

    def _fetch_with_playwright(self, url: str) -> str:
        ctx = self._ensure_playwright()
        page = ctx.new_page()
        try:
            if self.timeout_ms <= 0:
                page.set_default_timeout(0)
                page.set_default_navigation_timeout(0)
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms or 0)
            try:
                page.wait_for_selector("body", timeout=3000)
            except Exception:
                pass
            if self.settle_ms:
                page.wait_for_timeout(self.settle_ms)
            html = page.content()
            # 渲染成功后提取 WAF cookies，供 _fetch_html 的调用方注入 session
            self._save_browser_cookies(ctx)
        finally:
            page.close()
        return html

    def _is_waf_page(self, html: str) -> bool:
        if not html or len(html) < 500:
            return True
        return any(marker in html for marker in WAF_MARKERS)

    def _ensure_selenium(self) -> Any:
        """Lazy-init & reuse a Selenium driver."""
        if self._selenium_driver is not None:
            return self._selenium_driver
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        self._selenium_driver = webdriver.Chrome(options=options)
        return self._selenium_driver

    def _fetch_with_selenium(self, url: str) -> str:
        driver = self._ensure_selenium()
        driver.get(url)
        import time
        time.sleep(max(3, self.timeout_ms / 1000) if self.timeout_ms > 0 else 8)
        return driver.page_source

    def fetch_session(self) -> Dict[str, str]:
        """通过浏览器渲染获取完整 cookies（含 __jsl_clearance_s）"""
        html = self.fetch_html(CQUAE_BASE_URL)
        cookies = {}
        import re
        for m in re.finditer(r'__jsl_clearance_s\s*=\s*([^;]+)', html):
            cookies["__jsl_clearance_s"] = m.group(1).strip()
        return cookies

    def fetch_json(self, endpoint: str, params: Dict[str, str] | None = None,
                   referer: str = "") -> Optional[Dict[str, Any]]:
        """先渲染页面获取 cookies，再用 requests 调 JSON API"""
        import requests
        ts = int(time.time() * 1000)
        url = f"{self.SESSION_ENDPOINT}?&_={ts}&"
        if params:
            url += "&".join(f"{k}={v}" for k, v in params.items())

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer or CQUAE_BASE_URL + "/",
        }

        html = self.fetch_html(CQUAE_BASE_URL)
        session = requests.Session()
        session.headers.update(headers)
        for m in re.finditer(r'(?:(?:__jsl_clearance_s|ASP)\.(?:NET_SessionId|__jsluid_s))\s*=\s*([^;]+)', html, re.IGNORECASE):
            name = m.group(0).split("=")[0].strip()
            session.cookies.set(name, m.group(1).strip())

        resp = session.get(url, timeout=30)
        if resp.text.strip():
            try:
                return resp.json()
            except Exception:
                return {"raw_text": resp.text}
        return None


class CquaeAdapter:
    source_platform = CQUAE_PLATFORM
    source_site_name = CQUAE_DATA_SOURCE

    def build_list_url(
        self,
        *,
        page: int = 1,
        page_size: int = 15,
        project_id: int | str = 1,
        nt: int | str = 1,
        price_id: int | str = 32,
        type_id: int | str | None = None,
    ) -> str:
        params: Dict[str, Any] = {
            "q": "s",
            "projectID": project_id,
            "nt": nt,
            "priceID": price_id,
            "page": page,
            "pageSize": page_size,
        }
        if type_id is not None:
            params["type"] = type_id
        return f"{CQUAE_BASE_URL}{CQUAE_LIST_PATH}?{urlencode(params)}"

    def is_waf_challenge(self, html: str, status_code: Optional[int] = None) -> bool:
        if status_code == 521:
            return True
        haystack = (html or "").lower()
        return any(marker.lower() in haystack for marker in WAF_MARKERS)

    def parse_list_html(self, html: str, base_url: str = CQUAE_BASE_URL) -> List[CquaeListItem]:
        # 方案一：通过表格解析
        parser = _parse_lite_html(html)
        if parser.rows:
            headers = _cell_texts(parser.rows[0])
            rows = parser.rows[1:] if headers else parser.rows
            items: List[CquaeListItem] = []
            seen: set[str] = set()
            for row in rows:
                fields = fields_from_cells(row, headers) if headers else {}
                link = _first_link(row)
                source_item_id = extract_project_id(link.href if link else None)
                if not source_item_id:
                    continue
                if source_item_id in seen:
                    continue
                seen.add(source_item_id)
                title = compact_text(link.text if link else "") or _find_by_alias(fields, ("项目名称", "标的名称")) or best_title("", row)
                items.append(
                    CquaeListItem(
                        source_item_id=source_item_id,
                        source_url=urljoin(base_url, link.href if link else ""),
                        title=compact_text(title),
                        project_type=_find_by_alias(fields, ("项目类型", "资产类型", "标的类型")) or infer_project_type(title),
                        project_status=_find_by_alias(fields, ("项目状态", "交易状态", "状态")) or infer_project_status(" ".join(_cell_texts(row))),
                        price_raw=_find_by_alias(fields, ("挂牌价", "挂牌价格", "转让底价", "底价", "价格")),
                        deposit_raw=_find_by_alias(fields, ("保证金", "交易保证金")),
                        date_text=_find_by_alias(fields, ("披露起止日期", "挂牌起止日期", "公告起止日期", "报名时间")),
                        contact_info=_find_by_alias(fields, ("联系人", "联系电话")),
                        raw_fields=fields,
                        raw_text=" ".join(_cell_texts(row)),
                    )
                )
            if items:
                return items

        # 方案二：从 P_List_A 隐藏链接（div 渲染页面）提取
        items = []
        seen = set()
        for m in re.finditer(
            r'<a[^>]*href="(/Project/Show\?id=(\d+))"[^>]*class="P_List_A"[^>]*>([^<]+)</a>',
            html
        ):
            href, prj_id, title = m.group(1), m.group(2), m.group(3)
            if prj_id in seen:
                continue
            seen.add(prj_id)
            title = compact_text(title)
            items.append(
                CquaeListItem(
                    source_item_id=prj_id,
                    source_url=urljoin(base_url, href),
                    title=title,
                    project_type=infer_project_type(title),
                    raw_text=title,
                )
            )

        return items

    def parse_detail_html(
        self,
        html: str,
        *,
        url: str,
        list_item: Optional[CquaeListItem] = None,
    ) -> CquaeDetailBundle:
        parser = _parse_lite_html(html)
        key_values: Dict[str, str] = {}
        for row in parser.rows:
            cells = _cell_texts(row)
            if len(cells) < 2:
                continue
            if len(cells) == 2 and looks_like_label(cells[0]):
                add_key_value(key_values, cells[0], cells[1])
            elif len(cells) == 3 and looks_like_label(cells[1]):
                # 3 列行：冗余标签（如 rowspan 标题）+ 标签 + 值
                add_key_value(key_values, cells[1], cells[2])
            elif len(cells) % 2 == 0:
                for key, value in zip(cells[0::2], cells[1::2]):
                    if looks_like_label(key):
                        add_key_value(key_values, key, value)
        source_item_id = (
            compact_text(list_item.source_item_id if list_item else "")
            or extract_project_id(url)
            or _find_by_alias(key_values, ("项目编号", "标的编号", "项目代码"))
            or compact_text(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        )
        title = (
            first_non_blank(*parser.headings)
            or _find_by_alias(key_values, ("项目名称", "标的名称"))
            or compact_text(list_item.title if list_item else "")
            or source_item_id
        )
        image_urls = dedupe_texts(
            urljoin(url, image)
            for image in parser.image_urls
            if image and not image.lower().startswith("data:")
        )
        # 过滤站点全局 UI 图片（导航栏、页脚二维码、Logo、按钮等），只保留标的物相关图片
        _UI_IMAGE_PATTERNS = re.compile(
            r"/(?:(?:xmdhimgs|app)|Content2017/(?:2019Images|Images)|images)/|ba\.png$",
            re.I,
        )
        image_urls = [
            u for u in image_urls
            if not _UI_IMAGE_PATTERNS.search(u)
        ]
        # 后备：正则扫描 protocol-relative 和 files.cquae.com 标的图片
        if not any("Upload" in u or "files.cquae" in u for u in image_urls):
            for m in re.finditer(
                r'<img[^>]+src="\s*//(files\.cquae\.com/[^"]+(?:(?:jpg|png)|jpeg))"',
                html, re.IGNORECASE
            ):
                full_url = "https://" + m.group(1)
                if full_url not in image_urls:
                    image_urls.append(full_url)
        attachments = extract_attachments_from_raw_html(html, url) or extract_attachments(parser.links, url)
        return CquaeDetailBundle(
            source_item_id=source_item_id,
            source_url=url,
            title=compact_text(title),
            key_values=key_values,
            attachments=attachments,
            detail_text=parser.text,
            list_item=list_item,
            image_urls=image_urls,
            raw_html=html,
        )

    def classify_bundle(self, bundle: CquaeDetailBundle) -> str:
        common_type = infer_asset_type_from_sources(
            bundle.key_values,
            bundle.list_item,
            bundle.title,
            bundle.detail_text,
        )
        return classify_asset_group(common_type, bundle.title, bundle.detail_text)

    def map_common_candidates(self, bundle: CquaeDetailBundle) -> Dict[str, Any]:
        fields = bundle.key_values
        project_name = first_non_blank(
            _find_by_alias(fields, ("项目名称", "标的名称")),
            bundle.title,
            bundle.list_item.title if bundle.list_item else None,
        )
        asset_type = infer_asset_type_from_sources(fields, bundle.list_item, project_name, bundle.detail_text)
        asset_group = classify_asset_group(asset_type, project_name, bundle.detail_text)
        final_price = first_non_blank(
            _find_by_alias(fields, ("转让底价", "挂牌价", "挂牌价格", "底价", "价格", "评估价")),
            bundle.list_item.price_raw if bundle.list_item else None,
        )
        # Clean up meaningless price values like "-万元" (dash meaning "not set")
        price_is_valid = bool(final_price) and final_price.replace("-", "").strip() not in ("", "万元", "元", "万", "亿")
        contact = join_non_blank(
            _find_by_alias(fields, ("联系人", "交易机构联系人", "看货联系人")),
            _find_by_alias(fields, ("联系电话", "交易机构联系电话", "看货联系电话", "手机号码", "联系方式")),
            bundle.list_item.contact_info if bundle.list_item else None,
        )
        # Signup times — CQUAE uses "预披露起始/结束日期" (pre-disclosure) or
        # "挂牌起始/截止日期" (formal listing).
        signup_start = _find_by_alias(fields, (
            "预披露起始日期", "挂牌起始日期", "信息披露起始日期",
            "报名开始时间", "报名起始时间",
            "公告起止日期",
        ))
        signup_end = _find_by_alias(fields, (
            "预披露结束日期", "挂牌结束日期", "信息披露结束日期",
            "报名截止时间", "报名结束时间",
            "公告截止日期",
        ))
        # Parse date range fields like "公告起止日期 2026-07-08 至 2026-08-04"
        if not signup_start and not signup_end:
            date_range = _find_by_alias(fields, ("公告起止日期", "披露起止日期", "挂牌起止日期"))
            if date_range:
                parts = re.split(r"\s+至\s+|\s+~\s+|\s*-\s*", date_range)
                if len(parts) >= 2:
                    signup_start = parts[0].strip()
                    signup_end = parts[1].strip()

        # Asset location: for equity transfers the target company's address
        # ("住所") IS the asset location.  Also try the direct alias.
        asset_location = first_non_blank(
            _find_by_alias(fields, ("标的所在地", "标的坐落", "资产所在地", "项目所在地", "所在地", "住所")),
            _find_by_alias(fields, ("地址", "注册地址")),
        )
        # Project status: infer from the page context
        project_status = _find_by_alias(fields, ("项目状态", "交易状态", "状态"))
        if not project_status:
            haystack = " ".join(fields.values())
            if "预披露" in haystack or "预公告" in haystack:
                project_status = "预披露"
            elif "挂牌" in haystack or "公告" in haystack:
                project_status = "正式披露"

        # Start price — from transfer base price
        start_price = first_non_blank(
            _find_by_alias(fields, ("转让底价", "挂牌价", "转让底价（万元）", "挂牌价格")),
        )
        # Final price — for unsold items the transfer base price IS the current
        # price; for completed/sold items it would be the actual sale price.
        end_price = first_non_blank(
            _find_by_alias(fields, ("转让底价", "成交价", "成交价格", "最终成交价", "最终价格")),
            bundle.list_item.price_raw if bundle.list_item else None,
        )
        if not end_price:
            # CQUAE doesn't store a separate "成交价" field for open listings;
            # the transfer base price serves as both start and current price.
            end_price = start_price

        return {
            "source_platform": self.source_platform,
            "source_item_id": bundle.source_item_id,
            "source_url": bundle.source_url,
            "source_site_name": self.source_site_name,
            "asset_group": asset_group,
            "asset_type": asset_type,
            "asset_location": asset_location,
            "project_status": project_status,
            "auction_stage": _find_by_alias(fields, ("挂牌阶段", "竞价阶段", "交易阶段")),
            "data_source": self.source_site_name,
            "project_name": project_name,
            "signup_start_time": signup_start,
            "signup_end_time": signup_end,
            "disposal_party": _find_by_alias(fields, ("转让方", "处置方", "出让方", "委托方", "出租方")),
            "disposal_agency": _find_by_alias(fields, ("交易机构", "处置机构", "代理机构", "服务机构")),
            "start_price_raw": start_price if price_is_valid else None,
            "final_price_raw": final_price if price_is_valid else None,
            "contact_info": contact,
            "special_notice": _find_by_alias(fields, ("重要信息披露", "重大事项及其他披露内容", "特别告知", "特别提示", "风险提示")),
            "assessment_price_time": assessment_price_from_key_values(fields),
            "attachments_json": _json_dumps(bundle.attachments),
            "field_results": {},
        }

    def map_special_candidates(self, bundle: CquaeDetailBundle, asset_group: str) -> Dict[str, Any]:
        fields = bundle.key_values
        images = "; ".join(bundle.image_urls) if bundle.image_urls else None
        if asset_group == "real_estate":
            return {
                "certificate_no": clean_certificate_no(_find_by_alias(fields, ("权证编号", "产权证号", "不动产权证号"))),
                "building_area": _find_by_alias(fields, ("建筑面积", "证载建筑面积", "房屋面积")) or extract_area(bundle.detail_text),
                "property_use": _find_by_alias(fields, ("房产用途", "规划用途", "用途")),
                "use_term": _find_by_alias(fields, ("使用年限", "使用期限", "终止日期")),
                "property_location": _find_by_alias(fields, ("房产位置", "标的坐落", "坐落", "所在地")),
                "property_structure": _find_by_alias(fields, ("房产结构", "建筑结构")),
                "property_status": _find_by_alias(fields, ("房产状态", "现状", "租赁情况")),
                "disclosed_defects": _find_by_alias(fields, ("重要信息披露", "瑕疵", "风险提示")),
                "site_images": images,
                "property_type": _find_by_alias(fields, ("房产类型", "物业类型")),
                "asset_highlights": _find_by_alias(fields, ("资产亮点", "项目亮点")),
            }
        if asset_group == "debt":
            return {
                "debtor_name": _find_by_alias(fields, ("债务人", "主债务人", "借款人", "融资方")),
                "creditor": _find_by_alias(fields, ("债权人", "转让方")),
                "principal_balance": _find_by_alias(fields, ("本金余额", "借款本金", "债权本金")),
                "interest_balance": _find_by_alias(fields, ("利息余额", "欠息", "利息")),
                "claim_total": _find_by_alias(fields, ("债权总额", "债权金额", "合计金额")),
                "benchmark_date": _find_by_alias(fields, ("基准日", "债权基准日")),
                "collateral": _find_by_alias(fields, ("抵押物", "抵质押物", "担保物")),
                "guarantor": _find_by_alias(fields, ("保证人", "担保人")),
                "guarantee_method": _find_by_alias(fields, ("担保方式", "担保措施")),
                "litigation_status": _find_by_alias(fields, ("诉讼状态", "执行状态", "案件状态")),
            }
        if asset_group == "vehicle":
            return {
                "storage_location": _find_by_alias(fields, ("存放位置", "车辆所在地", "看样地点")),
                "vehicle_brand": _find_by_alias(fields, ("车型品牌", "品牌型号", "车辆品牌")),
                "plate_no": _find_by_alias(fields, ("车牌号", "牌照号")) or extract_plate_number(bundle.detail_text),
                "vehicle_status": _find_by_alias(fields, ("车辆状态", "车辆现状", "现状")),
                "vehicle_images": images,
            }
        if asset_group == "land":
            return {
                "certificate_no": clean_certificate_no(_find_by_alias(fields, ("权证编号", "土地证号", "不动产权证号"))),
                "land_area": _find_by_alias(fields, ("土地面积", "宗地面积")) or extract_area(bundle.detail_text),
                "land_use": _find_by_alias(fields, ("土地用途", "规划用途", "用途")),
                "use_term": _find_by_alias(fields, ("使用期限", "终止日期")),
                "land_location": _find_by_alias(fields, ("土地位置", "坐落", "所在地")),
                "site_images": images,
                "land_type": _find_by_alias(fields, ("土地类型", "土地性质")),
            }
        if asset_group == "equity":
            return {
                "transferor": _find_by_alias(fields, ("转让方", "出让方", "处置方")),
                "target_company": _find_by_alias(fields, ("标的企业", "企业名称", "公司名称")),
                "equity_ratio": find_by_alias_filtered(fields, ("股权比例", "持股比例", "出资比例"), forbidden=("名称", "股东名", "前十")),
                "company_nature": _find_by_alias(fields, ("企业性质", "公司性质", "企业类型", "经济类型")),
                "company_industry": _find_by_alias(fields, ("所属行业", "行业", "行业类别")),
                "business_scope": _find_by_alias(fields, ("经营范围", "主营业务", "经营业务")),
                "ownership_structure": _find_by_alias(fields, ("股权结构", "股东结构", "股东信息", "出资结构")),
                "financial_metrics": _find_by_alias(fields, ("财务指标", "财务数据", "财务状况", "主要财务指标")),
                "asset_valuation": _find_by_alias(fields, ("资产评估", "评估结果", "评估价值", "资产总额", "负债总额", "净资产")),
                "disclosure_items": _find_by_alias(fields, ("重大事项", "风险提示", "特别告知", "公示事项", "重要信息披露")),
                "attached_assets": _find_by_alias(fields, ("附带标的", "同步转让", "附带资产", "其他项目")),
            }
        if asset_group == "equipment":
            return {
                "storage_location": _find_by_alias(fields, ("存放位置", "设备所在地", "所在地", "存放地")),
                "equipment_status": _find_by_alias(fields, ("设备状态", "现状", "使用状态", "设备现状")),
                "disclosed_defects": _find_by_alias(fields, ("重要信息披露", "瑕疵", "风险提示")),
                "site_images": images,
                "equipment_type": _find_by_alias(fields, ("设备类型", "设备种类", "设备具体类型", "机器设备名称")),
            }
        if asset_group == "goods":
            return {
                "goods_category": _find_by_alias(fields, ("物资种类", "种类", "类别", "物资类别")),
                "goods_name": _find_by_alias(fields, ("物资名称", "标的名称", "存货名称", "货物名称")),
                "goods_location": _find_by_alias(fields, ("物资所在地", "存放位置", "所在地", "物资所在位置")),
                "goods_details": _find_by_alias(fields, ("物资详情", "详情", "规格", "数量", "存货详情")),
                "right_holder": _find_by_alias(fields, ("权利人", "所有权人", "产权人")),
                "disclosed_defects": _find_by_alias(fields, ("重要信息披露", "瑕疵", "风险提示")),
                "right_burden": _find_by_alias(fields, ("权利负担", "查封", "抵押", "他项权利")),
            }
        if asset_group == "usufruct":
            return {
                "right_category": _find_by_alias(fields, ("权益种类", "权益类型", "权利类型", "用益物权类型")),
                "subject_name": _find_by_alias(fields, ("标的名称", "项目名称", "名称")),
                "subject_location": _find_by_alias(fields, ("标的所在位置", "所在地", "位置", "标的坐落")),
                "subject_details": _find_by_alias(fields, ("标的物详情", "详情", "标的详情", "项目详情")),
                "valid_period": _find_by_alias(fields, ("有效期", "期限", "权利期限", "使用期限", "经营期限")),
                "original_right_holder": _find_by_alias(fields, ("原权利人", "权利人", "原产权人", "出让方")),
                "disclosed_defects": _find_by_alias(fields, ("重要信息披露", "瑕疵", "风险提示", "特别告知")),
                "right_burden": _find_by_alias(fields, ("权利负担", "查封", "抵押", "他项权利")),
            }
        if asset_group == "ip":
            return {
                "subject_name": _find_by_alias(fields, ("标的名称", "知识产权名称", "名称", "专利名称", "商标名称")),
                "certificate_no": _find_by_alias(fields, ("标的证号", "证书号", "登记号", "申请号", "专利号", "注册号")),
                "ip_type": _find_by_alias(fields, ("知产类型", "知识产权类型", "类型", "权利类型")),
                "ip_count": _find_by_alias(fields, ("知产数量", "项数", "数量", "知识产权数量")),
                "specific_category": _find_by_alias(fields, ("具体类别", "类别", "小类")),
                "right_holder": _find_by_alias(fields, ("权利人", "所有权人", "著作权人", "专利权人")),
                "subject_intro": _find_by_alias(fields, ("标的简介", "简介", "基本情况", "知识产权概况")),
                "disclosed_defects": _find_by_alias(fields, ("重要信息披露", "瑕疵", "风险提示")),
                "right_term": _find_by_alias(fields, ("权利期限", "有效期", "保护期限", "专利期限")),
            }
        return {
            "raw_detail_text": bundle.detail_text[:AI_DETAIL_TEXT_LIMIT] if bundle.detail_text else None,
            "raw_table_pairs_json": _json_dumps(fields),
        }

    def build_ai_context(self, bundle: CquaeDetailBundle) -> AIExtractionContext:
        asset_group = self.classify_bundle(bundle)
        attachment_lines = [
            f"{item.get('name') or ''} {item.get('url') or ''}".strip()
            for item in bundle.attachments
        ]
        detail_parts = [
            f"source_platform: {self.source_platform}",
            f"source_url: {bundle.source_url}",
            f"title: {bundle.title}",
            "key_values:",
            format_pairs(bundle.key_values),
            "attachments:",
            "\n".join(attachment_lines),
            "detail_text:",
            bundle.detail_text,
        ]
        return AIExtractionContext(
            html_key_values=bundle.key_values,
            detail_text="\n".join(part for part in detail_parts if part),
            notice_text="",
            image_urls=bundle.image_urls,
            asset_group=asset_group,
            paimai_id=f"{self.source_platform}:{bundle.source_item_id}",
        )
