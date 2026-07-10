"""
CBEX 平台适配器 - 北京产权交易所

数据来源：
- 列表页: www.cbex.com.cn/xm/zqzc/ (需浏览器渲染绕过 JS 挑战)
- 详情页: otc.cbex.com/xmjs/prj/detail/{id}.html (需浏览器渲染)
- 详情 API: www.cbex.com.cn/service/s/prj/toly (POST JSON)
"""

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from jd.ai_extractor import AIExtractionContext

# 复用 CQUAE 的 HTML 解析器提取详情页表格键值对
from platform_adapters.cquae_adapter import (
    _parse_lite_html,
    _cell_texts,
    looks_like_label,
    add_key_value,
    compact_text as cquae_compact_text,
    first_non_blank as cquae_first_non_blank,
    _find_by_alias,
    is_blank,
)


DEFAULT_BASE_URL = "https://www.cbex.com.cn"
OTC_BASE_URL = "https://otc.cbex.com"

OTCPRJ_DETAIL_URL = f"{OTC_BASE_URL}/xmjs/prj/detail/{{id}}.html"
# CBEX 列表 JSON 接口 (F12 抓包得到, 走 /onss-api/ 子域, 同样受创宇盾 WAF 保护)
CBEX_SEARCH_URL = "https://www.cbex.com.cn/onss-api/jsonp/project/search"
# 用于触发 WAF 计算 clearance cookie 的种子页
CBEX_WAF_SEED_PAGE = f"{DEFAULT_BASE_URL}/xm/zqzc/ypl/"
CBEX_PLATFORM = "cbex"
CBEX_DATA_SOURCE = "北京产权交易所"


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
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


def safe_json(val):
    if val is None:
        return "null"
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        return str(val)


def infer_asset_group(trade_type: Optional[str] = None, title: Optional[str] = None) -> str:
    haystack = compact_text(" ".join(filter(None, [trade_type, title])))
    checks = [
        ("debt", ("债权", "不良资产", "债务", "应收账款")),
        ("equity", ("股权", "产权转让", "企业增资", "增资扩股")),
        ("real_estate", ("房产", "房地产", "房屋", "商铺", "车位", "住宅", "厂房")),
        ("land", ("土地", "宗地", "建设用地", "土地使用权")),
        ("vehicle", ("车辆", "机动车", "汽车")),
        ("equipment", ("设备", "机器", "机械", "生产线")),
        ("usufruct", ("租赁", "经营权", "使用权", "招租")),
        ("goods", ("物资", "存货", "货物", "资产处置", "废旧")),
    ]
    for group, keywords in checks:
        if any(kw in haystack for kw in keywords):
            return group
    return "other"


def infer_asset_type(trade_type: Optional[str] = None, title: Optional[str] = None) -> Optional[str]:
    name_map = {
        "债权": "债权",
        "股权": "股权",
        "企业增资": "企业增资",
        "增资扩股": "增资扩股",
        "房产": "房产",
        "土地": "土地",
        "车辆": "车辆",
        "设备": "设备",
        "租赁": "租赁",
    }
    haystack = compact_text(" ".join(filter(None, [trade_type, title])))
    for key, label in name_map.items():
        if key in haystack:
            return label
    return "产权转让"


@dataclass
class CbexListItem:
    prj_id: str
    title: str
    detail_url: str
    project_no: Optional[str] = None
    price_raw: Optional[str] = None
    region: Optional[str] = None
    status: Optional[str] = None
    trade_type: Optional[str] = None
    source_excerpt: str = ""
    raw_json: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CbexDetailBundle:
    url: str
    prj_id: str
    html: str
    title: Optional[str] = None
    source_item_id: Optional[str] = None
    key_values: Dict[str, str] = field(default_factory=dict)
    image_urls: List[str] = field(default_factory=list)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    list_item: Optional[CbexListItem] = None
    raw_payloads: Dict[str, Any] = field(default_factory=dict)


