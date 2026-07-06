"""天津交易集团 / 天津产权交易中心 适配器

基于 RESTful JSON API 的采集适配器。

已确认可用的 API 端点：
- 列表 API：GET /up/biz/project/anmuas/equity-trading/page?current=1&size=10
- 企业增资预披露详情：GET /transaction/biz/sa/increase/prepare/anmuas/get?viewId={UUID}
- 产权转让正式项目：GET /transaction/biz/sa/property/right/project/anmuas/get?viewId={UUID}
- 产权转让预披露：GET /transaction/biz/sa/property/right/prepare/anmuas/get?viewId={UUID}

鉴权要求：
- 所有 API 需要 headers: systemcode, uniflowsystemcode
"""
import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from jd.ai_extractor import AIExtractionContext


# ===== 常量 =====
TPRE_BASE_URL = "https://trade.tpre.cn"
TPRE_PLATFORM = "tpre"
TPRE_DATA_SOURCE = "天津交易集团/天津产权交易中心"

# 列表 API
TPRE_LIST_PATH = "/up/biz/project/anmuas/equity-trading/page"

# 详情 API 路径映射（frontend_path → backend_path）
# 注意：同一个 frontend_path 会随 systemCode 指向不同业务后端。
TPRE_DETAIL_API_MAP: Dict[str, str] = {
    "increase-prepare-project-details": "/transaction/biz/sa/increase/prepare/anmuas/get",
    # 以下两个需要结合 systemCode 路由，不能只按 detail_type 固定映射。
    "prepare-project-details": "",
    "formal-project-details": "",
    "investment-detail": "",  # 招商项目 — 待确认
}

TPRE_DETAIL_SYSTEM_API_MAP: Dict[Tuple[str, str], str] = {
    ("formal-project-details", "PROPERTY_RIGHT_TRANSFER"): "/transaction/biz/sa/property/right/project/anmuas/get",
    ("formal-project-details", "PROPERTY_RIGHT_TRANSFER_WEB"): "/transaction/biz/sa/property/right/project/anmuas/get",
    ("prepare-project-details", "PROPERTY_RIGHT_TRANSFER"): "/transaction/biz/sa/property/right/prepare/anmuas/get",
    ("prepare-project-details", "PROPERTY_RIGHT_TRANSFER_WEB"): "/transaction/biz/sa/property/right/prepare/anmuas/get",
    ("formal-project-details", "ENTERPRISE_ASSETS"): "/transaction/biz/sa/asset/project/anmuas/get",
    ("prepare-project-details", "ENTERPRISE_ASSETS"): "/transaction/biz/sa/asset/prepare/anmuas/get",
    ("formal-project-details", "ENTERPRISE_CAPITAL_INCREASE"): "/transaction/biz/sa/increase/project/anmuas/get",
    ("prepare-project-details", "ENTERPRISE_CAPITAL_INCREASE"): "/transaction/biz/sa/increase/prepare/anmuas/get",
    ("increase-prepare-project-details", "ENTERPRISE_CAPITAL_INCREASE"): "/transaction/biz/sa/increase/prepare/anmuas/get",
}

# 业务类型映射
BIZ_TYPE_LABELS: Dict[str, str] = {
    "PREPARE": "预披露",
    "FORMAL": "正式项目",
    "INVESTMENT": "招商项目",
}

# 业务系统映射
SYSTEM_NAME_MAP: Dict[str, str] = {
    "ENTERPRISE_CAPITAL_INCREASE": "企业增资",
    "PROPERTY_RIGHT_TRANSFER": "产权转让",
    "EQUITY_TRANSFER": "股权转让",
    "ASSET_TRANSFER": "资产转让",
    "DEBT_TRANSFER": "债权转让",
}

# 项目状态码映射
PROJECT_STATUS_MAP: Dict[str, str] = {
    "DISCLOSED": "已披露",
    "LISTING": "挂牌中",
    "BIDDING": "竞价中",
    "TRADING": "交易中",
    "FINISHED": "已结束",
    "SUSPENDED": "已中止",
    "WITHDRAWN": "已撤回",
}

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "systemcode": "PROPERTY_RIGHT_TRANSFER_WEB",
    "uniflowsystemcode": "INFORMATIONIZE",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
}


# ===== 工具函数 =====
def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return compact_text(text)


def first_non_blank(*values: Any) -> Optional[str]:
    for value in values:
        text = compact_text(value)
        if text:
            return text
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


