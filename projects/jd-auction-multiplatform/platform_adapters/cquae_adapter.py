from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from jd.ai_extractor import AIExtractionContext


CQUAE_BASE_URL = "https://www.cquae.com"
CQUAE_LIST_PATH = "/Project"
CQUAE_DATA_SOURCE = "重庆联合产权交易所/重庆产权交易网"
CQUAE_PLATFORM = "cquae"
SDCQJY_BASE_URL = "http://www.sdcqjy.com"
SDCQJY_LIST_ENDPOINT = f"{SDCQJY_BASE_URL}/getZCData"
SDCQJY_DATA_SOURCE = "山东产权交易中心公开门户"

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


@dataclass
class _Link:
    href: str
    text: str


@dataclass
class _Cell:
    text: str
    links: List[_Link] = field(default_factory=list)


class _CquaeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[_Cell]] = []
        self.links: List[_Link] = []
        self.image_urls: List[str] = []
        self.headings: List[str] = []
        self.title_text: str = ""
        self._row: Optional[List[_Cell]] = None
        self._cell_parts: Optional[List[str]] = None
        self._cell_links: Optional[List[_Link]] = None
        self._link_href: Optional[str] = None
        self._link_parts: Optional[List[str]] = None
        self._title_parts: Optional[List[str]] = None
        self._heading_parts: Optional[List[str]] = None
        self._text_parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_map = {name.lower(): value for name, value in attrs if value is not None}
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell_parts = []
            self._cell_links = []
        elif tag == "a":
            self._link_href = attrs_map.get("href", "")
            self._link_parts = []
        elif tag == "title":
            self._title_parts = []
        elif tag in {"h1", "h2"}:
            self._heading_parts = []
        elif tag in {"img", "source"}:
            for key in ("src", "data-src", "data-original", "data-lazy-src"):
                image_url = compact_text(attrs_map.get(key))
                if image_url and image_url not in self.image_urls:
                    self.image_urls.append(image_url)

        if tag in {"p", "div", "br", "li", "tr", "table", "h1", "h2"}:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._link_href is not None:
            link = _Link(self._link_href, compact_text("".join(self._link_parts or [])))
            self.links.append(link)
            if self._cell_links is not None:
                self._cell_links.append(link)
            self._link_href = None
            self._link_parts = None
        elif tag in {"td", "th"} and self._cell_parts is not None:
            if self._row is not None:
                self._row.append(
                    _Cell(compact_text("".join(self._cell_parts)), list(self._cell_links or []))
                )
            self._cell_parts = None
            self._cell_links = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "title" and self._title_parts is not None:
            self.title_text = compact_text("".join(self._title_parts))
            self._title_parts = None
        elif tag in {"h1", "h2"} and self._heading_parts is not None:
            heading = compact_text("".join(self._heading_parts))
            if heading:
                self.headings.append(heading)
            self._heading_parts = None

        if tag in {"p", "div", "li", "tr", "table", "h1", "h2"}:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._link_parts is not None:
            self._link_parts.append(data)
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        self._text_parts.append(data)

    @property
    def text(self) -> str:
        lines: List[str] = []
        for line in "".join(self._text_parts).splitlines():
            clean = compact_text(line)
            if clean:
                lines.append(clean)
        return "\n".join(lines)


