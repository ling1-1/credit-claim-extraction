"""广西联合产权交易所 适配器

数据来源：PHPCMF CMS httpapi (HTML 片段) + ljs.gxcq.com.cn RESTful API (详情)
- 列表数据: www.gxcq.com.cn → /index.php?s=httpapi&id=3 (返回 project_html HTML片段)
- 详情API:   ljs.gxcq.com.cn/api/dscq-project/* (RESTful JSON)
- 前端SPA:    ljs.gxcq.com.cn/#/projectDetail/{assetsId}.html

已验证的 API 端点（2026-06-27 抓包验证）:

  列表 API (www.gxcq.com.cn, PHPCMF httpapi):
    GET /index.php?s=httpapi&id=3&appid=1&appsecret=PHPCMFA0EF8F01A56FF
        &data[assetsTypeParent]={type}&data[cate_id]={cid}
    返回: {code: 1, data: {project_html: "<li>...<li>..."}}
    HTML格式: <li><a href="{detail_url}"><h1>{title}</h1>
               <span>{price}</span><span>{end_time}</span><span>{status}</span></a></li>

  详情 API (ljs.gxcq.com.cn, header: client-id=gxcq):
    GET /api/dscq-project/assets-detail/base?assetsId={id}
    GET /api/dscq-project/announcement-detail/...?assetsId={id}  (20+子端点)

  assetsTypeParent 类型码:
    ZQ=债权, GQ=股权, FC=房产, TD=土地, CL=车辆, SB=设备, WZ=物资
"""


import json
import os
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests

from jd.ai_extractor import AIExtractionContext, AI_DETAIL_TEXT_LIMIT


# ===== 常量 =====
GXCQ_BASE_URL = "https://www.gxcq.com.cn"
GXCQ_DETAIL_API_BASE = "https://ljs.gxcq.com.cn"
GXCQ_PLATFORM = "gxcq"
GXCQ_DATA_SOURCE = "广西联合产权交易所集团有限责任公司"

# 列表 API 配置 (已验证: id=2=搜索API, 支持分页并返回 project_total; id=3=列表HTML, 不支持分页)
GXCQ_LIST_API_PATH = "/index.php"
GXCQ_LIST_APP_ID = "1"
GXCQ_LIST_APP_SECRET = os.getenv("GXCQ_LIST_APP_SECRET", "PHPCMFA0EF8F01A56FF")  # PHPCMF appsecret
GXCQ_LIST_API_ID = "2"  # 2=搜索API(支持分页); 3=项目列表HTML(不支持分页)

# assetsTypeParent 映射
ASSETS_TYPE_PARENT_MAP: Dict[str, str] = {
    "ZQ": ("debt", "债权"),
    "GQ": ("equity", "股权"),
    "FC": ("real_estate", "房产"),
    "TD": ("land", "土地"),
    "CL": ("vehicle", "车辆"),
    "SB": ("equipment", "设备"),
    "WZ": ("goods", "物资"),
}

# 默认 cate_id (分类ID)
DEFAULT_CATE_IDS: Dict[str, int] = {
    "ZQ": 154,  # 债权
    "GQ": 154,  # 股权(共用同一分类页)
    "FC": 154,  # 房产
}

# 详情 API 子端点 (按优先级排序)
DETAIL_API_ENDPOINTS: List[str] = [
    "assets-detail/base",                          # 基础信息
    "normal-announcement-detail/top-info",         # 顶部概要
    "announcement-detail/assets-house",            # 资产/标的物
    "announcement-detail/transferor-info",         # 转让方信息
    "normal-announcement-detail/assets-enterpris", # 标的企业
    "normal-announcement-detail/financial-audit",  # 财务审计
    "announcement-detail/dispose-method",          # 处置方式
    "announcement-detail/buyer-aptitude-content",  # 受让方资格
    "announcement-detail/transferor-promise",      # 转让方承诺
    "announcement-detail/trade-notice",            # 交易须知
    "announcement-detail/risk-warning-notice",     # 风险提示
    "announcement-detail/auction-announcement",    # 竞价公告
    "announcement-detail/bidding-notice-ty",       # 招标公告
    "announcement-detail/publish-accessory",       # 附件清单
    "announcement-detail/disclose-info",           # 披露信息
    "announcement-detail/enroll-projmaterial",     # 报名材料
    "announcement-detail/supplementary-disclosure",# 补充披露
    "announcement-detail/evaluation-info",         # 评估信息
    "announcement-detail/catalog-content",        # 目录内容
]

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{GXCQ_BASE_URL}/",
    "Origin": GXCQ_BASE_URL,
}

DETAIL_API_HEADERS: Dict[str, str] = {
    **DEFAULT_HEADERS,
    "client-id": "gxcq",
    "Referer": f"{GXCQ_DETAIL_API_BASE}/",
}