def dict_from_deep_key(data: Any, key: str) -> Dict[str, Any]:
    found = deep_find(data, (key,))
    if isinstance(found, dict):
        return found
    if isinstance(found, str):
        try:
            parsed = json.loads(found)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def infer_asset_group(system_name: Optional[str], title: Optional[str] = None) -> str:
    haystack = compact_text(" ".join(filter(None, [system_name, title])))
    checks = [
        ("equity", ("股权", "产权转让", "企业增资")),
        ("debt", ("债权",)),
        ("real_estate", ("房产", "房地产", "房屋")),
        ("land", ("土地",)),
        ("equipment", ("设备",)),
        ("vehicle", ("车辆", "机动车")),
        ("usufruct", ("租赁", "经营权", "使用权")),
        ("goods", ("物资", "存货")),
    ]
    for group, keywords in checks:
        if any(kw in haystack for kw in keywords):
            return group
    return "other"


def infer_asset_type(system_name: Optional[str], title: Optional[str] = None) -> Optional[str]:
    name_map = {
        "产权转让": "产权转让",
        "企业增资": "企业增资",
        "股权转让": "股权转让",
        "资产转让": "资产转让",
        "债权转让": "债权",
        "房产租赁": "房产租赁",
    }
    haystack = compact_text(" ".join(filter(None, [system_name, title])))
    for key, label in name_map.items():
        if key in haystack:
            return label
    if system_name:
        return compact_text(system_name)
    return None


def extract_uuid_from_link(project_link: str) -> Optional[str]:
    """从 projectLink 中提取 UUID 格式的真实 ID"""
    if not project_link:
        return None
    parsed = urlparse(project_link)
    params = parse_qs(parsed.query)
    return params.get("id", [None])[0]


def extract_detail_type_from_link(project_link: str) -> str:
    """从 projectLink 中提取 detail_type"""
    if not project_link:
        return ""
    parsed = urlparse(project_link)
    return parsed.path.rstrip("/").rsplit("/", 1)[-1]


# ===== 数据结构 =====
@dataclass
class TpreListItem:
    source_item_id: str
    source_url: str
    title: str
    real_id: str = ""  # UUID 格式的真实详情ID
    detail_type: str = ""  # 详情类型: prepare-project-details, formal-project-details 等
    system_code: str = ""
    system_name: str = ""
    biz_type_code: str = ""
    biz_type_name: str = ""
    price_raw: Optional[str] = None
    price_unit: Optional[str] = None
    project_status: Optional[str] = None
    project_status_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    address_province: Optional[str] = None
    address_city: Optional[str] = None
    industry_name: Optional[str] = None
    rate: Optional[str] = None
    state_owned: bool = False
    views: int = 0
    raw_json: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TpreDetailBundle:
    source_item_id: str
    source_url: str
    title: str
    detail_json: Dict[str, Any]
    key_values: Dict[str, str]
    attachments: List[Dict[str, Any]]
    detail_text: str
    list_item: Optional[TpreListItem] = None
    image_urls: List[str] = field(default_factory=list)
    raw_html: str = ""


# ===== HTML 辅助解析器 =====
class _TpreHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "br", "dd", "div", "dl", "dt",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "p",
        "section", "table", "tbody", "tfoot", "th", "thead", "tr",
        "td", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: List[str] = []
        self.rows: List[List[str]] = []
        self.links: List[Dict[str, str]] = []
        self.images: List[str] = []
        self._skip_depth = 0
        self._current_row: Optional[List[str]] = None
        self._current_cell: Optional[List[str]] = None
        self._current_link: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
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
        elif tag in {"img", "source"}:
            for src_key in ("src", "data-src", "data-original"):
                url = compact_text(attrs_dict.get(src_key))
                if url:
                    self.images.append(url)

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
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None
        elif tag == "a" and self._current_link is not None:
            text = compact_text("".join(self._current_link.get("text_parts", [])))
            href = compact_text(self._current_link.get("href"))
            if href or text:
                self.links.append({"href": href, "text": text})
            self._current_link = None
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.text_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)
        if self._current_link is not None:
            self._current_link["text_parts"].append(data)

    @property
    def text(self) -> str:
        return compact_text("\n".join(p for p in self.text_parts if p))


def parse_html_fragments(html_fragments: List[str]) -> _TpreHTMLParser:
    parser = _TpreHTMLParser()
    for frag in html_fragments:
        if frag:
            parser.feed(frag)
    parser.close()
    return parser