class CquaeBrowserFetcher:
    """Browser-rendered fallback for CQUAE pages protected by Knownsec JS challenges."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        profile_path: str | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.profile_path = profile_path

    def fetch_html(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._fetch_html_with_selenium(url)

        with sync_playwright() as playwright:
            if self.profile_path:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=self.profile_path,
                    headless=self.headless,
                )
                try:
                    page = context.new_page()
                    page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                    return page.content()
                finally:
                    context.close()
            browser = playwright.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                return page.content()
            finally:
                browser.close()

    def _fetch_html_with_selenium(self, url: str) -> str:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import WebDriverException
        except ImportError as exc:
            raise RuntimeError("CQUAE browser fallback requires Playwright or Selenium.") from exc

        last_error: Exception | None = None
        for browser_name, driver_factory, options_factory in (
            ("chrome", webdriver.Chrome, webdriver.ChromeOptions),
            ("edge", webdriver.Edge, webdriver.EdgeOptions),
        ):
            options = options_factory()
            if self.headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1365,900")
            if self.profile_path and browser_name == "chrome":
                options.add_argument(f"--user-data-dir={self.profile_path}")
            options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
            driver = None
            try:
                driver = driver_factory(options=options)
                if self.timeout_ms and self.timeout_ms > 0:
                    driver.set_page_load_timeout(max(1, int(self.timeout_ms / 1000)))
                driver.get(url)
                time.sleep(5)
                return driver.page_source
            except WebDriverException as exc:
                last_error = exc
            finally:
                if driver is not None:
                    driver.quit()
        raise RuntimeError(f"CQUAE browser fallback failed: {last_error}")


class CquaeAdapter:
    def __init__(self, *, base_url: str = CQUAE_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def build_list_url(
        self,
        *,
        page: int,
        project_id: int = 1,
        nt: int = 1,
        price_id: int = 32,
        type_id: Optional[int] = None,
    ) -> str:
        params = {
            "q": "s",
            "projectID": str(project_id),
            "nt": str(nt),
            "priceID": str(price_id),
            "page": str(page),
        }
        if type_id is not None:
            params["type"] = str(type_id)
        return f"{self.base_url}{CQUAE_LIST_PATH}?{urlencode(params)}"

    def is_waf_challenge(self, html: Optional[str], status_code: Optional[int] = None) -> bool:
        if status_code == 521:
            return True
        body = (html or "").lower()
        return any(marker in body for marker in WAF_MARKERS)

    def response_status(self, html: Optional[str], status_code: Optional[int] = None) -> str:
        return "needs_browser" if self.is_waf_challenge(html, status_code) else "ok"

    def parse_list_html(self, html: str, base_url: str = CQUAE_BASE_URL) -> List[CquaeListItem]:
        parser = parse_html(html)
        items: List[CquaeListItem] = []
        headers: Optional[List[str]] = None
        seen_ids: set[str] = set()

        for row in parser.rows:
            cells = [cell for cell in row if cell.text or cell.links]
            if not cells:
                continue
            detail_links = [
                link
                for cell in cells
                for link in cell.links
                if extract_project_id(link.href)
            ]
            if not detail_links:
                candidate_headers = [normalize_label(cell.text) for cell in cells]
                if candidate_headers and all(looks_like_label(value) for value in candidate_headers):
                    headers = candidate_headers
                continue

            raw_text = compact_text(" ".join(cell.text for cell in cells))
            raw_fields = fields_from_cells(cells, headers)
            for link in detail_links:
                source_item_id = extract_project_id(link.href)
                if not source_item_id or source_item_id in seen_ids:
                    continue
                seen_ids.add(source_item_id)
                source_url = urljoin(base_url, link.href)
                title = best_title(link.text, cells)
                item = CquaeListItem(
                    source_item_id=source_item_id,
                    source_url=source_url,
                    title=title,
                    project_type=find_by_alias(raw_fields, ("项目类型", "标的类型", "资产类型", "类型"))
                    or infer_project_type(" ".join([title, raw_text])),
                    project_status=find_by_alias(raw_fields, ("项目状态", "披露状态", "状态"))
                    or infer_project_status(raw_text),
                    price_raw=find_by_alias(raw_fields, ("挂牌价", "转让底价", "底价", "价格")),
                    deposit_raw=find_by_alias(raw_fields, ("保证金", "交易保证金")),
                    date_text=find_by_alias(raw_fields, ("披露起止日期", "披露日期", "公告日期", "日期")),
                    contact_info=find_by_alias(raw_fields, ("联系人", "联系电话", "联系方式", "咨询电话")),
                    raw_fields=raw_fields,
                    raw_text=raw_text,
                )
                items.append(item)

        if items:
            return items
        return list_items_from_links(parser.links, base_url)

    def parse_sdcqjy_list_html(self, html: str, base_url: str = SDCQJY_BASE_URL) -> List[CquaeListItem]:
        """Parse SPREC/SDCQJY public list rows.

        The page stores the row JSON inside linkToDetail(...). We reuse the
        site's own detail routing logic instead of guessing hidden endpoints.
        """
        rows = re.findall(r"<tr\b[\s\S]*?</tr>", html or "", flags=re.I)
        items: List[CquaeListItem] = []
        seen: set[str] = set()
        headers: List[str] = []
        for row in rows:
            cells = [compact_text(re.sub(r"<[^>]+>", " ", cell)) for cell in re.findall(r"<t[dh]\b[^>]*>([\s\S]*?)</t[dh]>", row, flags=re.I)]
            if cells and all(looks_like_label(cell) for cell in cells):
                headers = [normalize_label(cell) for cell in cells]
                continue
            match = re.search(r'onclick=["\']linkToDetail\((.*?)\)["\']', row, flags=re.I | re.S)
            if not match:
                continue
            try:
                payload = json.loads(unescape(match.group(1)))
            except Exception:
                continue
            source_item_id = compact_text(str(payload.get("code") or payload.get("proNo") or payload.get("id") or ""))
            if not source_item_id or source_item_id in seen:
                continue
            seen.add(source_item_id)
            raw_fields = {str(key): compact_text(str(value)) for key, value in payload.items() if value is not None}
            row_fields = fields_from_texts(cells, headers)
            raw_fields.update(row_fields)
            title = first_non_blank(
                raw_fields.get("项目名称"),
                raw_fields.get("name"),
                raw_fields.get("title"),
                raw_fields.get("标的名称"),
            ) or source_item_id
            detail_url = sdcqjy_detail_url(payload, base_url=base_url)
            price_raw = first_non_blank(
                raw_fields.get("挂牌价格"),
                raw_fields.get("pubPrice"),
                raw_fields.get("pubAmount"),
                price_display_from_payload(payload),
            )
            items.append(
                CquaeListItem(
                    source_item_id=source_item_id,
                    source_url=detail_url,
                    title=title,
                    project_type=first_non_blank(raw_fields.get("assetType"), raw_fields.get("propertyType"), raw_fields.get("corpInd")),
                    project_status=first_non_blank(raw_fields.get("proStage"), raw_fields.get("projectStatus"), raw_fields.get("项目状态")),
                    price_raw=price_raw,
                    date_text=first_non_blank(raw_fields.get("截止日期"), raw_fields.get("endDate"), raw_fields.get("pubDate")),
                    raw_fields=raw_fields,
                    raw_text=compact_text(" ".join(cells)) or title,
                )
            )
        return items

    def parse_detail_html(
        self,
        html: str,
        url: str = "",
        list_item: Optional[CquaeListItem] = None,
    ) -> CquaeDetailBundle:
        parser = parse_html(html)
        source_url = urljoin(self.base_url, url) if url else ""
        source_item_id = extract_project_id(source_url) or (list_item.source_item_id if list_item else "")
        key_values = extract_key_values(parser)
        title = first_non_blank(
            *(parser.headings or []),
            find_by_alias(key_values, ("项目名称", "标的名称", "名称")),
            list_item.title if list_item else None,
            parser.title_text,
        )
        attachments = extract_attachments(parser.links, source_url or self.base_url)
        image_urls = dedupe_texts(
            urljoin(source_url or self.base_url, image_url)
            for image_url in parser.image_urls
            if image_url
        )

        return CquaeDetailBundle(
            source_item_id=source_item_id,
            source_url=source_url,
            title=title or "",
            key_values=key_values,
            attachments=attachments,
            image_urls=image_urls,
            detail_text=parser.text,
            list_item=list_item,
            raw_html=html or "",
        )

    def build_ai_context(self, bundle: CquaeDetailBundle) -> AIExtractionContext:
        sections = [
            f"source_platform: {CQUAE_PLATFORM}",
            f"source_item_id: {bundle.source_item_id}",
            f"source_url: {bundle.source_url}",
            f"title: {bundle.title}",
        ]
        if bundle.list_item:
            sections.append("list_text:\n" + bundle.list_item.raw_text)
            if bundle.list_item.raw_fields:
                sections.append("list_fields:\n" + format_pairs(bundle.list_item.raw_fields))
        if bundle.key_values:
            sections.append("detail_key_values:\n" + format_pairs(bundle.key_values))
        if bundle.attachments:
            attachment_lines = [
                f"- {attachment.get('name') or ''}: {attachment.get('url') or ''}"
                for attachment in bundle.attachments
            ]
            sections.append("attachments:\n" + "\n".join(attachment_lines))
        if bundle.detail_text:
            sections.append("detail_text:\n" + bundle.detail_text)
        # Raw HTML is kept in raw_payloads for audit; the AI context should favor visible text.
        # Including the full HTML here crowds out useful page content on long project pages.
        return AIExtractionContext(
            html_key_values=dict(bundle.key_values),
            detail_text="\n\n".join(section for section in sections if section)[:30000],
            notice_text="",
            image_urls=list(bundle.image_urls),
            asset_group=classify_asset_group(
                infer_asset_type_from_sources(bundle.key_values, bundle.list_item, bundle.title, bundle.detail_text),
                bundle.title,
                bundle.detail_text,
            ),
            paimai_id=f"{CQUAE_PLATFORM}:{bundle.source_item_id}" if bundle.source_item_id else "",
        )

    def map_common_candidates(self, bundle: CquaeDetailBundle) -> Dict[str, Optional[str]]:
        key_values = bundle.key_values
        list_item = bundle.list_item
        project_name = first_non_blank(
            find_by_alias(key_values, ("项目名称", "标的名称", "名称")),
            bundle.title,
            list_item.title if list_item else None,
        )
        asset_type = infer_asset_type_from_sources(key_values, list_item, project_name, bundle.detail_text)
        final_price = first_non_blank(
            find_by_alias(key_values, ("转让底价", "挂牌价", "挂牌价格", "租金底价", "底价")),
            list_item.price_raw if list_item else None,
        )
        start_price = first_non_blank(
            find_by_alias(key_values, ("起始价", "起拍价", "竞价起始价")),
        )
        contact_info = join_non_blank(
            find_by_alias(key_values, ("联系人", "交易机构联系人", "项目联系人", "看货联系人")),
            find_by_alias(key_values, ("联系电话", "交易机构联系电话", "联系方式", "咨询电话", "看货联系电话")),
            list_item.contact_info if list_item else None,
        )
        assessment_price = assessment_price_from_key_values(key_values)
        assessment_date = find_by_alias(key_values, ("评估基准日", "评估日期", "评估时间"))
        assessment_text = join_non_blank(
            f"评估价：{assessment_price}" if assessment_price else None,
            f"评估基准日：{assessment_date}" if assessment_date else None,
        )

        return {
            "source_platform": CQUAE_PLATFORM,
            "source_item_id": bundle.source_item_id,
            "source_url": bundle.source_url,
            "asset_group": classify_asset_group(asset_type, project_name, bundle.detail_text),
            "asset_type": asset_type,
            "project_name": project_name,
            "asset_location": find_by_alias(
                key_values,
                ("标的所在地", "项目所在地", "资产所在地", "所在地", "坐落", "地址"),
            ),
            "project_status": first_non_blank(
                find_by_alias(key_values, ("项目状态", "披露状态", "状态")),
                list_item.project_status if list_item else None,
            ),
            "signup_start_time": first_non_blank(
                find_by_alias(key_values, ("挂牌日期", "挂牌开始日期", "挂牌起始日期", "开始日期")),
                list_item.raw_fields.get("startDate") if list_item else None,
            ),
            "signup_end_time": first_non_blank(
                find_by_alias(key_values, ("挂牌截止日期", "挂牌截止时间", "截止日期")),
                list_item.date_text if list_item else None,
            ),
            "disposal_party": find_by_alias(key_values, ("转让方", "委托方", "出让方", "出租方", "转让单位")),
            "disposal_agency": find_by_alias_filtered(
                key_values,
                ("处置机构", "交易机构", "组织机构", "交易中心", "服务机构"),
                forbidden=("联系人", "联系电话", "电话", "评估", "中介"),
            ),
            "start_price_raw": start_price,
            "final_price_raw": final_price,
            "contact_info": contact_info,
            "special_notice": find_by_alias(
                key_values,
                ("重要信息披露", "特别告知", "特别提示", "重大事项", "其他披露事项", "风险提示"),
            ),
            "assessment_price_time": assessment_text,
            "attachments_json": json.dumps(bundle.attachments, ensure_ascii=False),
            "data_source": CQUAE_DATA_SOURCE,
        }

    def classify_bundle(self, bundle: CquaeDetailBundle) -> str:
        asset_type = infer_asset_type_from_sources(
            bundle.key_values,
            bundle.list_item,
            bundle.title,
            bundle.detail_text,
        )
        return classify_asset_group(asset_type, bundle.title, bundle.detail_text)

    def map_special_candidates(self, bundle: CquaeDetailBundle, asset_group: str) -> Dict[str, Any]:
        key_values = bundle.key_values
        text = bundle.detail_text or ""
        images = "; ".join(bundle.image_urls[:80])
        values: Dict[str, Any] = {}

        if asset_group == "equipment":
            values.update(
                {
                    "storage_location": find_by_alias(key_values, ("设备存放地", "存放地", "存放位置", "所在地", "地址")),
                    "equipment_status": find_by_alias(key_values, ("设备现状", "设备状态", "标的状态", "现状", "状态")),
                    "equipment_type": first_non_blank(
                        find_by_alias(key_values, ("设备类型", "资产类别", "标的类型", "项目类型")),
                        infer_project_type(" ".join([bundle.title, text])),
                    ),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                    "site_images": images,
                }
            )
        elif asset_group == "vehicle":
            values.update(
                {
                    "storage_location": find_by_alias(key_values, ("车辆存放地", "车辆停放地", "存放地", "看货地点", "所在地", "地址")),
                    "vehicle_brand_model": join_non_blank(
                        find_by_alias(key_values, ("机动车品牌", "品牌型号", "车辆品牌", "厂牌型号", "车型品牌")),
                        find_by_alias(key_values, ("规格型号", "车辆型号")),
                    ),
                    "vehicle_usage": join_non_blank(
                        find_by_alias(key_values, ("行驶里程数", "行驶里程", "使用情况")),
                        find_by_alias(key_values, ("年检至", "年检有效期")),
                        find_by_alias(key_values, ("交强险有效期", "保险")),
                    ),
                    "plate_number": first_non_blank(
                        find_by_alias(key_values, ("车牌号码", "车牌号", "牌照号")),
                        extract_plate_number(text),
                    ),
                    "vehicle_configuration": join_non_blank(
                        find_by_alias(key_values, ("排量", "排量（ml）")),
                        find_by_alias(key_values, ("颜色",)),
                        find_by_alias(key_values, ("车架号",)),
                    ),
                    "vehicle_status": join_non_blank(
                        find_by_alias(key_values, ("标的状态", "车辆状态", "车辆现状")),
                        find_by_alias(key_values, ("详细信息",)),
                    ),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                    "vehicle_type": first_non_blank(find_by_alias(key_values, ("车辆类型", "资产类别", "标的类型")), "机动车"),
                    "vehicle_images": images,
                }
            )
        elif asset_group == "real_estate":
            values.update(
                {
                    "right_certificate_no": clean_certificate_no(
                        find_by_alias(key_values, ("权证编号", "权证", "不动产权证", "证号"))
                    ),
                    "building_area": first_non_blank(
                        find_by_alias(key_values, ("建筑面积", "出租面积", "证载建筑面积", "面积")),
                        extract_area(text),
                    ),
                    "property_use": find_by_alias(key_values, ("房产用途", "出租用途", "规划用途", "用途")),
                    "use_term": find_by_alias(key_values, ("使用年限", "出租期限", "租赁期限", "使用期限")),
                    "property_location": find_by_alias(key_values, ("房产位置", "坐落", "地址", "所在地", "标的所在地")),
                    "property_structure": find_by_alias(key_values, ("房产结构", "房屋结构", "结构")),
                    "property_status": join_non_blank(
                        find_by_alias(key_values, ("房产状态", "出租情况", "标的状态", "现状")),
                        find_by_alias(key_values, ("详细信息",)),
                    ),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                    "property_type": first_non_blank(find_by_alias(key_values, ("房产类型", "物业类型", "资产类别", "标的类型")), infer_project_type(bundle.title)),
                    "asset_highlights": find_by_alias(key_values, ("资产亮点", "详细信息", "项目亮点")),
                    "site_images": images,
                }
            )
        elif asset_group == "land":
            values.update(
                {
                    "right_certificate_no": clean_certificate_no(
                        find_by_alias(key_values, ("权证编号", "土地证号", "不动产权证", "证号"))
                    ),
                    "land_area": first_non_blank(find_by_alias(key_values, ("土地面积", "宗地面积", "证载面积")), extract_area(text)),
                    "land_use": find_by_alias(key_values, ("土地用途", "规划用途", "用途")),
                    "use_term": find_by_alias(key_values, ("使用期限", "土地使用期限", "使用年限")),
                    "land_location": find_by_alias(key_values, ("土地位置", "坐落", "地址", "所在地", "标的所在地")),
                    "land_status": join_non_blank(
                        find_by_alias(key_values, ("土地状态", "标的状态", "现状")),
                        find_by_alias(key_values, ("详细信息",)),
                    ),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                    "land_type": find_by_alias(key_values, ("土地类型", "权利性质", "资产类别", "标的类型")),
                    "assessment_time_value": join_non_blank(
                        assessment_price_from_key_values(key_values),
                        find_by_alias(key_values, ("评估基准日", "评估日期", "评估时间")),
                    ),
                    "site_images": images,
                }
            )
        elif asset_group == "usufruct":
            values.update(
                {
                    "right_category": first_non_blank(find_by_alias(key_values, ("资产类别", "标的类型", "项目类型")), infer_project_type(bundle.title)),
                    "subject_name": first_non_blank(find_by_alias(key_values, ("标的名称", "项目名称")), bundle.title),
                    "subject_location": find_by_alias(key_values, ("地址", "所在地", "标的所在地")),
                    "subject_details": find_by_alias(key_values, ("详细信息", "重大事项及其他披露内容")),
                    "valid_period": find_by_alias(key_values, ("出租期限", "租赁期限", "使用期限", "有效期")),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                }
            )
        elif asset_group == "goods":
            values.update(
                {
                    "goods_category": first_non_blank(find_by_alias(key_values, ("资产类别", "物资种类", "标的类型")), infer_project_type(bundle.title)),
                    "goods_name": first_non_blank(find_by_alias(key_values, ("标的名称", "项目名称")), bundle.title),
                    "goods_location": find_by_alias(key_values, ("存放地", "所在地", "地址")),
                    "goods_details": find_by_alias(key_values, ("详细信息", "重大事项及其他披露内容")),
                    "disclosed_defects": find_by_alias(key_values, ("重大事项及其他披露内容", "重要信息披露", "瑕疵", "风险提示")),
                }
            )
        elif asset_group == "other":
            values.update(
                {
                    "raw_detail_text": text[:12000],
                    "raw_table_pairs_json": json.dumps(key_values, ensure_ascii=False, sort_keys=True),
                    "extracted_summary": first_non_blank(
                        find_by_alias(key_values, ("详细信息", "重大事项及其他披露内容")),
                        bundle.title,
                    ),
                }
            )

        return {key: value for key, value in values.items() if compact_text(str(value or ""))}


def parse_html(html: str) -> _CquaeHTMLParser:
    parser = _CquaeHTMLParser()
    parser.feed(html or "")
    parser.close()
    return parser


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
    if len(texts) >= 2 and len(texts) % 2 == 0:
        for key, value in zip(texts[0::2], texts[1::2]):
            clean_key = normalize_label(key)
            clean_value = compact_text(value)
            if looks_like_label(clean_key) and clean_value:
                fields[clean_key] = clean_value
    return fields


def fields_from_texts(texts: List[str], headers: Optional[List[str]]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    if headers and len(headers) == len(texts):
        for header, value in zip(headers, texts):
            clean_header = normalize_label(header)
            clean_value = compact_text(value)
            if clean_header and clean_value:
                fields[clean_header] = clean_value
    return fields


def price_display_from_payload(payload: Dict[str, object]) -> Optional[str]:
    value = payload.get("price")
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return compact_text(str(value))
    if amount == 0:
        return None
    if amount % 10000 == 0:
        return f"{amount / 10000:.0f}万元"
    text = f"{amount / 10000:.6f}".rstrip("0").rstrip(".")
    return f"{text}万元"


def sdcqjy_detail_url(payload: Dict[str, object], *, base_url: str = SDCQJY_BASE_URL) -> str:
    redirect = compact_text(str(payload.get("redirectUrl") or ""))
    if redirect:
        return urljoin(base_url, redirect)
    item_id = compact_text(str(payload.get("id") or ""))
    if not item_id:
        return base_url
    item_type = compact_text(str(payload.get("type") or "tc"))
    system_source = compact_text(str(payload.get("systemSource") or ""))
    if system_source in {"12", "13"}:
        bid_mode = compact_text(str(payload.get("bidMode") or ""))
        prefix = "biddingForEnergy" if bid_mode == "3" else "bidding/bidprice"
        return urljoin(base_url, f"/{prefix}/{item_id}")
    if item_type == "bid":
        bid_mode = compact_text(str(payload.get("bidMode") or ""))
        prefix = "biddingForEnergy" if bid_mode == "3" else "bidding/bidprice"
        return urljoin(base_url, f"/{prefix}/{item_id}")
    if item_type == "pw":
        return urljoin(base_url, f"/proj/pw/{item_id}")
    return urljoin(base_url, f"/proj/tc/{item_id}")


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


ATTACHMENT_EXT_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|xls|zip|rar|pptx?|jpg|jpeg|png)(?:$|[?#])", re.I)


def is_attachment_link(link: _Link) -> bool:
    href = compact_text(link.href).lower()
    text = compact_text(link.text)
    if not href or href.startswith(("javascript:", "about:", "#")):
        return False
    if "dw.ashx" in href or "/attachment/" in href or "noauthorizefiles" in href:
        return True
    if ATTACHMENT_EXT_RE.search(href):
        return True
    return any(keyword in text for keyword in ("附件", "下载", "材料", "承诺", "查看附件", "报告", "清单"))


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
    if re.search(r"ICP|备案|许可证|营业执照", clean, flags=re.I):
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
    match = re.search(r"(?:建筑面积|土地面积|出租面积|面积|宗地面积)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:平方米|㎡|平米|m²))", text or "", flags=re.I)
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