# ===== 工具函数 =====
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


def infer_asset_group(
    assets_type_parent: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    if assets_type_parent and assets_type_parent in ASSETS_TYPE_PARENT_MAP:
        group, _ = ASSETS_TYPE_PARENT_MAP[assets_type_parent]
        return group
    haystack = compact_text(title or "")
    checks = [
        ("equity", ("股权", "产权转让", "企业增资")),
        ("debt", ("债权", "不良资产", "破产", "债权转让")),
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


def infer_asset_type(
    assets_type_parent: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    if assets_type_parent and assets_type_parent in ASSETS_TYPE_PARENT_MAP:
        _, label = ASSETS_TYPE_PARENT_MAP[assets_type_parent]
        return label
    return "产权转让"


# ===== 数据结构 =====
@dataclass
class GxcqListItem:
    source_item_id: str          # assetsId (从详情URL提取)
    source_url: str               # 详情页 URL
    title: str
    price_raw: Optional[str] = None
    price_num: Optional[float] = None
    evaluation_price: Optional[str] = None
    deposit_amount: Optional[str] = None
    project_status: Optional[str] = None
    end_time: Optional[str] = None
    signup_deadline: Optional[str] = None
    industry_type: Optional[str] = None
    assets_type_parent: Optional[str] = None
    region: Optional[str] = None
    contact_person: Optional[str] = None
    contact_phone: Optional[str] = None
    raw_json: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GxcqDetailBundle:
    source_item_id: str
    source_url: str
    title: str
    key_values: Dict[str, str]
    attachments: List[Dict[str, Any]]
    detail_text: str
    list_item: Optional[GxcqListItem] = None
    image_urls: List[str] = field(default_factory=list)
    detail_json: Dict[str, Any] = field(default_factory=dict)
    raw_html: str = ""


# ===== 核心 Adapter 类 =====
class GxcqBrowserFetcher:
    """使用 Selenium 渲染 GXCQ 列表页，支持翻页"""
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless

    def fetch_list_html(self, page: int = 1, cate_id: int = 154) -> str:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        import time
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=options)
        try:
            driver.get(f"{GXCQ_BASE_URL}/list-{cate_id}.html")
            time.sleep(3)
            if page > 1:
                try:
                    page_btn = driver.find_element(By.CSS_SELECTOR, f"a[data-page='{page}']")
                    driver.execute_script("arguments[0].click();", page_btn)
                    time.sleep(2)
                except Exception:
                    pass
            return driver.page_source
        finally:
            driver.quit()

    def parse_list_items(self, html: str) -> List[GxcqListItem]:
        """解析列表页 HTML"""
        items: List[GxcqListItem] = []
        import re
        # 匹配项目链接: 支持 /projectDetail/xxx.html 和 #/projectDetail/xxx.html 格式
        link_pattern = re.compile(
            r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']*?projectDetail/[a-f0-9]+\.html)["\'][^>]*>(.*?)</a>',
            re.DOTALL | re.I
        )
        for m in link_pattern.finditer(html):
            link_url = m.group(1)
            inner_html = m.group(2)
            # 构建完整 URL
            if link_url.startswith("http"):
                detail_url = link_url.replace("http://", "https://")
            elif link_url.startswith("/"):
                detail_url = f"https://ljs.gxcq.com.cn{link_url}"
            else:
                detail_url = f"https://ljs.gxcq.com.cn/{link_url}"
            # 提取标题
            title = compact_text(re.sub(r'<[^>]+>', '', inner_html))[:100]
            if not title:
                continue
            # 提取 assetsId
            id_m = re.search(r'/projectDetail/([a-f0-9]+)\.html', link_url, re.I)
            item_id = id_m.group(1) if id_m else ""
            items.append(GxcqListItem(
                source_item_id=item_id or title[:30],
                source_url=detail_url,
                title=title,
            ))
        return items
    """广西联合产权交易所适配器
    
    列表: PHPCMF httpapi (id=3) 返回 HTML 项目片段
    详情: ljs.gxcq.com.cn dscq-project RESTful API
    """

    source_platform = GXCQ_PLATFORM

    def __init__(
        self,
        *,
        base_url: str = GXCQ_BASE_URL,
        detail_base_url: str = GXCQ_DETAIL_API_BASE,
        session: Optional[requests.Session] = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.detail_base_url = detail_base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    # ── 列表 API ──
    def build_list_url(
        self,
        *,
        page: int = 1,
        size: int = 10,
        assets_type_parent: str = "",
        cate_id: int = 0,
    ) -> str:
        """构建列表 API URL (PHPCMF httpapi, id=2)

        不传 assets_type_parent / cate_id 则不筛选，返回全部项目。
        """
        params = {
            "s": "httpapi",
            "id": GXCQ_LIST_API_ID,
            "appid": GXCQ_LIST_APP_ID,
            "appsecret": GXCQ_LIST_APP_SECRET,
            "data[page]": str(page),
            "data[pagesize]": str(size),
        }
        if assets_type_parent:
            params["data[assetsTypeParent]"] = assets_type_parent
        if cate_id:
            params["data[cate_id]"] = str(cate_id)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}{GXCQ_LIST_API_PATH}?{query}"

    def fetch_list_api(
        self,
        *,
        page: int = 1,
        size: int = 10,
        assets_type_parent: str = "",
        cate_id: int = 0,
    ) -> Dict[str, Any]:
        """获取项目列表 (PHPCMF httpapi, id=2)

        不传 assets_type_parent / cate_id 则不筛选，返回全部项目。
        
        Returns:
            {code: 1, msg: "...", data: {project_html: "<li>...</li>..."}}
        """
        url = self.build_list_url(
            page=page, size=size,
            assets_type_parent=assets_type_parent,
            cate_id=cate_id,
        )
        resp = self.session.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 1 and data.get("code") != "1":
            raise RuntimeError(
                f"GXCQ list API error: code={data.get('code')}, "
                f"msg={data.get('msg', '')}"
            )
        return data

    def parse_list_response(self, api_data: Dict[str, Any]) -> List[GxcqListItem]:
        """解析列表 API 响应

        从 project_html 字段的 HTML 片段中提取项目列表。
        HTML 格式:
        <li><a href="{url}" ...>
          <h1>{title}</h1>
          <span>{price}</span>
          <span>{end_time}</span>
          <span>{status}</span>
        </a></li>
        """
        raw_data = api_data.get("data")
        project_html = ""
        if isinstance(raw_data, dict):
            project_html = raw_data.get("project_html", "")
        elif isinstance(raw_data, str):
            project_html = raw_data

        if not project_html:
            return []

        items: List[GxcqListItem] = []

        # 解析每个 <li> 条目
        li_pattern = re.compile(
            r'<li[^>]*>\s*<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*</li>',
            re.DOTALL | re.IGNORECASE,
        )

        for m in li_pattern.finditer(project_html):
            link_url = m.group(1).strip()
            inner_html = m.group(2)

            # 提取标题 (<h1>)
            title_m = re.search(r'<h\d[^>]*>([^<]+)</h\d>', inner_html, re.IGNORECASE | re.DOTALL)
            title = compact_text(title_m.group(1)) if title_m else ""

            if not title:
                continue

            # 提取所有 <span> 内容作为字段
            spans = re.findall(r'<span[^>]*>([^<]*)</span>', inner_html, re.IGNORECASE | re.DOTALL)
            
            price_raw = None
            price_num = None
            end_time = None
            status = None
            
            # 根据位置推断 span 含义
            # id=2 格式: [项目编号, 价格, 时间, 状态] (4个span)
            # id=3 格式: [价格, 时间, 状态] (3个span)
            span_offset = 0
            if len(spans) == 4:
                # id=2 格式: 第一个span是项目编号, 跳过
                span_offset = 1
            
            price_idx = 0 + span_offset
            time_idx = 1 + span_offset
            status_idx = 2 + span_offset
            
            if len(spans) > price_idx:
                price_val = compact_text(spans[price_idx])
                if price_val and price_val != '-':
                    price_raw = price_val
                    try:
                        price_num = float(re.sub(r'[^\d.]', '', price_val))
                    except ValueError:
                        price_num = None
            if len(spans) > time_idx:
                time_val = compact_text(spans[time_idx])
                if time_val and time_val != '-':
                    end_time = time_val
            if len(spans) > status_idx:
                status_val = compact_text(spans[status_idx])
                if status_val and status_val != '-':
                    status = status_val

            # 从 URL 提取 assetsId
            url_id_m = re.search(r'/projectDetail/([a-f0-9]+)\.html', link_url, re.IGNORECASE)
            item_id = url_id_m.group(1) if url_id_m else ""

            # 规范化 URL (使用 https)
            detail_url = link_url.replace("http://ljs.gxcq.com.cn/", "https://ljs.gxcq.com.cn/")
            if not detail_url.startswith("http"):
                detail_url = urljoin(self.detail_base_url, detail_url)

            # 推断资产类型 (从标题)
            atp = None
            for code, (_, label) in ASSETS_TYPE_PARENT_MAP.items():
                if label in title or code.lower() in title.lower():
                    atp = code
                    break

            items.append(GxcqListItem(
                source_item_id=item_id or title[:30],
                source_url=detail_url,
                title=title,
                price_raw=price_raw,
                price_num=price_num,  # 已在前面 try/except 计算好
                project_status=status,
                end_time=end_time,
                signup_deadline=end_time,
                assets_type_parent=atp,
                raw_json={"_raw_html_snippet": inner_html[:500]},
            ))

        return items

    def parse_total_count(self, api_data: Dict[str, Any]) -> int:
        """从列表 API 响应中提取条目总数，用于分页判断。

        返回 0 表示无法获取总数，调用方应回退到"空页即结束"策略。

        优先从 data.project_total 读取（id=2 搜索API 返回此字段）；
        若不存在则退回到 data.pages_html 中解析"共N页"来估算总数。
        """
        raw_data = api_data.get("data")
        if isinstance(raw_data, dict):
            # 方式1: 直接取 project_total（id=2 搜索API 专用）
            total = raw_data.get("project_total", 0) or raw_data.get("total", 0) or raw_data.get("count", 0) or 0
            try:
                total = int(total)
                if total > 0:
                    return total
            except (TypeError, ValueError):
                pass

            # 方式2: 从 pages_html 中解析"共N页"
            pages_html = raw_data.get("pages_html", "")
            if pages_html:
                m = re.search(r'共(\d+)页', pages_html)
                if m:
                    return int(m.group(1)) * 10  # 每页10条估算
        return 0

    # ── 详情 API ──
    def fetch_detail_api(self, list_item: GxcqListItem) -> Dict[str, Any]:
        """通过 RESTful API 获取完整详情数据 (ljs.gxcq.com.cn)
        
        依次调用所有已知的详情子端点并合并结果。
        """
        merged: Dict[str, Any] = {}
        errors: List[str] = []

        assets_id = self._extract_assets_id(list_item)
        if not assets_id:
            merged["_source"] = "error_no_id"
            merged["_errors"] = ["Cannot extract assetsId from list_item"]
            return merged

        for endpoint in DETAIL_API_ENDPOINTS:
            url = f"{self.detail_base_url}/api/dscq-project/{endpoint}?assetsId={assets_id}"
            try:
                resp = self.session.get(url, headers=DETAIL_API_HEADERS, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") in (200, "200", 0, "0", True):
                    endpoint_key = endpoint.replace("/", "_").replace("-", "_")
                    merged[endpoint_key] = data.get("data")
            except Exception as e:
                errors.append(f"{endpoint}: {e}")

        if errors:
            success_count = len(DETAIL_API_ENDPOINTS) - len(errors)
            print(f"[GXCQ] Detail API: {success_count}/{len(DETAIL_API_ENDPOINTS)} succeeded")

        merged["_source"] = "detail_api"
        merged["_assets_id"] = assets_id
        merged["_errors"] = errors if errors else []
        return merged

    def _extract_assets_id(self, list_item: GxcqListItem) -> str:
        """从 list_item 中提取 assetsId"""
        url = list_item.source_url or ""
        m = re.search(r"/projectDetail/([a-f0-9]+)\.html", url, re.IGNORECASE)
        if m:
            return m.group(1)
        if list_item.raw_json:
            aid = first_non_blank(
                compact_text(list_item.raw_json.get("assetsId")),
                compact_text(list_item.raw_json.get("id")),
            )
            if aid:
                return aid
        return list_item.source_item_id or ""

    def parse_detail_response(
        self,
        detail_data: Dict[str, Any],
        list_item: Optional[GxcqListItem] = None,
    ) -> GxcqDetailBundle:
        """解析详情 API 数据"""
        key_values: Dict[str, str] = {}
        all_attachments: List[Dict[str, Any]] = []
        text_parts: List[str] = []

        li = list_item
        source_id = li.source_item_id if li else ""
        source_url = li.source_url if li else ""

        # ---- 从各子端点提取 ----
        base_data = detail_data.get("assets_detail_base") or {}
        if isinstance(base_data, dict):
            self._extract_kv_from_dict(base_data, key_values, prefix="")
            title = first_non_blank(
                deep_find(base_data, ("projectName", "title", "name", "assetsName")),
                li.title if li else "",
            )
            # 价格
            for src_key, dst_key in [
                ("listingPrice", "挂牌价格"), ("transferPrice", "挂牌价格"),
                ("price", "挂牌价格"), ("evaluationPrice", "评估价"),
                ("assessmentPrice", "评估价"), ("deposit", "保证金"),
                ("bondAmount", "保证金"), ("earnestMoney", "保证金"),
            ]:
                val = deep_find(base_data, (src_key,))
                if val is not None:
                    key_values.setdefault(dst_key, f"{val}万元" if float(val) == int(float(val)) else f"{val}万元")
            # 联系人
            for src_key, dst_key in [
                ("contactPerson", "项目咨询联系人"), ("consultantName", "项目咨询联系人"),
                ("contactPhone", "联系电话"), ("consultantTel", "联系电话"),
                ("telephone", "联系电话"), ("telphone", "联系电话"),
            ]:
                val = deep_find(base_data, (src_key,))
                if val:
                    key_values.setdefault(dst_key, str(val))
            # 时间
            for src_key, dst_key in [
                ("endDate", "报名截止时间"), ("deadline", "报名截止时间"),
                ("listingEndDate", "报名截止时间"), ("signupDeadline", "报名截止时间"),
            ]:
                val = deep_find(base_data, (src_key,))
                if val:
                    key_values.setdefault(dst_key, str(val))

        top_info = detail_data.get("normal_announcement_detail_top_info") or {}
        if isinstance(top_info, dict):
            self._extract_kv_from_dict(top_info, key_values, prefix="概要_")

        assets_house = detail_data.get("announcement_detail_assets_house") or {}
        if isinstance(assets_house, dict):
            self._extract_kv_from_dict(assets_house, key_values, prefix="资产_")

        transferor_info = detail_data.get("announcement_detail_transferor_info") or {}
        if isinstance(transferor_info, dict):
            tname = deep_find(transferor_info, (
                "transferorName", "transferor", "sellerName", "outgoingPartyName",
            ))
            if tname:
                key_values.setdefault("转让方", tname)
            self._extract_kv_from_dict(transferor_info, key_values, prefix="转让方_", skip_keys={
                "transferorName", "transferor", "sellerName", "outgoingPartyName",
            })

        enterprise = detail_data.get("normal_announcement_detail_assets_enterpris") or {}
        if isinstance(enterprise, dict):
            cname = deep_find(enterprise, (
                "companyName", "enterpriseName", "targetCompanyName", "subjectCompanyName",
            ))
            if cname:
                key_values.setdefault("标的企业", cname)
            self._extract_kv_from_dict(enterprise, key_values, prefix="企业_", skip_keys={
                "companyName", "enterpriseName", "targetCompanyName", "subjectCompanyName",
            })

        financial_audit = detail_data.get("normal_announcement_detail_financial_audit") or {}
        if isinstance(financial_audit, dict):
            ardate = deep_find(financial_audit, ("auditRefDate", "auditDate", "baseDate"))
            if ardate:
                key_values.setdefault("评估基准日", str(ardate))
            self._extract_kv_from_dict(financial_audit, key_values, prefix="财务_", max_key_len=20)

        dispose_method = detail_data.get("announcement_detail_dispose_method") or {}
        if isinstance(dispose_method, dict):
            mtext = deep_find(dispose_method, ("disposeMethod", "methodDesc", "description"))
            if mtext:
                key_values.setdefault("处置方式", str(mtext)[:200])

        buyer_aptitude = detail_data.get("announcement_detail_buyer_aptitude_content") or {}
        if isinstance(buyer_aptitude, dict):
            btext = deep_find(buyer_aptitude, ("content", "condition", "qualification"))
            if btext:
                key_values.setdefault("受让方资格条件", str(btext)[:500])

        risk_notice = detail_data.get("announcement_detail_risk_warning_notice") or {}
        if isinstance(risk_notice, dict):
            rtext = deep_find(risk_notice, ("content", "notice", "warning"))
            if rtext:
                key_values.setdefault("风险提示", str(rtext)[:500])

        auction = detail_data.get("announcement_detail_auction_announcement") or {}
        if isinstance(auction, dict):
            atext = deep_find(auction, ("content", "noticeText", "description"))
            if atext:
                key_values.setdefault("竞价公告", str(atext)[:500])

        disclose = detail_data.get("announcement_detail_disclose_info") or {}
        if isinstance(disclose, dict):
            dtext = deep_find(disclose, ("content", "info", "description"))
            if dtext:
                key_values.setdefault("信息披露", str(dtext)[:500])

        eval_info = detail_data.get("announcement_detail_evaluation_info") or {}
        if isinstance(eval_info, dict):
            eprice = deep_find(eval_info, ("evalPrice", "evaluationPrice", "price"))
            eorg = deep_find(eval_info, ("evalOrg", "organization", "agency"))
            if eprice:
                key_values.setdefault("评估价格", str(eprice))
            if eorg:
                key_values.setdefault("评估机构", str(eorg))

        # ---- 附件清单 ----
        accessory_data = detail_data.get("announcement_detail_publish_accessory") or {}
        if isinstance(accessory_data, dict) or isinstance(accessory_data, list):
            file_list = accessory_data.get("data") if isinstance(accessory_data, dict) else accessory_data
            if isinstance(file_list, list):
                seen_urls: set[str] = set()
                for f in file_list:
                    if not isinstance(f, dict):
                        continue
                    fname = compact_text(f.get("fileName") or f.get("name") or f.get("originalName") or "")
                    furl = compact_text(f.get("url") or f.get("fileUrl") or f.get("downloadUrl") or f.get("filePath") or "")
                    if furl and furl not in seen_urls:
                        seen_urls.add(furl)
                        abs_url = furl if furl.startswith("http") else urljoin(self.detail_base_url, furl)
                        all_attachments.append({
                            "name": fname or abs_url.rsplit("/", 1)[-1],
                            "url": abs_url,
                            "source_payload_type": "detail_api.accessory",
                            "source_path": "publish_accessory",
                            "source_excerpt": fname,
                        })

        # 目录内容中的文本
        catalog = detail_data.get("announcement_detail_catalog_content") or {}
        if isinstance(catalog, dict):
            ctext = deep_find(catalog, ("content", "html", "text"))
            if ctext and isinstance(ctext, str):
                text_parts.append(compact_text(re.sub(r"<[^>]+>", "", ctext)))

        # ---- 从 list_item 补充基本信息 ----
        if li:
            if li.source_item_id and "项目编号" not in key_values:
                key_values["项目编号"] = li.source_item_id
            if li.title and "项目名称" not in key_values:
                key_values["项目名称"] = li.title
            if li.price_raw and "挂牌价格" not in key_values:
                key_values["挂牌价格"] = li.price_raw
            if li.project_status:
                key_values["项目状态"] = li.project_status
            if li.end_time and "报名截止时间" not in key_values:
                key_values["报名截止时间"] = li.end_time
            if li.contact_person:
                key_values.setdefault("联系人", li.contact_person)
            if li.contact_phone:
                key_values.setdefault("联系电话", li.contact_phone)
            if li.assets_type_parent and li.assets_type_parent in ASSETS_TYPE_PARENT_MAP:
                _, type_label = ASSETS_TYPE_PARENT_MAP[li.assets_type_parent]
                key_values["资产类型"] = type_label

        kv_lines = [f"{k}: {v}" for k, v in key_values.items()]
        detail_text = "\n".join(kv_lines + text_parts)[:15000]
        title = key_values.get("项目名称") or (li.title if li else "")

        return GxcqDetailBundle(
            source_item_id=source_id,
            source_url=source_url,
            title=title,
            key_values=key_values,
            attachments=all_attachments,
            detail_text=detail_text,
            list_item=list_item,
            image_urls=[],
            detail_json=dict(detail_data),
        )

    def _extract_kv_from_dict(
        self,
        data: Dict[str, Any],
        target: Dict[str, str],
        *,
        prefix: str = "",
        skip_keys: Optional[set] = None,
        max_key_len: int = 30,
    ) -> None:
        if not isinstance(data, dict):
            return
        skip = skip_keys or set()
        for k, v in data.items():
            k_str = str(k).strip()
            if not k_str or k_str.startswith("_"):
                continue
            if k_str.lower() in {s.lower() for s in skip}:
                continue
            if v is None or isinstance(v, (dict, list)):
                continue
            val_str = compact_text(v)
            if not val_str:
                continue
            display_key = f"{prefix}{k_str}" if prefix else k_str
            if len(display_key) <= max_key_len:
                target.setdefault(display_key, val_str)

    # ── 兼容接口: SPA 页面解析 (fallback) ──
    def fetch_detail_page(self, url: str) -> str:
        resp = self.session.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    def parse_detail_html(
        self,
        html: str,
        url: str = "",
        list_item: Optional[GxcqListItem] = None,
    ) -> GxcqDetailBundle:
        """兼容方法: 优先使用 API 获取详情，失败则降级到 HTML 文本提取"""
        if list_item:
            try:
                detail_data = self.fetch_detail_api(list_item)
                has_data = any(
                    v is not None and v != [] and v != ""
                    for k, v in detail_data.items()
                    if not k.startswith("_")
                )
                if has_data:
                    return self.parse_detail_response(detail_data, list_item=list_item)
            except Exception as e:
                print(f"[GXCQ] Detail API failed, falling back to HTML: {e}")

        text = re.sub(r"(?is)<script.*?</script>|<style.*</style>", " ", html or "")
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = compact_text(text)
        kv: Dict[str, str] = {}

        if list_item:
            for fk, fv in [("项目编号", list_item.source_item_id),
                          ("项目名称", list_item.title),
                          ("挂牌价格", list_item.price_raw),
                          ("项目状态", list_item.project_status)]:
                if fv and fk not in kv:
                    kv[fk] = fv
            kv.update({k: str(v) for k, v in list_item.raw_json.items()
                      if isinstance(v, (str, int, float))})

        return GxcqDetailBundle(
            source_item_id=list_item.source_item_id if list_item else "",
            source_url=url,
            title=list_item.title if list_item else "",
            key_values=kv,
            attachments=self._extract_attachments_fallback(html),
            detail_text=text[:AI_DETAIL_TEXT_LIMIT],
            list_item=list_item,
            image_urls=self._extract_images_fallback(html),
            raw_html=html,
        )

    def _extract_attachments_fallback(self, html: str) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        pattern = re.compile(
            r'<a[^>]+href=["\']([^"\']*(?:\.pdf|\.docx?|\.xlsx?|\.zip|\.rar))[^"\']*["\']',
            re.IGNORECASE,
        )
        for m in pattern.finditer(html):
            href = m.group(1)
            abs_url = urljoin(self.detail_base_url, href)
            if abs_url not in seen:
                seen.add(abs_url)
                attachments.append({"name": abs_url.rsplit("/", 1)[-1], "url": abs_url})
        return attachments

    def _extract_images_fallback(self, html: str) -> List[str]:
        images: List[str] = []
        seen: set[str] = set()
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
            src = m.group(1)
            if src.startswith("data:"):
                continue
            abs_url = urljoin(self.detail_base_url, src)
            if abs_url not in seen:
                seen.add(abs_url)
                images.append(abs_url)
        return images[:30]

    # ── AI 上下文 ──
    def build_ai_context(self, bundle: GxcqDetailBundle) -> AIExtractionContext:
        sections = [
            f"source_platform: {GXCQ_PLATFORM}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.source_url}",
            f"title: {bundle.title}",
        ]
        if bundle.key_values:
            sections.append("key_values:\n" + json.dumps(bundle.key_values, ensure_ascii=False, indent=2))
        if bundle.detail_text:
            sections.append("detail_text:\n" + bundle.detail_text[:AI_DETAIL_TEXT_LIMIT])

        asset_group = infer_asset_group(
            bundle.list_item.assets_type_parent if bundle.list_item else None,
            bundle.title,
        )
        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(sections)[:AI_DETAIL_TEXT_LIMIT],
            notice_text=bundle.key_values.get("风险提示", "") or bundle.key_values.get("特别告知", ""),
            image_urls=list(bundle.image_urls),
            asset_group=asset_group,
            paimai_id=f"{GXCQ_PLATFORM}:{bundle.source_item_id}" if bundle.source_item_id else "",
        )

    # ── 公共字段映射 ──
    def map_common_candidates(self, bundle: GxcqDetailBundle) -> Dict[str, Any]:
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

        title = first_non_blank(kv.get("项目名称"), kv.get("标的名称"), bundle.title)
        status = first_non_blank(kv.get("项目状态"), (li.project_status if li else None))
        price = first_non_blank(
            kv.get("挂牌价格"), kv.get("转让底价"),
            (li.price_raw if li else None),
        )
        deposit = first_non_blank(kv.get("保证金"), (li.deposit_amount if li else None))
        contact = join_non_blank(
            kv.get("联系人"), kv.get("项目咨询联系人"), kv.get("联系电话"),
            kv.get("概要_agencyContacts"), kv.get("概要_agencyContactsTel"),
            kv.get("概要_agencyContactsDeptTel"),
            (li.contact_person if li else None), (li.contact_phone if li else None),
        )
        notice = first_non_blank(kv.get("风险提示"), kv.get("特别告知"))
        location = join_non_blank(
            kv.get("概要_province_name"), kv.get("概要_city_name"), kv.get("概要_county_name"),
            kv.get("标的所在地"), kv.get("资产_坐落"), kv.get("资产_位置"),
        )
        disposal_party = first_non_blank(
            kv.get("转让方"), kv.get("转让方名称"),
            kv.get("概要_transferorName"), kv.get("标的企业"),
        )
        disposal_agency = first_non_blank(
            kv.get("概要_organizationName"), kv.get("机构名称"), kv.get("交易机构"),
        )
        signup_start = first_non_blank(
            kv.get("概要_listingStartTime"), kv.get("概要_startTime"),
            kv.get("挂牌开始日期"), kv.get("信息披露开始日期"),
        )

        asset_group = infer_asset_group((li.assets_type_parent if li else None), title)
        asset_type = infer_asset_type((li.assets_type_parent if li else None), title)

        set_field("source_platform", GXCQ_PLATFORM, "computed", "adapter", "constant", 1.0)
        set_field("source_item_id", bundle.source_item_id, "list_html", "id", bundle.source_item_id, "html_rule", 1.0)
        set_field("source_url", bundle.source_url, "computed", "url", bundle.source_url, "request", 1.0)
        set_field("asset_group", asset_group, "computed", "infer", asset_group, "inference", 0.85)
        set_field("asset_type", asset_type or "产权转让", "computed", "infer", asset_type or "产权转让", "inference", 0.85)
        set_field("project_name", title, "detail/list", "title", title, "api_rule", 0.95)
        set_field("project_status", status, "list/detail", "status", status)
        set_field("asset_location", location, "detail_api", "location", location)
        set_field("disposal_party", disposal_party, "detail_api", "disposal_party", disposal_party)
        set_field("disposal_agency", disposal_agency, "detail_api", "disposal_agency", disposal_agency)
        set_field("final_price_raw", price, "list/detail", "price", price)
        set_field("start_price_raw", price, "list/detail", "price", price)
        set_field("deposit_amount", deposit, "list/detail", "deposit", deposit)
        set_field("contact_info", contact, "detail/api", "contact", contact)
        set_field("special_notice", notice, "detail/api", "notice", notice)
        set_field("signup_start_time", signup_start, "detail_api", "signup_start", signup_start)
        set_field("signup_end_time", first_non_blank(
            (li.end_time if li else None), kv.get("报名截止时间"), kv.get("概要_endDate"),
        ), "list/detail", "end_time", (li.end_time if li else None))
        set_field("attachments_json", json.dumps(bundle.attachments, ensure_ascii=False), "detail_api", "attachments", "", "api_rule", 0.9)
        set_field("data_source", GXCQ_DATA_SOURCE, "computed", "adapter", "constant", 1.0)

        common["field_results"] = results
        return common

    def classify_bundle(self, bundle: GxcqDetailBundle) -> str:
        return infer_asset_group(
            (bundle.list_item.assets_type_parent if bundle.list_item else None),
            bundle.title,
        )

    def map_special_candidates(self, bundle: GxcqDetailBundle, asset_group: str) -> Dict[str, Any]:
        kv = bundle.key_values
        values: Dict[str, Any] = {}
        images = "; ".join(bundle.image_urls[:80])

        if asset_group == "equity":
            values.update({
                "transferor": first_non_blank(kv.get("转让方"), kv.get("出让方")),
                "target_company": first_non_blank(kv.get("标的企业"), kv.get("企业名称")),
                "equity_ratio": first_non_blank(kv.get("持股比例"), kv.get("转让比例")),
                "disclosure_items": first_non_blank(kv.get("重大事项"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "debt":
            values.update({
                "debtor_name": kv.get("债务人") or kv.get("主债务人"),
                "creditor": first_non_blank(kv.get("转让方"), kv.get("债权人")),
                "site_images": images,
            })
        elif asset_group == "real_estate":
            values.update({
                "building_area": first_non_blank(kv.get("建筑面积"), kv.get("面积")),
                "property_location": first_non_blank(kv.get("位置"), kv.get("坐落"), kv.get("标的所在地")),
                "site_images": images,
            })
        elif asset_group == "land":
            values.update({
                "land_area": first_non_blank(kv.get("土地面积")),
                "site_images": images,
            })
        else:
            values.update({
                "raw_detail_text": (bundle.detail_text or "")[:AI_DETAIL_TEXT_LIMIT],
                "raw_table_pairs_json": json.dumps(kv, ensure_ascii=False, sort_keys=True),
                "site_images": images,
            })

        return {k: v for k, v in values.items() if compact_text(str(v or ""))}


# 别名: GxcqAdapter = GxcqBrowserFetcher
# GxcqBrowserFetcher 实际包含了完整的适配器逻辑（REST API + 解析 + AI 上下文），
# 只是类名有误导性，此处提供别名供 multi_platform_runner / gycq_adapter 导入。
GxcqAdapter = GxcqBrowserFetcher


# ===== 测试入口 =====
if __name__ == "__main__":
    adapter = GxcqAdapter()

    print("=== GXCQ (广西联合产权) 测试 ===\n")
    try:
        # 1. 列表测试
        print("--- 1. 列表 API ---")
        list_data = adapter.fetch_list_api(page=1, size=5, assets_type_parent="ZQ", cate_id=154)
        items = adapter.parse_list_response(list_data)
        print(f"获取到 {len(items)} 条项目\n")

        for i, item in enumerate(items[:10]):
            info = {"编号": item.source_item_id, "标题": item.title,
                    "价格": item.price_raw, "状态": item.project_status,
                    "截止": item.end_time}
            print(f"[{i+1}] {info['标题']}")
            for k, v in info.items():
                if k != "标题" and v:
                    print(f"    {k}: {v}")
            print()

        # 2. 详情测试
        if items:
            print("--- 2. 详情 API ---")
            test_item = items[0]
            print(f"目标: [{test_item.source_item_id}] {test_item.title}")
            detail_data = adapter.fetch_detail_api(test_item)
            non_empty = sum(1 for k, v in detail_data.items()
                           if not k.startswith("_") and v is not None and v != [] and v != "")
            print(f"成功获取 {non_empty}/{len(DETAIL_API_ENDPOINTS)} 个详情端点\n")
            bundle = adapter.parse_detail_response(detail_data, list_item=test_item)
            print(f"键值对数: {len(bundle.key_values)}")
            for k, v in list(bundle.key_values.items())[:20]:
                print(f"  {k}: {v}")
            print(f"附件数: {len(bundle.attachments)}")
            for att in bundle.attachments[:5]:
                print(f"  - {att['name']} | {att['url'][:80]}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