FILE_LINK_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|zip|rar|pptx?)($|[?#])", re.IGNORECASE)


def extract_attachments_from_links(links: List[Dict[str, str]], base_url: str) -> List[Dict[str, Any]]:
    attachments: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        href = link.get("href", "")
        text = compact_text(link.get("text"))
        if not href:
            continue
        is_file = bool(FILE_LINK_RE.search(href)) or any(kw in text for kw in ("附件", "下载", "清单"))
        if not is_file:
            continue
        absolute_url = urljoin(base_url, href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        attachments.append({
            "name": text or absolute_url.rsplit("/", 1)[-1],
            "url": absolute_url,
            "source_payload_type": "detail_api",
            "source_path": "attachment_link",
            "source_excerpt": text,
        })
    return attachments


def extract_view_attachment_files(detail_data: Dict[str, Any], base_url: str) -> List[Dict[str, Any]]:
    """Extract files from TPRE's viewAttachment/attachmentTypes/attachments payload."""
    view_attachment = deep_find(detail_data, ("viewAttachment",))
    if isinstance(view_attachment, dict):
        view_attachment = [view_attachment]
    if not isinstance(view_attachment, list):
        return []

    files: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for group_index, group in enumerate(view_attachment):
        if not isinstance(group, dict):
            continue
        group_name = compact_text(group.get("businessTypeName") or group.get("businessTypeCode"))
        attachment_types = group.get("attachmentTypes") or group.get("attachmentTypeList") or []
        if isinstance(attachment_types, dict):
            attachment_types = [attachment_types]
        if not isinstance(attachment_types, list):
            continue

        for type_index, attachment_type in enumerate(attachment_types):
            if not isinstance(attachment_type, dict):
                continue
            type_name = compact_text(
                attachment_type.get("attachmentTypeName")
                or attachment_type.get("attachmentTypeCode")
            )
            attachments = attachment_type.get("attachments") or attachment_type.get("files") or []
            if isinstance(attachments, dict):
                attachments = [attachments]
            if not isinstance(attachments, list):
                continue

            for file_index, attachment in enumerate(attachments):
                if not isinstance(attachment, dict):
                    continue
                name = compact_text(
                    attachment.get("attachmentName")
                    or attachment.get("fileName")
                    or attachment.get("name")
                )
                pk_id = compact_text(
                    attachment.get("pkId")
                    or attachment.get("id")
                    or attachment.get("attachmentId")
                )
                url = compact_text(
                    attachment.get("fileUrl")
                    or attachment.get("url")
                    or attachment.get("downloadUrl")
                )
                if not url and pk_id:
                    url = f"/attachment/api/download/{pk_id}"
                if not url:
                    continue

                absolute_url = url if url.startswith("http") else urljoin(base_url, url)
                dedupe_key = absolute_url or pk_id
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                files.append({
                    "name": name or pk_id or absolute_url.rsplit("/", 1)[-1],
                    "url": absolute_url,
                    "source_payload_type": "detail_api.viewAttachment",
                    "source_path": (
                        f"viewAttachment[{group_index}]."
                        f"attachmentTypes[{type_index}].attachments[{file_index}]"
                    ),
                    "source_excerpt": join_non_blank(group_name, type_name, name) or name,
                    "attachment_id": pk_id,
                })
    return files


# ===== 核心适配器类 =====
class TpreAdapter:
    source_platform = TPRE_PLATFORM

    def __init__(
        self,
        *,
        base_url: str = TPRE_BASE_URL,
        session: Optional[requests.Session] = None,
        timeout: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._detail_api_cache: Dict[str, str] = dict(TPRE_DETAIL_API_MAP)

    # ── 列表 API ──
    def build_list_url(
        self,
        *,
        page: int = 1,
        size: int = 10,
        keyword: str = "",
        biz_type: str = "equity-trading",
    ) -> str:
        params = []
        if keyword:
            params.append(f"projectInformation={keyword}")
        params.append(f"current={page}")
        params.append(f"size={size}")
        return f"{self.base_url}/up/biz/project/anmuas/{biz_type}/page?{'&'.join(params)}"

    def fetch_list_api(
        self,
        page: int = 1,
        size: int = 10,
        keyword: str = "",
        biz_type: str = "equity-trading",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"current": page, "size": size}
        if keyword:
            params["projectInformation"] = keyword
        url = f"{self.base_url}/up/biz/project/anmuas/{biz_type}/page"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"TPRE list API error: code={data.get('code')}, msg={data.get('msg')}")
        return data

    def parse_list_response(self, api_data: Dict[str, Any]) -> List[TpreListItem]:
        items: List[TpreListItem] = []
        records = (api_data.get("data") or {}).get("records") or []
        for rec in records:
            item_id = compact_text(rec.get("projectCode") or rec.get("id") or "")
            if not item_id:
                continue

            project_link = compact_text(rec.get("projectLink") or "")
            real_id = extract_uuid_from_link(project_link) or ""
            detail_type = extract_detail_type_from_link(project_link)

            # 构建价格文本
            price_val = rec.get("price")
            price_raw = None
            if price_val is not None and str(price_val).strip():
                unit = compact_text(rec.get("priceUnit") or "万元")
                price_raw = f"{price_val} {unit}"

            # 状态映射
            status_code = compact_text(rec.get("projectStatus") or rec.get("projectStatusName") or "")
            status_name = PROJECT_STATUS_MAP.get(status_code, status_code)

            items.append(TpreListItem(
                source_item_id=item_id,
                source_url=project_link or f"{self.base_url}/transaction-view/index?bizTypeCode=CQZR",
                title=compact_text(rec.get("title") or ""),
                real_id=real_id,
                detail_type=detail_type,
                system_code=compact_text(rec.get("systemCode") or ""),
                system_name=compact_text(rec.get("systemName") or ""),
                biz_type_code=compact_text(rec.get("bizTypeCode") or ""),
                biz_type_name=compact_text(rec.get("bizTypeName") or ""),
                price_raw=price_raw,
                price_unit=compact_text(rec.get("priceUnit")),
                project_status=status_code,
                project_status_name=status_name,
                start_time=compact_text(rec.get("startTime")),
                end_time=compact_text(rec.get("endTime")),
                address_province=compact_text(rec.get("addressProvince")),
                address_city=compact_text(rec.get("addressCity")),
                industry_name=compact_text(rec.get("industryInvolvedName")),
                rate=compact_text(rec.get("rate")) if rec.get("rate") else None,
                state_owned=bool(rec.get("stateOwnedAssets")),
                views=int(rec.get("views") or 0),
                raw_json=dict(rec),
            ))
        return items

    # ── 详情 API ──
    def _get_detail_api_path(self, detail_type: str, system_code: str = "") -> str:
        """根据 detail_type 和 systemCode 获取后端 API 路径。"""
        system_code = compact_text(system_code)
        if system_code:
            system_path = TPRE_DETAIL_SYSTEM_API_MAP.get((detail_type, system_code))
            if system_path:
                return system_path
        if detail_type in self._detail_api_cache and self._detail_api_cache[detail_type]:
            return self._detail_api_cache[detail_type]
        return ""

    def fetch_detail_api(
        self,
        list_item: TpreListItem,
    ) -> Dict[str, Any]:
        """获取详情数据，优先用后端 API，失败则用列表数据做 fallback"""
        # 尝试后端 API
        api_path = self._get_detail_api_path(list_item.detail_type, list_item.system_code)
        if api_path and list_item.real_id:
            url = f"{self.base_url}{api_path}"
            try:
                resp = self.session.get(url, params={"viewId": list_item.real_id}, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") in (0, 200):
                    return {"_source": "api", "data": data}
            except Exception:
                pass  # 降级到列表数据

        # 降级：尝试直接访问 SPA 页面
        if list_item.source_url:
            try:
                resp = self.session.get(list_item.source_url, timeout=self.timeout)
                if resp.status_code == 200:
                    # SPA shell — 无法获取渲染后的数据，使用列表数据
                    pass
            except Exception:
                pass

        # 最终 fallback：用列表数据构建 bundle
        return {
            "_source": "list_fallback",
            "data": {"code": 0, "data": list_item.raw_json},
        }

    def parse_detail_response(
        self,
        api_data: Dict[str, Any],
        list_item: Optional[TpreListItem] = None,
    ) -> TpreDetailBundle:
        source = api_data.get("_source", "api")
        detail_data = api_data.get("data", {}).get("data") or api_data.get("data", {}) or {}

        item_id = list_item.source_item_id if list_item else ""
        title = first_non_blank(
            compact_text(detail_data.get("projectName") or detail_data.get("title")),
            list_item.title if list_item else None,
        )

        # 构建键值对
        key_values: Dict[str, str] = {}

        def merge_kv(label: str, *values: Any) -> None:
            clean = join_non_blank(*(compact_text(value) for value in values))
            if not clean:
                return
            existing = key_values.get(label)
            key_values[label] = join_non_blank(existing, clean) or clean

        # 从列表数据补充
        if list_item:
            for field, label in [
                ("source_item_id", "项目编号"),
                ("system_name", "业务系统"),
                ("biz_type_name", "项目阶段"),
                ("project_status_name", "项目状态"),
                ("price_raw", "挂牌价/转让底价"),
                ("start_time", "披露开始日期"),
                ("end_time", "披露截止日期"),
                ("industry_name", "所属行业"),
            ]:
                val = getattr(list_item, field, None)
                if val and compact_text(str(val)):
                    key_values.setdefault(label, compact_text(str(val)))
            # 地区
            loc = join_non_blank(list_item.address_province, list_item.address_city)
            if loc:
                key_values.setdefault("标的所在地", loc)
            # 比例
            if list_item.rate:
                key_values.setdefault("转让/持股比例", f"{list_item.rate}%")

        # 从详情 JSON 补充（当 source == "api" 时）
        if source == "api":
            json_key_mapping = {
                "projectName": "项目名称",
                "projectCode": "项目编号",
                "listingPrice": "挂牌价/转让底价",
                "transferPrice": "转让底价",
                "transferorName": "转让方",
                "orgName": "机构名称",
                "contactPerson": "联系人",
                "contactPhone": "联系电话",
                "assessmentPrice": "评估价",
                "assessmentDate": "评估基准日",
                "address": "地址",
                "industryName": "所属行业",
                "registeredCapital": "注册资本",
                "businessScope": "经营范围",
            }
            for json_key, label in json_key_mapping.items():
                val = deep_find(detail_data, (json_key,))
                if val and compact_text(str(val)):
                    key_values.setdefault(label, compact_text(str(val)))

            center_contact = dict_from_deep_key(detail_data, "centerContactInformation")
            merge_kv(
                "项目联系人",
                center_contact.get("handlerName"),
                center_contact.get("leaderName"),
            )
            merge_kv(
                "项目联系电话",
                center_contact.get("handlerTelephone"),
                center_contact.get("leaderTelephone"),
            )

            increase_company = dict_from_deep_key(detail_data, "saIncreaseCompanyInfo")
            merge_kv(
                "增资企业",
                increase_company.get("enterpriseName"),
                increase_company.get("companyName"),
                increase_company.get("targetEnterpriseName"),
            )
            merge_kv(
                "企业联系人",
                increase_company.get("contactName"),
                increase_company.get("contacts"),
            )
            merge_kv(
                "企业联系电话",
                increase_company.get("contactTelephone"),
                increase_company.get("contactPhone"),
                increase_company.get("phone"),
            )

            transferor_info = dict_from_deep_key(detail_data, "transferorInfo")
            merge_kv(
                "转让方",
                transferor_info.get("transferorName"),
                transferor_info.get("name"),
            )

        # 如果有 HTML 字段，解析提取更多键值对
        html_fields: List[str] = []
        for key in ("content", "description", "projectDescription", "detailContent",
                     "transferorInfo", "subjectCompanyInfo", "contactInfo",
                     "disclosureContent", "specialNotice"):
            val = detail_data.get(key)
            if val and isinstance(val, str) and ("<" in val or ">" in val):
                html_fields.append(val)

        parser = None
        detail_text = ""
        image_urls: List[str] = []
        if html_fields:
            parser = parse_html_fragments(html_fields)
            detail_text = parser.text
            # 从 HTML 表格行提取键值对
            for row in parser.rows:
                if len(row) < 2:
                    continue
                for i in range(0, len(row) - 1, 2):
                    k = compact_text(row[i]).rstrip(":：")
                    v = compact_text(row[i + 1])
                    if k and len(k) <= 30 and v:
                        key_values.setdefault(k, v)
            image_urls = [urljoin(self.base_url, img) for img in parser.images[:50] if img]
        elif source == "api":
            # 使用 JSON 数据构建文本
            lines = []
            for k, v in key_values.items():
                lines.append(f"{k}: {v}")
            detail_text = "\n".join(lines)

        # 提取附件
        attachments: List[Dict[str, Any]] = []
        if source == "api":
            attachments.extend(extract_view_attachment_files(detail_data, self.base_url))
            file_list = deep_find(detail_data, ("files", "attachments", "fileList", "enclosures"))
            if isinstance(file_list, list):
                seen_urls: set[str] = {compact_text(att.get("url")) for att in attachments if att.get("url")}
                for f in file_list:
                    if not isinstance(f, dict):
                        continue
                    fname = compact_text(f.get("fileName") or f.get("name") or "")
                    furl = compact_text(f.get("fileUrl") or f.get("url") or f.get("downloadUrl") or "")
                    absolute_furl = furl if furl.startswith("http") else urljoin(self.base_url, furl)
                    if furl and absolute_furl not in seen_urls:
                        seen_urls.add(absolute_furl)
                        attachments.append({
                            "name": fname or furl.rsplit("/", 1)[-1],
                            "url": absolute_furl,
                            "source_payload_type": "detail_api.files",
                            "source_path": f"files[{fname}]",
                            "source_excerpt": fname,
                        })
            if not attachments and parser:
                attachments = extract_attachments_from_links(parser.links, self.base_url)

        # 附件JSON（当详情API不可用时，从列表的附件URL构建）
        if not attachments and list_item and list_item.raw_json:
            raw_attachments = deep_find(list_item.raw_json, ("viewAttachment", "attachment"))
            if isinstance(raw_attachments, list):
                for att in raw_attachments:
                    if isinstance(att, dict):
                        fname = compact_text(att.get("name") or att.get("fileName") or "")
                        furl = compact_text(att.get("url") or att.get("fileUrl") or "")
                        if furl:
                            attachments.append({
                                "name": fname or furl.rsplit("/", 1)[-1],
                                "url": furl if furl.startswith("http") else urljoin(self.base_url, furl),
                                "source_payload_type": "list_json",
                                "source_path": "viewAttachment",
                                "source_excerpt": fname,
                            })

        source_url = list_item.source_url if list_item else ""
        return TpreDetailBundle(
            source_item_id=item_id,
            source_url=source_url,
            title=title or "",
            detail_json=dict(detail_data),
            key_values=key_values,
            attachments=attachments,
            detail_text=detail_text,
            list_item=list_item,
            image_urls=image_urls,
            raw_html="\n".join(html_fields),
        )

    # ── 兼容接口 ──
    def parse_list_html(self, html: str, base_url: str = "") -> List[TpreListItem]:
        raise NotImplementedError("TPRE list pages are SPA shells; use fetch_list_api() + parse_list_response().")

    def parse_detail_html(self, html: str, url: str = "", list_item: Optional[TpreListItem] = None) -> TpreDetailBundle:
        return TpreDetailBundle(
            source_item_id=list_item.source_item_id if list_item else "",
            source_url=url,
            title=list_item.title if list_item else "",
            detail_json={},
            key_values={},
            attachments=[],
            detail_text="",
            list_item=list_item,
        )

    # ── AI 上下文构建 ──
    def build_ai_context(self, bundle: TpreDetailBundle) -> AIExtractionContext:
        sections = [
            f"source_platform: {TPRE_PLATFORM}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.source_url}",
            f"title: {bundle.title}",
        ]
        if bundle.list_item and bundle.list_item.system_name:
            sections.append(f"system_name: {bundle.list_item.system_name}")
        if bundle.list_item and bundle.list_item.biz_type_name:
            sections.append(f"biz_type: {bundle.list_item.biz_type_name}")
        if bundle.key_values:
            sections.append("key_values:\n" + json.dumps(bundle.key_values, ensure_ascii=False, indent=2))
        if bundle.attachments:
            sections.append("attachments:\n" + json.dumps(bundle.attachments, ensure_ascii=False, indent=2))
        if bundle.detail_text:
            sections.append("detail_text:\n" + bundle.detail_text[:8000])

        asset_group = infer_asset_group(
            bundle.list_item.system_name if bundle.list_item else None,
            bundle.title,
        )
        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(sections)[:12000],
            notice_text="",
            image_urls=list(bundle.image_urls),
            asset_group=asset_group,
            paimai_id=f"{TPRE_PLATFORM}:{bundle.source_item_id}" if bundle.source_item_id else "",
        )

    # ── 公共字段映射 ──
    def map_common_candidates(self, bundle: TpreDetailBundle) -> Dict[str, Any]:
        common: Dict[str, Any] = {}
        results: Dict[str, Dict[str, Any]] = {}

        def set_field(field_key, value, source_type, source_path, excerpt=None, method="api_rule", confidence=None):
            common[field_key] = value
            results[field_key] = {
                "value": value,
                "status": "extracted" if compact_text(value) else "missing_on_page",
                "method": method if compact_text(value) else "not_found",
                "confidence": confidence if confidence is not None else (0.95 if compact_text(value) else 0.0),
                "source_payload_type": source_type,
                "source_path": source_path,
                "source_excerpt": excerpt or compact_text(value),
            }

        kv = bundle.key_values
        li = bundle.list_item

        title = first_non_blank(
            kv.get("项目名称"), kv.get("标的名称"),
            bundle.title,
            li.title if li else None,
        )
        status = first_non_blank(
            kv.get("项目状态"), kv.get("交易状态"),
            li.project_status_name if li else None,
        )
        location = first_non_blank(
            kv.get("标的所在地"), kv.get("地址"), kv.get("坐落"),
            join_non_blank(
                li.address_province if li else None,
                li.address_city if li else None,
            ) if li else None,
        )
        start_price = first_non_blank(kv.get("起拍价"), kv.get("起始价"), kv.get("挂牌价"))
        final_price = first_non_blank(
            kv.get("成交价"), kv.get("当前价"), kv.get("转让底价"),
            kv.get("挂牌价格"), kv.get("挂牌价/转让底价"),
            li.price_raw if li else None,
        )
        contact = join_non_blank(
            kv.get("联系人"),
            kv.get("联系电话"),
            kv.get("联系方式"),
            kv.get("项目联系人"),
            kv.get("项目联系电话"),
            kv.get("企业联系人"),
            kv.get("企业联系电话"),
            kv.get("经办人"),
            kv.get("经办电话"),
        )
        assessment = join_non_blank(kv.get("评估价"), kv.get("评估基准日"))
        notice = first_non_blank(kv.get("特别告知"), kv.get("特别提示"), kv.get("重大事项"), kv.get("风险提示"))
        disposal_party = first_non_blank(
            kv.get("转让方"),
            kv.get("委托方"),
            kv.get("出让方"),
            kv.get("融资方"),
            kv.get("增资企业"),
            kv.get("标的企业"),
        )
        disposal_agency = first_non_blank(kv.get("机构名称"), kv.get("交易机构"), kv.get("服务机构"))
        signup_start = first_non_blank(
            kv.get("挂牌开始日期"), kv.get("披露开始日期"),
            li.start_time if li else None,
        )
        signup_end = first_non_blank(
            kv.get("挂牌截止日期"), kv.get("披露截止日期"),
            li.end_time if li else None,
        )
        asset_group = infer_asset_group(li.system_name if li else None, title)
        asset_type = infer_asset_type(li.system_name if li else None, title)

        set_field("source_platform", TPRE_PLATFORM, "computed", "adapter", TPRE_PLATFORM, "constant", 1.0)
        set_field("source_item_id", bundle.source_item_id, "list_api", "projectCode", bundle.source_item_id, "api", 1.0)
        set_field("source_url", bundle.source_url, "api", "projectLink", bundle.source_url, "api", 1.0)
        set_field("asset_group", asset_group, "computed", "infer", asset_group, "inference", 0.85)
        set_field("asset_type", asset_type or "产权转让", "computed", "infer", asset_type or "产权转让", "inference", 0.85)
        set_field("project_name", title, "detail/list_api", "title", title, "api", 0.95)
        set_field("asset_location", location, "detail/list_api", "location", location, "api", 0.9)
        set_field("project_status", status, "detail/list_api", "status", status, "api", 0.9)
        set_field("start_price_raw", start_price, "detail_api", "start_price", start_price)
        set_field("final_price_raw", final_price, "detail/list_api", "final_price", final_price)
        set_field("contact_info", contact, "detail_api", "contact", contact)
        set_field("special_notice", notice, "detail_api", "notice", notice)
        set_field("assessment_price_time", assessment, "detail_api", "assessment", assessment)
        set_field("disposal_party", disposal_party, "detail_api", "disposal_party", disposal_party)
        set_field("disposal_agency", disposal_agency, "detail_api", "disposal_agency", disposal_agency)
        set_field("signup_start_time", signup_start, "detail/list_api", "signup_start", signup_start)
        set_field("signup_end_time", signup_end, "detail/list_api", "signup_end", signup_end)
        set_field("attachments_json", json.dumps(bundle.attachments, ensure_ascii=False), "detail_api", "attachments", "", "api", 0.9)
        set_field("data_source", TPRE_DATA_SOURCE, "computed", "adapter", TPRE_DATA_SOURCE, "constant", 1.0)

        common["field_results"] = results
        return common

    def classify_bundle(self, bundle: TpreDetailBundle) -> str:
        return infer_asset_group(
            bundle.list_item.system_name if bundle.list_item else None,
            bundle.title,
        )

    def map_special_candidates(self, bundle: TpreDetailBundle, asset_group: str) -> Dict[str, Any]:
        kv = bundle.key_values
        li = bundle.list_item
        values: Dict[str, Any] = {}
        images = "; ".join(bundle.image_urls[:80])

        if asset_group == "equity":
            values.update({
                "transferor": first_non_blank(kv.get("转让方"), kv.get("出让方"), kv.get("机构名称")),
                "target_company": first_non_blank(kv.get("标的企业"), kv.get("企业名称"), kv.get("公司名称")),
                "equity_ratio": first_non_blank(
                    f"{li.rate}%" if li and li.rate else None,
                    kv.get("持股比例"), kv.get("转让比例"), kv.get("股权比例"),
                    kv.get("转让/持股比例"),
                ),
                "company_nature": first_non_blank(kv.get("企业性质"), kv.get("企业类型")),
                "company_industry": first_non_blank(
                    li.industry_name if li else None,
                    kv.get("所属行业"), kv.get("行业"),
                ),
                "business_scope": kv.get("经营范围"),
                "disclosure_items": first_non_blank(kv.get("重大事项"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "real_estate":
            values.update({
                "building_area": first_non_blank(kv.get("建筑面积"), kv.get("面积")),
                "property_use": kv.get("房产用途"),
                "property_location": first_non_blank(kv.get("房产位置"), kv.get("坐落")),
                "property_status": kv.get("房产状态"),
                "disclosed_defects": first_non_blank(kv.get("瑕疵"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "land":
            values.update({
                "land_area": first_non_blank(kv.get("土地面积"), kv.get("宗地面积")),
                "land_use": kv.get("土地用途"),
                "land_location": first_non_blank(kv.get("土地位置"), kv.get("坐落")),
                "disclosed_defects": first_non_blank(kv.get("瑕疵"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "debt":
            values.update({
                "debtor_name": kv.get("主债务人") or kv.get("债务人"),
                "creditor": kv.get("债权人") or kv.get("转让方"),
                "disclosed_defects": first_non_blank(kv.get("瑕疵"), kv.get("特别提示")),
            })
        else:
            values.update({
                "raw_detail_text": (bundle.detail_text or "")[:12000],
                "raw_table_pairs_json": json.dumps(kv, ensure_ascii=False, sort_keys=True),
                "extracted_summary": first_non_blank(
                    kv.get("详细信息"), kv.get("重大事项"),
                    bundle.title,
                ),
                "site_images": images,
            })

        return {k: v for k, v in values.items() if compact_text(str(v or ""))}


# ===== 测试入口 =====
if __name__ == "__main__":
    adapter = TpreAdapter()

    print("=== TPRE 列表测试 ===")
    list_data = adapter.fetch_list_api(page=1, size=5)
    items = adapter.parse_list_response(list_data)
    total = (list_data.get("data") or {}).get("total", "?")
    print(f"总数: {total}, 本页: {len(items)} 条\n")

    for item in items:
        info = {
            "id": item.source_item_id,
            "title": item.title,
            "系统": item.system_name,
            "阶段": item.biz_type_name,
            "状态": item.project_status_name,
            "价格": item.price_raw,
            "时间": f"{item.start_time}~{item.end_time}",
            "地点": f"{item.address_province} {item.address_city}".strip(),
            "比例": item.rate,
        }
        for k, v in info.items():
            if v:
                print(f"  {k}: {v}")
        print()

    # 测试详情获取
    print("=== 测试详情获取 ===")
    if items:
        test_item = items[0]
        print(f"目标: [{test_item.source_item_id}] {test_item.title}")
        print(f"Detail Type: {test_item.detail_type}, Real ID: {test_item.real_id}")
        detail_data = adapter.fetch_detail_api(test_item)
        bundle = adapter.parse_detail_response(detail_data, list_item=test_item)
        print(f"来源: {detail_data.get('_source', 'unknown')}")
        print(f"键值对数: {len(bundle.key_values)}")
        for k, v in list(bundle.key_values.items())[:10]:
            print(f"  {k}: {v}")
        print(f"附件数: {len(bundle.attachments)}")
        common = adapter.map_common_candidates(bundle)
        print("\n公共字段:")
        for k, v in common.items():
            if k != "field_results":
                print(f"  {k}: {v}")
