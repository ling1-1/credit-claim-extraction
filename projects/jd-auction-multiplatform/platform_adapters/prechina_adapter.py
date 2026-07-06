"""贵州阳光产权交易所 适配器

数据来源：首页 HTML 表格（MetInfo CMS）
- 首页包含多个表格，每个表格代表一个业务类别
- 表格字段：项目编号、项目名称、挂牌价格(万元)、公告日期、截止日期
- 项目编号格式：GP-{D|B|C}-{CQ|ZL|ZZ|ZC|QT}-{年份}{序号}({序号})

业务类别：
- CQ = 股权转让
- ZL = 招租/租赁  
- ZZ = 增资扩股
- ZC = 资产处置/物资设备
- QT = 其他项目
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from jd.ai_extractor import AIExtractionContext


# ===== 常量 =====
PRECHINA_BASE_URL = "https://www.prechina.net"
PRECHINA_PLATFORM = "prechina"
PRECHINA_DATA_SOURCE = "贵州阳光产权交易所有限公司"
PRECHINA_HOME_URL = PRECHINA_BASE_URL + "/"

# 项目编号前缀含义
PROJECT_PREFIX_MAP: Dict[str, str] = {
    "GP": "贵州",       # Guizhou PreChina
}

# 业务类型编码
BIZ_TYPE_CODES: Dict[str, Tuple[str, str]] = {
    "CQ": ("equity", "股权转让"),
    "ZL": ("usufruct", "招租/资产招商"),
    "ZZ": ("equity", "增资扩股"),
    "ZC": ("goods", "资产处置/物资设备"),
    "QT": ("other", "其他项目"),
    "PC": ("debt", "破产清算"),
    "SWZC": ("real_estate", "实物资产"),  # 外部联合挂牌
    "QY": ("other", "企业资产"),
}

# 状态编码
STATUS_CODE_MAP: Dict[str, str] = {
    "D": "正式挂牌",
    "B": "预披露",
    "C": "正式公告",
    "F": "推介",
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ").replace("\u200c", "").replace("\u200d", "")
    return re.sub(r"\s+", " ", text).strip()


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


def parse_project_code(project_code: str) -> Dict[str, Optional[str]]:
    """解析项目编号，提取业务类型、状态等信息
    
    格式示例：GP-D-CQ-2026150(32), GP-B-CQ-2026148(30), SWZC260508H
    返回: {prefix, status, biz_type, year_seq, sub_seq}
    """
    code = compact_text(project_code)
    result: Dict[str, Optional[str]] = {"raw": code or None, "prefix": None, "status": None,
                                          "biz_type": None, "year_seq": None, "sub_seq": None}
    
    if not code:
        return result
    
    # 标准格式: GP-{status}-{biz_type}-{year}{seq}({sub_seq})
    m = re.match(r"^([A-Z]+)-([A-Z])-([A-Z]{2})-(\d{4})(\d+)\((\d+)\)$", code)
    if m:
        result["prefix"] = m.group(1)
        result["status"] = m.group(2)
        result["biz_type"] = m.group(3)
        result["year_seq"] = f"{m.group(4)}{m.group(5)}"
        result["sub_seq"] = m.group(6)
        return result
    
    # 特殊格式: SWZC + 数字 + 字母后缀 (外部联合挂牌)
    m2 = re.match(r"^(SWZC|QY)(\d+)([A-Z]*)$", code)
    if m2:
        result["prefix"] = m2.group(1)
        result["status"] = "D"  # 默认正式
        result["biz_type"] = m2.group(1)[:2] if len(m2.group(1)) > 2 else "QT"
        result["year_seq"] = m2.group(2)
        return result
    
    # 其他格式，尝试提取信息
    return result


def infer_asset_group(project_code: str, title: str = "") -> str:
    """根据项目编号和标题推断 asset_group"""
    parsed = parse_project_code(project_code)
    biz_type = parsed.get("biz_type") or ""
    
    if biz_type in BIZ_TYPE_CODES:
        group, _ = BIZ_TYPE_CODES[biz_type]
        if group != "other":
            return group
    
    # fallback: 从标题推断
    haystack = compact_text(f"{project_code} {title}")
    checks = [
        ("equity", ("股权", "产权转让", "企业增资", "持股")),
        ("debt", ("债权", "不良资产", "破产")),
        ("real_estate", ("房产", "房地产", "房屋", "住宅", "商铺", "厂房")),
        ("land", ("土地", "地块", "建设用地")),
        ("vehicle", ("车辆", "机动车", "汽车")),
        ("equipment", ("设备", "机器", "机械")),
        ("usufruct", ("租赁", "经营权", "使用权", "招租")),
        ("goods", ("物资", "存货", "资产处置", "废旧")),
    ]
    for group, keywords in checks:
        if any(kw in haystack for kw in keywords):
            return group
    return "other"


def infer_asset_type(project_code: str, title: str = "") -> Optional[str]:
    """推断 asset_type 文本标签"""
    parsed = parse_project_code(project_code)
    biz_type = parsed.get("biz_type") or ""
    
    if biz_type in BIZ_TYPE_CODES:
        _, label = BIZ_TYPE_CODES[biz_type]
        return label
    
    # fallback
    name_map = {
        "股权转让": "股权转让",
        "增资扩股": "增资扩股",
        "资产转让": "资产转让",
        "资产处置": "资产处置",
        "招商招租": "招商招租",
        "破产清算": "破产清算",
    }
    haystack = compact_text(title)
    for key, label in name_map.items():
        if key in haystack:
            return label
    return None


# ===== HTML 解析器 =====
class _PrechinaTableParser(HTMLParser):
    """解析贵州阳光产权交易所首页的表格数据"""
    
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []  # [table][row][cell_text]
        self.table_links: List[List[List[Dict[str, str]]]] = []  # [table][row][links]
        
        self._current_table_rows: List[List[str]] = []
        self._current_table_links: List[List[Dict[str, str]]] = []
        self._current_row_cells: List[str] = []
        self._current_row_links: List[Dict[str, str]] = []
        self._current_cell: str = ""
        self._current_cell_links: List[Dict[str, str]] = []
        self._in_td = False
        self._in_th = False
        self._in_table = False
        
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        tag_lower = tag.lower()
        
        if tag_lower == 'table':
            self._in_table = True
            self._current_table_rows = []
            self._current_table_links = []
        elif tag_lower in ('td', 'th') and self._in_table:
            self._in_td = True
            self._current_cell = ""
            self._current_cell_links = []
        elif tag_lower == 'tr' and self._in_table:
            self._current_row_cells = []
            self._current_row_links = []
        elif tag_lower == 'a' and self._in_td:
            href = attrs_dict.get('href', '')
            self._current_cell_links.append({"href": href, "text": ""})
            
    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        
        if tag_lower == 'table':
            self._in_table = False
            if self._current_table_rows:
                self.tables.append(self._current_table_rows)
                self.table_links.append(self._current_table_links)
            self._current_table_rows = []
            self._current_table_links = []
        elif tag_lower in ('td', 'th') and self._in_td:
            self._in_td = False
            cell_text = compact_text(self._current_cell)
            self._current_row_cells.append(cell_text)
            self._current_row_links.append(list(self._current_cell_links))
        elif tag_lower == 'tr' and self._in_table:
            if self._current_row_cells:
                self._current_table_rows.append(self._current_row_cells)
                self._current_table_links.append(self._current_row_links)
                
    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_cell += data
            
    def handle_entityref(self, name: str) -> None:
        if self._in_td:
            char = unescape(f"&{name};")
            self._current_cell += char


# ===== 数据结构 =====
@dataclass
class PrechinaListItem:
    source_item_id: str
    source_url: str
    title: str
    project_code_raw: str = ""
    price_raw: Optional[str] = None
    announce_date: Optional[str] = None
    end_date: Optional[str] = None
    biz_type_code: str = ""
    biz_type_name: str = ""
    status_code: str = ""
    status_name: str = ""
    table_index: int = -1  # 来源表格索引（用于分类）
    row_index: int = -1
    raw_fields: Dict[str, str] = field(default_factory=dict)


@dataclass
class PrechinaDetailBundle:
    source_item_id: str
    source_url: str
    title: str
    key_values: Dict[str, str]
    attachments: List[Dict[str, Any]]
    detail_text: str
    list_item: Optional[PrechinaListItem] = None
    image_urls: List[str] = field(default_factory=list)
    raw_html: str = ""


# ===== 核心 Adapter 类 =====
class PrechinaAdapter:
    """贵州阳光产权交易所适配器 — 基于 HTML 表格解析"""
    source_platform = PRECHINA_PLATFORM

    def __init__(
        self,
        *,
        base_url: str = PRECHINA_BASE_URL,
        session: Optional[requests.Session] = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def build_list_url(self, category: str = "default", page: int = 1) -> str:
        """构建列表页 URL（首页包含所有类别）"""
        return f"{self.base_url}/ejygg/index.jhtml?page={page}"

    # ---- 获取首页数据 ----
    def fetch_homepage(self) -> str:
        """获取首页 HTML"""
        resp = self.session.get(self.base_url, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp.text

    def parse_homepage_tables(self, html: str) -> Tuple[List[List[List[str]]], List[List[List[Dict[str, str]]]]]:
        """解析首页所有表格，返回 (tables, links)"""
        parser = _PrechinaTableParser()
        parser.feed(html)
        return parser.tables, parser.table_links

    def parse_list_from_homepage(
        self,
        html: str = "",
        *,
        min_columns: int = 3,
        min_rows: int = 2,
    ) -> List[PrechinaListItem]:
        """从首页 HTML 中提取项目列表
        
        Args:
            html: 首页 HTML 内容（为空则自动获取）
            min_columns: 最小列数过滤（表头通常>=4列）
            min_rows: 最小行数过滤（含表头）
        """
        if not html:
            html = self.fetch_homepage()
        
        tables, links = self.parse_homepage_tables(html)
        items: List[PrechinaListItem] = []
        
        for t_idx, (table, table_links) in enumerate(zip(tables, links)):
            # 跳过太小的表格
            if len(table) < min_rows:
                continue
            # 检查是否是项目表格（通过表头判断）
            header = table[0] if table else []
            header_str = "|".join(header).lower()
            
            # 必须包含关键字段才算项目表格
            is_project_table = any(kw in header_str for kw in [
                "项目编号", "项目名称", "挂牌价", "价格", "公告日期", "截止"
            ])
            if not is_project_table:
                continue
            
            # 构建列名映射（从表头行）
            col_map: Dict[int, str] = {}
            for c_idx, cell in enumerate(header):
                cell_clean = compact_text(cell).rstrip(":：")
                if cell_clean:
                    col_map[c_idx] = cell_clean
            
            # 提取数据行
            for r_idx, row in enumerate(table[1:], start=1):  # 跳过表头
                if not row:
                    continue
                
                # 获取各字段
                get_val = lambda idx: row[idx] if idx < len(row) else ""
                def _get_first_link(idx: int) -> Dict[str, str]:
                    """安全获取单元格中第一个链接的 {href, text}"""
                    try:
                        cell_links = table_links[r_idx][idx]
                        if isinstance(cell_links, list) and cell_links:
                            first = cell_links[0]
                            if isinstance(first, dict):
                                return first
                    except (IndexError, TypeError):
                        pass
                    return {}
                
                # 尝试多种列名匹配
                item_id = ""
                title = ""
                price = None
                announce_date = None
                end_date = None
                detail_url = ""
                
                for c_idx, col_name in col_map.items():
                    val = get_val(c_idx)
                    link_info = _get_first_link(c_idx)
                    
                    if "项目编号" in col_name:
                        item_id = val
                        if link_info and link_info.get('href'):
                            detail_url = urljoin(self.base_url, link_info['href'])
                    elif "项目名称" in col_name:
                        title = val
                        if not detail_url and link_info and link_info.get('href'):
                            detail_url = urljoin(self.base_url, link_info['href'])
                    elif any(k in col_name for k in ["挂牌价", "价格", "增资金额", "金额"]):
                        if val and val.strip() and val.strip() != '-':
                            price = val
                    elif "公告日期" in col_name or "发布时间" in col_name:
                        if val and val.strip() and val.strip() != '-':
                            announce_date = val
                    elif "截止" in col_name:
                        if val and val.strip() and val.strip() != '-':
                            end_date = val
                
                # 跳过无效行（没有编号或标题）
                if not item_id and not title:
                    continue
                
                # 解析项目编号
                parsed = parse_project_code(item_id)
                biz_type = parsed.get("biz_type") or ""
                status = parsed.get("status") or ""
                
                # 推断业务类型（从表头或编号）
                biz_type_name = ""
                if biz_type in BIZ_TYPE_CODES:
                    _, biz_type_name = BIZ_TYPE_CODES[biz_type]
                else:
                    # 尝试从表头推断
                    for bt, (_, btn) in BIZ_TYPE_CODES.items():
                        if btn in header_str:
                            biz_type = bt
                            biz_type_name = btn
                            break
                
                status_name = STATUS_CODE_MAP.get(status, "")
                
                # 构建原始字段映射
                raw_fields: Dict[str, str] = {}
                for c_idx, col_name in col_map.items():
                    if c_idx < len(row):
                        v = row[c_idx]
                        if v and v.strip():
                            raw_fields[col_name] = v
                
                items.append(PrechinaListItem(
                    source_item_id=item_id or title[:50],  # 用标题截断作为备用ID
                    source_url=detail_url or f"{self.base_url}/ejygg/index.jhtml",
                    title=title or item_id or "",
                    project_code_raw=item_id,
                    price_raw=price,
                    announce_date=announce_date,
                    end_date=end_date,
                    biz_type_code=biz_type,
                    biz_type_name=biz_type_name,
                    status_code=status,
                    status_name=status_name,
                    table_index=t_idx,
                    row_index=r_idx,
                    raw_fields=raw_fields,
                ))
        
        return items

    # ---- 详情页获取 ----
    def fetch_detail_page(self, url: str) -> str:
        """获取详情页 HTML"""
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp.text

    def parse_detail_html(
        self,
        html: str,
        url: str = "",
        list_item: Optional[PrechinaListItem] = None,
    ) -> PrechinaDetailBundle:
        """解析详情页 HTML，提取结构化数据"""
        # 详情页面通常包含更完整的项目信息
        # 这里做基本解析，后续可扩展
        
        # 清理HTML标签获取纯文本
        text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html or "")
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = compact_text(text)
        
        # 尝试提取键值对（从详情页的表格或列表中）
        key_values: Dict[str, str] = dict(list_item.raw_fields) if list_item else {}
        
        # 补充基本信息
        if list_item:
            if list_item.project_code_raw and "项目编号" not in key_values:
                key_values["项目编号"] = list_item.project_code_raw
            if list_item.title and "项目名称" not in key_values:
                key_values["项目名称"] = list_item.title
            if list_item.price_raw:
                key_values["挂牌价格"] = list_item.price_raw
            if list_item.announce_date:
                key_values["公告日期"] = list_item.announce_date
            if list_item.end_date:
                key_values["截止日期"] = list_item.end_date
            if list_item.biz_type_name:
                key_values["业务类型"] = list_item.biz_type_name
        
        # 提取附件链接
        attachments = self._extract_attachments(html, url)
        
        # 提取图片
        image_urls = self._extract_images(html)
        
        return PrechinaDetailBundle(
            source_item_id=list_item.source_item_id if list_item else "",
            source_url=url,
            title=list_item.title if list_item else "",
            key_values=key_values,
            attachments=attachments,
            detail_text=text[:10000],
            list_item=list_item,
            image_urls=image_urls,
            raw_html=html,
        )

    def _extract_attachments(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """从HTML中提取附件链接"""
        attachments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        
        # 查找文件下载链接
        file_pattern = re.compile(
            r'<a[^>]+href=["\']([^"\']*(?:\.pdf|\.docx?|\.xlsx?|\.zip|\.rar|\.pptx?))[^"\']*["\'][^>]*>'
            r'(.*?)</a>',
            re.IGNORECASE | re.DOTALL
        )
        
        for match in file_pattern.finditer(html):
            href = match.group(1)
            text = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            abs_url = urljoin(base_url, href)
            if abs_url not in seen:
                seen.add(abs_url)
                attachments.append({
                    "name": text or abs_url.rsplit("/", 1)[-1],
                    "url": abs_url,
                    "source_payload_type": "detail_html",
                    "source_path": "attachment_link",
                    "source_excerpt": text,
                })
        
        return attachments

    def _extract_images(self, html: str) -> List[str]:
        """从HTML中提取图片URL"""
        images: List[str] = []
        img_pattern = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
        seen: set[str] = set()
        
        for match in img_pattern.finditer(html):
            src = match.group(1)
            if src.startswith('data:'):
                continue
            abs_url = urljoin(self.base_url, src)
            if abs_url not in seen:
                seen.add(abs_url)
                images.append(abs_url)
        
        return images[:30]

    # ---- AI 上下文构建 ----
    def build_ai_context(self, bundle: PrechinaDetailBundle) -> AIExtractionContext:
        sections = [
            f"source_platform: {PRECHINA_PLATFORM}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.source_url}",
            f"title: {bundle.title}",
        ]
        if bundle.list_item:
            if bundle.list_item.biz_type_name:
                sections.append(f"biz_type: {bundle.list_item.biz_type_name}")
            if bundle.list_item.status_name:
                sections.append(f"status: {bundle.list_item.status_name}")
        if bundle.key_values:
            sections.append("key_values:\n" + json.dumps(bundle.key_values, ensure_ascii=False, indent=2))
        if bundle.attachments:
            sections.append("attachments:\n" + json.dumps(bundle.attachments, ensure_ascii=False, indent=2))
        if bundle.detail_text:
            sections.append("detail_text:\n" + bundle.detail_text[:8000])

        asset_group = infer_asset_group(bundle.source_item_id, bundle.title)
        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(sections)[:12000],
            notice_text="",
            image_urls=list(bundle.image_urls),
            asset_group=asset_group,
            paimai_id=f"{PRECHINA_PLATFORM}:{bundle.source_item_id}" if bundle.source_item_id else "",
        )

    # ---- 公共字段映射 ----
    def map_common_candidates(self, bundle: PrechinaDetailBundle) -> Dict[str, Any]:
        common: Dict[str, Any] = {}
        results: Dict[str, Dict[str, Any]] = {}

        def set_field(field_key, value, source_type, source_path, excerpt=None, method="html_rule", confidence=None):
            common[field_key] = value
            results[field_key] = {
                "value": value,
                "status": "extracted" if compact_text(value) else "missing_on_page",
                "method": method if compact_text(value) else "not_found",
                "confidence": confidence if confidence is not None else (0.95 if compact_text(value) else 0.0),
                "source_payload_type": source_type,
                "source_path": source_path,
                "source_excerpt": compact_text(excerpt if excerpt is not None else value),
            }

        kv = bundle.key_values
        li = bundle.list_item

        title = first_non_blank(kv.get("项目名称"), kv.get("标的名称"), bundle.title)
        status = first_non_blank(li.status_name if li else None, kv.get("项目状态"))
        price = first_non_blank(kv.get("挂牌价格"), kv.get("转让底价"), li.price_raw if li else None)
        contact = join_non_blank(kv.get("联系人"), kv.get("联系电话"))
        notice = first_non_blank(kv.get("特别告知"), kv.get("重大事项"))

        asset_group = infer_asset_group(bundle.source_item_id, bundle.title)
        asset_type = infer_asset_type(bundle.source_item_id, bundle.title)

        set_field("source_platform", PRECHINA_PLATFORM, "computed", "adapter.source_platform", PRECHINA_PLATFORM, "constant", 1.0)
        set_field("source_item_id", bundle.source_item_id, "list_html", "item.id", bundle.source_item_id, "html_rule", 1.0)
        set_field("source_url", bundle.source_url, "list_html", "item.url", bundle.source_url, "request", 1.0)
        set_field("asset_group", asset_group, "computed", "infer_asset_group", asset_group, "inference", 0.85)
        set_field("asset_type", asset_type or "其他", "computed", "infer_asset_type", asset_type, "inference", 0.85)
        set_field("project_name", title, "detail_html", "title", title, "html_rule", 0.95)
        set_field("project_status", status, "list_html", "status", status)
        set_field("final_price_raw", price, "list_html", "price", price)
        set_field("contact_info", contact, "detail_html", "contact", contact)
        set_field("special_notice", notice, "detail_html", "notice", notice)
        set_field("signup_start_time", li.announce_date if li else None, "list_html", "announce_date", li.announce_date if li else None)
        set_field("signup_end_time", li.end_date if li else None, "list_html", "end_date", li.end_date if li else None)
        set_field("attachments_json", json.dumps(bundle.attachments, ensure_ascii=False), "detail_html", "attachments", "", "html_rule", 0.9)
        set_field("data_source", PRECHINA_DATA_SOURCE, "computed", "adapter.data_source", PRECHINA_DATA_SOURCE, "constant", 1.0)

        common["field_results"] = results
        return common

    def classify_bundle(self, bundle: PrechinaDetailBundle) -> str:
        return infer_asset_group(bundle.source_item_id, bundle.title)

    def map_special_candidates(self, bundle: PrechinaDetailBundle, asset_group: str) -> Dict[str, Any]:
        kv = bundle.key_values
        values: Dict[str, Any] = {}
        images = "; ".join(bundle.image_urls[:80])

        if asset_group == "equity":
            values.update({
                "transferor": first_non_blank(kv.get("转让方"), kv.get("出让方")),
                "target_company": first_non_blank(kv.get("标的企业"), kv.get("企业名称")),
                "equity_ratio": first_non_blank(kv.get("持股比例"), kv.get("转让比例")),
                "company_nature": first_non_blank(kv.get("企业性质"), kv.get("企业类型")),
                "disclosure_items": first_non_blank(kv.get("重大事项"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "usufruct":
            values.update({
                "property_location": first_non_blank(kv.get("坐落"), kv.get("位置")),
                "property_use": kv.get("用途"),
                "site_images": images,
            })
        elif asset_group == "real_estate":
            values.update({
                "building_area": first_non_blank(kv.get("建筑面积"), kv.get("面积")),
                "property_location": first_non_blank(kv.get("坐落"), kv.get("位置")),
                "site_images": images,
            })
        else:
            values.update({
                "raw_detail_text": (bundle.detail_text or "")[:12000],
                "raw_table_pairs_json": json.dumps(kv, ensure_ascii=False, sort_keys=True),
                "site_images": images,
            })

        return {k: v for k, v in values.items() if compact_text(str(v or ""))}


# ===== 测试入口 =====
if __name__ == "__main__":
    adapter = PrechinaAdapter()

    print("=== PreChina (贵州阳光产权) 列表测试 ===")
    try:
        html = adapter.fetch_homepage()
        print(f"首页 HTML 长度: {len(html)}")

        items = adapter.parse_list_from_homepage(html)
        print(f"\n共提取到 {len(items)} 条项目记录\n")

        # 按业务类型分组统计
        from collections import Counter
        type_counts = Counter((i.biz_type_name or "未知") for i in items)
        print("按业务类型分布:")
        for t, c in type_counts.most_common():
            print(f"  {t}: {c}")

        print("\n--- 前10条项目 ---")
        for i, item in enumerate(items[:10]):
            print(f"\n[{i+1}] [{item.source_item_id}] {item.title}")
            print(f"    类型: {item.biz_type_name} | 状态: {item.status_name}")
            print(f"    价格: {item.price_raw} | 公告: {item.announce_date} ~ 截止: {item.end_date}")
            print(f"    URL: {item.source_url[:80]}")

            # 尝试获取详情
            if item.source_url and item.source_url != f"{adapter.base_url}/ejygg/index.jhtml":
                try:
                    detail_html = adapter.fetch_detail_page(item.source_url)
                    bundle = adapter.parse_detail_html(detail_html, item.source_url, list_item=item)
                    print(f"    详情页长度: {len(detail_html)}, 键值对: {len(bundle.key_values)}, 附件: {len(bundle.attachments)}")
                except Exception as e:
                    print(f"    详情获取失败: {e}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