class CbexBrowserFetcher:
    """使用浏览器渲染绕过 CBEX 的 JS 挑战返回渲染后的 HTML

    优化说明：
    - 复用浏览器上下文，避免每次请求都启动/关闭浏览器（耗时 3-5s 每次）
    - 用 domcontentloaded + 短等待替代 networkidle（耗时 10-30s 每次）
    - 使用 channel="chrome" 启动真实 Chrome，避免 WAF 检测
    - 调用 close() 释放浏览器资源
    """

    def __init__(self, headless: bool = True, timeout_ms: int = 60_000, profile_path: str | None = None) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.profile_path = profile_path
        # Reusable browser resources
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        # 复用浏览器页面绕过 WAF (单线程): API 用同一页面, 详情按域名各维护一页
        self._api_page: Any = None
        self._detail_pages: Dict[str, Any] = {}

    def close(self) -> None:
        """Release browser resources."""
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
        # 页面随 browser 关闭而失效, 清空引用避免悬空
        self._api_page = None
        self._detail_pages = {}
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _ensure_playwright(self) -> Any:
        """Lazy-init & reuse a Playwright browser context."""
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Playwright is required for CBEX browser fallback") from exc

        self._playwright = sync_playwright()
        p = self._playwright.__enter__()
        if self.profile_path:
            self._context = p.chromium.launch_persistent_context(
                self.profile_path,
                headless=self.headless,
                viewport={"width": 1365, "height": 900},
                channel="chrome",
            )
        else:
            self._browser = p.chromium.launch(
                headless=self.headless,
                channel="chrome",
            )
            self._context = self._browser.new_context(
                viewport={"width": 1365, "height": 900},
            )
        # Anti-detection: hide automation fingerprints from WAF
        self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
        """)
        return self._context

    def _fetch_with_playwright(self, url: str) -> str:
        ctx = self._ensure_playwright()
        page = ctx.new_page()
        try:
            if self.timeout_ms <= 0:
                page.set_default_timeout(0)
                page.set_default_navigation_timeout(0)
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms or 0)
            # CBEX 依赖 AJAX 动态渲染列表内容，等 networkidle（最多 15s）+
            # 额外 sleep 保证内容填充完成。
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            import time
            time.sleep(3)
            html = page.content()
        finally:
            page.close()
        return html

    def _render_with_selenium(self, url: str, wait_seconds: int = 8) -> str:
        """通过 Selenium 渲染页面（处理 JS 挑战 + 跨域跳转）"""
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
        driver = webdriver.Chrome(options=options)
        try:
            driver.get(url)
            import time
            time.sleep(wait_seconds)
            return driver.page_source
        finally:
            driver.quit()

    def fetch_list_html(self, url: str | None = None) -> str:
        """获取列表页渲染后的 HTML（默认取债权资产页面）
        先试 Playwright，失败则降级到 Selenium。
        """
        url = url or f"{DEFAULT_BASE_URL}/xm/zqzc/"
        try:
            html = self._fetch_with_playwright(url)
            if html and len(html) > 5000 and not self._is_waf_page(html):
                return html
        except Exception:
            pass
        return self._render_with_selenium(url, wait_seconds=12)

    def _is_waf_page(self, html: str) -> bool:
        return any(marker in html for marker in ("__jsl_clearance_s", "knownsec", "创宇盾"))

    def fetch_detail_html(self, prj_id: str) -> str:
        """获取详情页渲染后的 HTML"""
        url = OTCPRJ_DETAIL_URL.format(id=prj_id)
        try:
            html = self._fetch_with_playwright(url)
            if html and len(html) > 5000 and not self._is_waf_page(html):
                return html
        except Exception:
            pass
        return self._render_with_selenium(url, wait_seconds=8)

    def _ensure_api_page(self) -> Any:
        """复用同一个 Playwright 页面做 API fetch (同源 www.cbex.com.cn, 自动绕过 WAF)。"""
        if self._api_page is not None:
            return self._api_page
        ctx = self._ensure_playwright()
        page = ctx.new_page()
        page.goto(CBEX_WAF_SEED_PAGE, wait_until="domcontentloaded", timeout=60000)
        # 等待 WAF 的 JS 挑战执行完毕并写入 clearance cookie
        page.wait_for_timeout(8000)
        self._api_page = page
        return page

    def api_search(self, business_type: str = "", disclosure_type: str = "",
                   from_page: int = 1, page_size: int = 15) -> Optional[Dict[str, Any]]:
        """在浏览器上下文内 fetch 列表 JSON 接口 (绕过 WAF)。返回解析后的 dict 或 None。

        实测: 纯 requests 带 clearance cookie 仍会被 521 拦截 (WAF 状态化);
        但浏览器页面上下文内的 fetch 始终携带有效 cookie 与挑战状态, 稳定返回 JSON。
        """
        page = self._ensure_api_page()
        params = (f"fromPage={from_page}&pageSize={page_size}"
                  f"&businessType={business_type}&disclosureType={disclosure_type}"
                  f"&sortProperty=disclosuretime&sortDirection=1&mark=xm"
                  f"&_={int(time.time() * 1000)}")
        url = f"{CBEX_SEARCH_URL}?{params}"
        js = "(u) => fetch(u, {credentials:'include'}).then(r => r.text())"
        try:
            text = page.evaluate(js, url)
        except Exception:
            # WAF 可能重新挑战, 重置 API 页面再过一次种子页
            try:
                page.goto(CBEX_WAF_SEED_PAGE, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                text = page.evaluate(js, url)
            except Exception:
                return None
        try:
            return json.loads(text)
        except Exception:
            return None

    def _ensure_detail_page_for(self, host: str) -> Any:
        """为指定详情域名维护一个页面: 先 goto 该域首页过 WAF, 之后可同域 fetch 详情。"""
        if host in self._detail_pages:
            return self._detail_pages[host]
        ctx = self._ensure_playwright()
        page = ctx.new_page()
        try:
            page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=60000)
            # 等待 WAF 的 JS 挑战执行完毕并写入该域 clearance cookie
            page.wait_for_timeout(6000)
        except Exception:
            pass
        self._detail_pages[host] = page
        return page

    def fetch_detail_html_by_url(self, url: str) -> str:
        """在浏览器上下文内打开详情页 (自动绕过 WAF), 返回渲染后的 HTML。

        重要: CBEX 详情页正文由 JS 动态注入, 必须等渲染后取 page.content();
        纯 fetch/requests 拿到的服务器原始 HTML 只是骨架 (样板示例数据, 如固定
        的 GR2023BJ1002647), 故详情必须用整页 goto, 不能用 fetch 文本替代。
        """
        # www.cbex.com 301 -> www.cbex.com.cn, 二者同站; 归一到 .cn 复用 API 页面
        goto_url = url.replace("https://www.cbex.com/", "https://www.cbex.com.cn/")
        host = urlparse(goto_url).netloc
        if host == "www.cbex.com.cn":
            # 复用已稳定过 WAF 的 API 页面, 省去再开页/过 WAF 的开销
            page = self._ensure_api_page()
        else:
            page = self._ensure_detail_page_for(host)
        for _ in range(2):
            try:
                page.goto(goto_url, wait_until="domcontentloaded", timeout=60000)
                # 等 JS 把真实数据注入表格后再取 DOM
                page.wait_for_timeout(2000)
                return page.content()
            except Exception:
                continue
        return ""


class CbexAdapter:
    source_platform = CBEX_PLATFORM
    source_site_name = CBEX_DATA_SOURCE

    # ===== 列表解析 =====
    def parse_list_html(self, html: str) -> List[CbexListItem]:
        """从渲染后的列表页 HTML 解析项目列表"""
        items = []
        # CBEX 新版：链接格式为 otc.cbex.com/page/s/zc_prjs/index?id=N
        # 旧版：/prj/detail/N
        link_re = re.compile(
            r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']*(?:(?:/prj/detail/|index\?id=)(\d+))[^"\']*)["\'][^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        seen_ids = set()
        for m in link_re.finditer(html):
            href = m.group(1)
            prj_id = m.group(2)
            if prj_id in seen_ids:
                continue
            seen_ids.add(prj_id)
            title = compact_text(re.sub(r'<[^>]+>', '', m.group(3)))
            if not title or len(title) < 4:
                continue
            detail_url = urljoin(OTC_BASE_URL, href)
            items.append(CbexListItem(
                prj_id=prj_id,
                title=title,
                detail_url=detail_url,
            ))
        return items

    # ===== 列表 JSON 接口解析 =====
    @staticmethod
    def from_api_item(it: Dict[str, Any]) -> Optional[CbexListItem]:
        """把 /onss-api/jsonp/project/search 返回的列表项映射为 CbexListItem。

        关键字段 (实测):
          code            挂牌编号 (如 CP2026BJ1000193), 作为稳定 prj_id
          name            项目名称
          url             真实详情文章页 (www.cbex.com/xm/...html)
          disclosureprice 挂牌价/转让底价 ("面议" 等)
          regionname      所在地区
          businesstypename 交易品类 (债权资产/租赁权等)
          id              UUID (详情接口用不到, 旧 otc 页已失效)
        """
        code = it.get("code") or it.get("id")
        if not code:
            return None
        detail_url = it.get("url") or it.get("docsurl")
        if not detail_url:
            return None
        # 部分类目返回相对路径 (如 /xm/fwzl/...), 补全为绝对 URL。
        # 统一归一到 www.cbex.com.cn (www.cbex.com 会 301 重定向到 .cn, 且 .cn 与
        # API 页面同域, 详情 fetch 可复用已稳定过 WAF 的 API 页面, 避免跨域 CORS)。
        if not detail_url.startswith("http"):
            detail_url = "https://www.cbex.com.cn" + (detail_url if detail_url.startswith("/") else "/" + detail_url)
        elif detail_url.startswith("https://www.cbex.com/"):
            detail_url = "https://www.cbex.com.cn/" + detail_url[len("https://www.cbex.com/"):]
        name = it.get("name") or it.get("businesstypename") or ""
        price = it.get("disclosureprice") or it.get("disclosurepricewithunit")
        region = it.get("regionname")
        trade_type = it.get("businesstypename")
        excerpt = " ".join(filter(None, [name, price, region]))
        return CbexListItem(
            prj_id=compact_text(code),
            title=compact_text(name),
            detail_url=detail_url,
            project_no=compact_text(code),
            price_raw=compact_text(price),
            region=compact_text(region),
            status=None,
            trade_type=compact_text(trade_type),
            source_excerpt=compact_text(excerpt),
            raw_json=it,
        )

    @staticmethod
    def parse_list_json_response(data: Dict[str, Any]) -> List[CbexListItem]:
        """从接口 JSON 解析出列表项 (data.data 为列表数组)。"""
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        rows = (inner or {}).get("data") if isinstance(inner, dict) else None
        if not isinstance(rows, list):
            return []
        items: List[CbexListItem] = []
        for it in rows:
            li = CbexAdapter.from_api_item(it)
            if li:
                items.append(li)
        return items

    # ===== 详情解析 =====
    def parse_detail_html(self, html: str, prj_id: str, list_item: Optional[CbexListItem] = None) -> CbexDetailBundle:
        """从渲染后的详情页 HTML 解析详情数据"""
        key_values: Dict[str, str] = {}
        image_urls: List[str] = []
        attachments: List[Dict[str, Any]] = []

        # 用 CQUAE 解析器提取表格键值对
        if html:
            parser = _parse_lite_html(html)
            for row in parser.rows:
                cells = _cell_texts(row)
                if len(cells) < 2:
                    continue
                if len(cells) == 2 and looks_like_label(cells[0]):
                    add_key_value(key_values, cells[0], cells[1])
                elif len(cells) == 3 and looks_like_label(cells[1]):
                    add_key_value(key_values, cells[1], cells[2])
                elif len(cells) % 2 == 0:
                    for key, value in zip(cells[0::2], cells[1::2]):
                        if looks_like_label(key):
                            add_key_value(key_values, key, value)

            # CBEX 自定义布局：从 xmjs_detail_label_name/cont 中提取键值对
            label_cont_re = re.compile(
                r'class\s*=\s*["\']xmjs_detail_label_name["\'][^>]*>\s*([^<]+?)\s*</'
                r'[\s\S]*?'
                r'class\s*=\s*["\']xmjs_detail_label_cont["\'][^>]*>\s*([\s\S]*?)</span>',
                re.I,
            )
            for m in label_cont_re.finditer(html):
                label = compact_text(m.group(1))
                value = compact_text(re.sub(r'<[^>]+>', '', m.group(2)))
                if label and value and len(label) < 30:
                    key_values.setdefault(label, value)

            # 从纯文本中提取关键字段（后备）
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', '\n', text)
            for line in text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                if line.startswith('参考价格') and '参考价格' not in key_values:
                    key_values['参考价格'] = compact_text(line.replace('参考价格：', '').replace('参考价格', ''))
                elif line.startswith('所在地区') and '所在地区' not in key_values:
                    key_values['所在地区'] = compact_text(line.replace('所在地区：', '').replace('所在地区', ''))
                elif line.startswith('所属行业') and '所属行业' not in key_values:
                    key_values['所属行业'] = compact_text(line.replace('所属行业：', '').replace('所属行业', ''))
                elif line.startswith('招商主体') and '招商主体' not in key_values:
                    key_values['招商主体'] = compact_text(line.replace('招商主体：', '').replace('招商主体', ''))

            # 从 xmjs_price_num 提取价格
            for m in re.finditer(r'class\s*=\s*["\']xmjs_price_num["\'][^>]*>([\s\S]*?)</span>', html):
                raw = re.sub(r'<[^>]+>', '', m.group(1))
                raw = re.sub(r'[<>]', '', raw)
                price_raw = compact_text(raw)
                if price_raw:
                    key_values.setdefault("参考价格", price_raw)
                break

            # 提取标题
            title = None
            for m in re.finditer(r'class\s*=\s*["\'][^"\']*xmjs_detail_title[^"\']*["\']\s*>(.*?)</\w+>', html, re.DOTALL):
                t = compact_text(re.sub(r'<[^>]+>', '', m.group(1)))
                if len(t) > 10:
                    title = t
                    break
            if not title:
                for h in parser.headings:
                    if len(h) > 10:
                        title = h
                        break
            if not title:
                m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
                if m:
                    title = compact_text(re.sub(r'<[^>]+>', '', m.group(1)))

            # 项目编号
            if not _find_by_alias(key_values, ("项目编号",)):
                for m in re.finditer(r'\[?([A-Z]{2}\d{4}BJ\d{7})\]?', html):
                    key_values.setdefault("项目编号", m.group(1))
                    break

            # 图片（过滤 UI 元素）
            for m in re.finditer(r'<img[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', html):
                src = m.group(1)
                if any(kw in src for kw in ['static', 'logo', 'QRcode', 'bah', 'base64', 'captcha']):
                    continue
                if 'editorUpload' in src or 'upload' in src:
                    full_url = urljoin(OTC_BASE_URL, src)
                    if full_url not in image_urls:
                        image_urls.append(full_url)

            # 附件
            for m in re.finditer(r'<a[^>]*\bhref\s*=\s*["\']([^"\']+\.(?:(?:pdf|docx)?|xlsx?|(?:zip|rar)))["\'][^>]*>(.*?)</a>', html, re.IGNORECASE):
                url = urljoin(OTC_BASE_URL, m.group(1))
                name = compact_text(re.sub(r'<[^>]+>', '', m.group(2))) or url.rsplit("/", 1)[-1]
                attachments.append({"name": name, "url": url})

        source_item_id = (
            _find_by_alias(key_values, ("项目编号",))
            or (list_item.prj_id if list_item else None)
            or prj_id
        )
        title = title or (list_item.title if list_item else None) or source_item_id

        return CbexDetailBundle(
            url=OTCPRJ_DETAIL_URL.format(id=prj_id),
            prj_id=prj_id,
            html=html,
            title=title,
            source_item_id=source_item_id,
            key_values=key_values,
            image_urls=image_urls,
            attachments=attachments,
            list_item=list_item,
            raw_payloads={"detail_html": html},
        )

    # ===== 分类 =====
    def classify_bundle(self, bundle: CbexDetailBundle) -> str:
        trade_type = _find_by_alias(bundle.key_values, ("交易品类", "资产类型", "标的类型"))
        # 优先使用列表 API 返回的 businesstypename (更准确), 详情页可能缺失该字段
        if not trade_type and bundle.list_item:
            trade_type = bundle.list_item.trade_type
        return infer_asset_group(trade_type, bundle.title)

    # ===== 公共字段映射 =====
    def map_common_candidates(self, bundle: CbexDetailBundle) -> Dict[str, Any]:
        kv = bundle.key_values
        li = bundle.list_item
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

        title = first_non_blank(kv.get("项目名称"), kv.get("标的名称"), bundle.title)
        location = first_non_blank(kv.get("标的所在地"), kv.get("所在地区"), kv.get("地址"))
        price = first_non_blank(kv.get("转让底价"), kv.get("挂牌价格"), kv.get("挂牌价"), kv.get("参考价格"), kv.get("价格"))
        contact = join_non_blank(
            kv.get("联系人"), kv.get("项目负责人"), kv.get("联系方式"),
            kv.get("项目负责人联系方式"),
        )
        notice = first_non_blank(kv.get("特别告知"), kv.get("重大事项"), kv.get("风险提示"))
        disposal_party = first_non_blank(kv.get("转让方名称"), kv.get("转让方"), kv.get("招商主体"))
        asset_group = self.classify_bundle(bundle)
        detail_trade_type = _find_by_alias(kv, ("交易品类",))
        # 优先使用详情页交易品类, 缺失时 fallback 到列表 API 的 businesstypename
        if not detail_trade_type and li:
            detail_trade_type = li.trade_type
        asset_type = infer_asset_type(
            detail_trade_type,
            title,
        )
        # Signup / disclosure period
        signup_start = first_non_blank(
            kv.get("披露起止日期"), kv.get("信息披露起始日期"),
            kv.get("挂牌起始日期"), kv.get("报名开始时间"),
        )
        signup_end = first_non_blank(
            kv.get("披露截止日期"), kv.get("信息披露截止日期"),
            kv.get("挂牌截止日期"), kv.get("报名截止时间"),
        )
        # Parse "2026-07-08 至 2026-08-04" style date ranges
        date_range = first_non_blank(
            kv.get("披露起止日期"), kv.get("信息披露起止日期"),
            kv.get("挂牌起止日期"), kv.get("公告起止日期"),
        )
        if date_range:
            parts = re.split(r"\s+至\s+|\s+~\s+|\s*-\s*", date_range)
            if len(parts) >= 2:
                signup_start = signup_start or parts[0].strip()
                signup_end = signup_end or parts[1].strip()
        # Project status
        project_status = first_non_blank(kv.get("项目状态"), kv.get("状态"))
        if not project_status:
            haystack = " ".join(v for v in kv.values() if v)
            if "预披露" in haystack:
                project_status = "预披露"
            elif "正式披露" in haystack or "挂牌中" in haystack:
                project_status = "正式披露"
            elif "已成交" in haystack or "已结束" in haystack:
                project_status = "已成交"


        set_field("source_platform", self.source_platform, "computed", "adapter", self.source_platform, "constant", 1.0)
        set_field("source_site_name", self.source_site_name, "computed", "adapter", self.source_site_name, "constant", 1.0)
        set_field("source_item_id", bundle.source_item_id, "detail_html", "id", bundle.source_item_id, "html_rule", 1.0)
        set_field("source_url", bundle.url, "computed", "url", bundle.url, "request", 1.0)
        set_field("asset_group", asset_group, "computed", "infer", asset_group, "inference", 0.85)
        set_field("asset_type", asset_type or "产权转让", "computed", "infer", asset_type or "产权转让", "inference", 0.85)
        set_field("project_name", title, "detail_html", "title", title, "html_rule", 0.95)
        set_field("asset_location", location, "detail_html", "location", location)
        set_field("project_status", project_status, "detail_html", "status", project_status)
        set_field("disposal_party", disposal_party, "detail_html", "disposal_party", disposal_party)
        set_field("final_price_raw", price, "detail_html", "price", price)
        set_field("start_price_raw", price, "detail_html", "price", price)
        set_field("contact_info", contact, "detail_html", "contact", contact)
        set_field("special_notice", notice, "detail_html", "notice", notice)
        set_field("signup_start_time", signup_start, "detail_html", "signup_start", signup_start)
        set_field("signup_end_time", signup_end, "detail_html", "signup_end", signup_end)
        set_field("attachments_json", json.dumps(bundle.attachments, ensure_ascii=False), "detail_html", "attachments", "", "html_rule", 0.9)
        set_field("data_source", self.source_site_name, "computed", "adapter", self.source_site_name, "constant", 1.0)

        common["field_results"] = results
        return common

    # ===== 特殊字段映射 =====
    def map_special_candidates(self, bundle: CbexDetailBundle, asset_group: str) -> Dict[str, Any]:
        kv = bundle.key_values
        values: Dict[str, Any] = {}
        images = "; ".join(bundle.image_urls[:80])

        if asset_group == "equity":
            values.update({
                "transferor": first_non_blank(kv.get("转让方名称"), kv.get("转让方"), kv.get("招商主体")),
                "target_company": first_non_blank(kv.get("标的企业"), kv.get("企业名称"), kv.get("公司名称")),
                "equity_ratio": first_non_blank(kv.get("持股比例"), kv.get("转让比例"), kv.get("股权比例")),
                "company_nature": first_non_blank(kv.get("企业性质"), kv.get("企业类型"), kv.get("公司类型")),
                "company_industry": first_non_blank(kv.get("所属行业"), kv.get("行业")),
                "business_scope": kv.get("经营范围"),
                "disclosure_items": first_non_blank(kv.get("重大事项"), kv.get("风险提示"), kv.get("特别告知")),
                "site_images": images,
            })
        elif asset_group == "debt":
            values.update({
                "debtor_name": first_non_blank(kv.get("债务人"), kv.get("主债务人"), kv.get("借款人")),
                "creditor": first_non_blank(kv.get("债权人"), kv.get("转让方")),
                "principal_balance": first_non_blank(kv.get("本金余额"), kv.get("债权本金"), kv.get("借款本金")),
                "interest_balance": first_non_blank(kv.get("利息余额"), kv.get("欠息")),
                "claim_total": first_non_blank(kv.get("债权总额"), kv.get("债权金额")),
                "benchmark_date": first_non_blank(kv.get("基准日"), kv.get("债权基准日")),
                "collateral": first_non_blank(kv.get("抵押物"), kv.get("抵质押物"), kv.get("担保物")),
                "guarantor": first_non_blank(kv.get("保证人"), kv.get("担保人")),
                "site_images": images,
            })
        elif asset_group == "real_estate":
            values.update({
                "certificate_no": first_non_blank(kv.get("权证编号"), kv.get("不动产权证号"), kv.get("房产证号")),
                "building_area": first_non_blank(kv.get("建筑面积"), kv.get("房屋面积"), kv.get("面积")),
                "property_use": first_non_blank(kv.get("房产用途"), kv.get("规划用途"), kv.get("用途")),
                "property_location": first_non_blank(kv.get("房产位置"), kv.get("坐落"), kv.get("标的所在地")),
                "property_structure": kv.get("房产结构"),
                "property_status": first_non_blank(kv.get("房产状态"), kv.get("现状")),
                "disclosed_defects": first_non_blank(kv.get("瑕疵"), kv.get("风险提示")),
                "site_images": images,
            })
        elif asset_group == "land":
            values.update({
                "certificate_no": first_non_blank(kv.get("权证编号"), kv.get("土地证号")),
                "land_area": first_non_blank(kv.get("土地面积"), kv.get("宗地面积")),
                "land_use": first_non_blank(kv.get("土地用途"), kv.get("规划用途")),
                "land_location": first_non_blank(kv.get("土地位置"), kv.get("坐落"), kv.get("标的所在地")),
                "site_images": images,
            })
        elif asset_group == "vehicle":
            values.update({
                "storage_location": first_non_blank(kv.get("存放位置"), kv.get("车辆所在地")),
                "vehicle_brand": first_non_blank(kv.get("车辆品牌"), kv.get("品牌型号"), kv.get("车型")),
                "plate_number": first_non_blank(kv.get("车牌号"), kv.get("牌照号")),
                "vehicle_status": first_non_blank(kv.get("车辆状态"), kv.get("现状")),
                "vehicle_images": images,
            })
        elif asset_group == "equipment":
            values.update({
                "storage_location": first_non_blank(kv.get("存放位置"), kv.get("设备所在地")),
                "equipment_status": first_non_blank(kv.get("设备状态"), kv.get("现状")),
                "equipment_type": first_non_blank(kv.get("设备类型"), kv.get("设备名称")),
                "site_images": images,
            })
        else:
            values.update({
                "raw_detail_text": "",
                "raw_table_pairs_json": json.dumps(kv, ensure_ascii=False, sort_keys=True) if kv else None,
                "site_images": images,
            })

        return {k: v for k, v in values.items() if compact_text(str(v or ""))}

    # ===== AI 上下文 =====
    def build_ai_context(self, bundle: CbexDetailBundle) -> AIExtractionContext:
        sections = [
            f"source_platform: {self.source_platform}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.url}",
            f"title: {bundle.title}",
        ]
        if bundle.key_values:
            sections.append("key_values:\n" + json.dumps(bundle.key_values, ensure_ascii=False, indent=2))
        if bundle.attachments:
            sections.append("attachments:\n" + json.dumps(bundle.attachments, ensure_ascii=False, indent=2))
        if bundle.html:
            text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", bundle.html)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = compact_text(text)
            sections.append("detail_text:\n" + text[:8000])

        asset_group = self.classify_bundle(bundle)
        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(sections)[:12000],
            notice_text="",
            image_urls=list(bundle.image_urls),
            asset_group=asset_group,
            paimai_id=f"{self.source_platform}:{bundle.source_item_id}" if bundle.source_item_id else "",
        )
