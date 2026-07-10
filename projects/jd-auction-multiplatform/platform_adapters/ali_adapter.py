
import json
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

import requests

from jd.ai_extractor import AIExtractionContext, AI_DETAIL_TEXT_LIMIT


ALI_SOURCE_PLATFORM = "ali"
ALI_DEFAULT_PROFILE_ENV = "ALI_PLAYWRIGHT_PROFILE_PATH"
ALI_DEFAULT_DETAIL_URL = "https://zc-paimai.taobao.com/auction.htm?itemId={item_id}"
ALI_MTOP_APP_KEY = os.getenv("ALI_MTOP_APP_KEY", "24679788")
ALI_LIST_PAGE_ID = 1900755
ALI_LIST_MODULE_ID = "9018433170"
ALI_LIST_DF_API_NAME = "auctionwalle.datou.getPageModulesData"

ALI_ASSET_GROUP_LABELS = {
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


@dataclass(frozen=True)
class AliListChannel:
    key: str
    label: str
    asset_group: str
    page_id: int
    scene_code: str = ""
    spm: str = ""
    module_id: str = ALI_LIST_MODULE_ID
    page_size: int = 60
    url: str = ""
    fcat_v4_ids: tuple[str, ...] = ()


ALI_DEFAULT_CHANNEL = AliListChannel(
    key="default",
    label="阿里拍卖",
    asset_group="other",
    page_id=ALI_LIST_PAGE_ID,
    url="https://zc-paimai.taobao.com/",
)

ALI_REAL_ESTATE_CHANNEL = AliListChannel(
    key="real_estate",
    label="房地产",
    asset_group="real_estate",
    page_id=1410667,
    scene_code="20200713C5R32B6N",
    spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-1-1",
    url=(
        "https://zc-paimai.taobao.com/wow/pm/default/pc/4b80fa"
        "?spm=a2129.27064540.puimod-zc-focus-2021_2860107850.category-1-1"
    ),
)

ALI_LIST_CHANNELS = (
    AliListChannel(key="zspl_zz", label="住宅用房", asset_group="real_estate",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206060601",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zspl_sy", label="商业用房", asset_group="real_estate",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206057102",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zspl_gy", label="工业用房", asset_group="real_estate",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206051702",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zspl_qt", label="其他用房", asset_group="real_estate",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206060701","206060202"),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="cl", label="机动车", asset_group="vehicle",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206053405",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="cb", label="船舶", asset_group="vehicle",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067401",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="qtjtgj", label="其他交通工具", asset_group="vehicle",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206137507","206146502","206149901"),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="gq", label="股权", asset_group="equity",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067201",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zq", label="债权", asset_group="debt",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067301",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="td", label="土地", asset_group="land",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067101",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="kq", label="矿权", asset_group="usufruct",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206068001",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="lq", label="林权", asset_group="usufruct",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067901",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="jxsb", label="机械设备", asset_group="equipment",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067001",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zjgc", label="在建工程", asset_group="other",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206146002",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="hy", label="海域", asset_group="usufruct",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206146904",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="wxzc", label="无形资产", asset_group="other",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206165202",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="ylbjl", label="原材料/边角料", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206067601",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="txsb", label="通信设备", asset_group="equipment",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206149902",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="scp", label="奢侈品", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206054502",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="zbss", label="珠宝首饰", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206059902",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="wwsc", label="文玩收藏", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206080001",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="sj", label="手机", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206058004",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="dn", label="电脑", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206057705",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="qtsm", label="其他数码", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD",
        fcat_v4_ids=("206067802","206167303","206215205","206224704","206215504","206228901","206479511"),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="jj", label="家具", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206147503",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="dq", label="电器", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206140005",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="bjj", label="白酒", asset_group="goods",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206060901",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="wjj", label="挖掘机/叉车", asset_group="equipment",
        page_id=1910955, scene_code="20210823QCG72BUD", fcat_v4_ids=("206148603",),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    AliListChannel(key="ali_qt", label="其他", asset_group="other",
        page_id=1910955, scene_code="20210823QCG72BUD",
        fcat_v4_ids=("206143604","206136908","206134505","206220804","206147403","206069101","206068701",
                     "206152901","206136704","206057702","206061101","206136406","206061001","206060102",
                     "206051502","206072501","206071001","206139805","206059302"),
        spm="a2129.27064540.puimod-zc-focus-2021_2860107850.category-4-5"),
    ALI_REAL_ESTATE_CHANNEL,
    ALI_DEFAULT_CHANNEL,
)


@dataclass
class AliListItem:
    item_id: str
    title: str = ""
    source_url: str = ""
    category: str = ""
    asset_group: str = "other"
    asset_location: str = ""
    project_status: str = ""
    start_price_raw: Optional[str] = None
    final_price_raw: Optional[str] = None
    price_basis: str = ""
    source_excerpt: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AliDetailBundle:
    source_platform: str = ALI_SOURCE_PLATFORM
    source_item_id: str = ""
    source_url: str = ""
    title: str = ""
    category: str = ""
    asset_group: str = "other"
    asset_type: str = ""
    asset_location: str = ""
    project_status: str = ""
    start_price_raw: Optional[str] = None
    final_price_raw: Optional[str] = None
    price_basis: str = ""
    source_excerpt: str = ""
    contact_info: str = ""
    special_notice: str = ""
    signup_start_time: str = ""
    signup_end_time: str = ""
    disposal_party: str = ""
    disposal_agency: str = ""
    assessment_price_time: str = ""
    attachments: list[Any] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    summary_fields: Mapping[str, str] = field(default_factory=dict)
    notice_html: str = ""
    notice_text: str = ""
    top_json: Any = None
    rendered_html: str = ""
    rendered_text: str = ""
    page_text: str = ""
    list_item: Optional[AliListItem] = None
    status: str = "ok"
    block_reason: str = ""
    data_source: str = ""


class AliTopApiFetcher:
    """TOP API helper with no built-in credentials or network transport."""

    def __init__(
        self,
        *,
        app_key: Optional[str] = None,
        endpoint: str = "https://eco.taobao.com/router/rest",
        list_method: str = "ali.asset.auction.list",
        detail_method: str = "ali.asset.auction.detail",
        api_version: str = "2.0",
        sign_method: str = "md5",
    ) -> None:
        self.app_key = app_key
        self.endpoint = endpoint
        self.list_method = list_method
        self.detail_method = detail_method
        self.api_version = api_version
        self.sign_method = sign_method

    def build_request_params(
        self,
        method: str,
        *,
        session: Optional[str] = None,
        timestamp: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "method": method,
            "format": "json",
            "v": self.api_version,
            "sign_method": self.sign_method,
        }
        if self.app_key:
            params["app_key"] = self.app_key
        if session:
            params["session"] = session
        if timestamp:
            params["timestamp"] = timestamp
        if extra:
            params.update({key: value for key, value in extra.items() if value is not None})
        return params

    def build_list_params(self, *, page_no: int = 1, page_size: int = 20, **filters: Any) -> dict[str, Any]:
        extra = {"page_no": page_no, "page_size": page_size, **filters}
        return self.build_request_params(self.list_method, extra=extra)

    def build_detail_params(self, item_id: str, **extra: Any) -> dict[str, Any]:
        payload = {"item_id": item_id, **extra}
        return self.build_request_params(self.detail_method, extra=payload)

    def fetch_list(self, transport: Callable[[dict[str, Any]], Mapping[str, Any]], **params: Any) -> list[AliListItem]:
        return self.parse_list(transport(self.build_list_params(**params)))

    def fetch_detail(
        self,
        transport: Callable[[dict[str, Any]], Mapping[str, Any]],
        item_id: str,
        **params: Any,
    ) -> AliDetailBundle:
        return self.parse_detail(transport(self.build_detail_params(item_id, **params)))

    def parse_list(self, json_data: Mapping[str, Any]) -> list[AliListItem]:
        return [_parse_list_item(item) for item in _find_item_dicts(json_data)]

    def parse_detail(self, json_data: Mapping[str, Any]) -> AliDetailBundle:
        return _parse_detail_json(json_data)


class AliMtopAuctionFetcher:
    """Public mtop data source used by the Ali asset-auction web app.

    This fetcher avoids a logged-in browser session for scheduled crawls. It
    signs H5 mtop requests with the public app key and keeps the token cookie
    inside one requests session.
    """

    def __init__(
        self,
        *,
        app_key: str = ALI_MTOP_APP_KEY,
        timeout: int | float | None = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.app_key = app_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://zc-paimai.taobao.com",
                "Referer": "https://zc-paimai.taobao.com/",
            }
        )

    def fetch_list(
        self,
        *,
        limit: int = 10,
        channels: Optional[Iterable[AliListChannel]] = None,
        pages_per_channel: int = 2,
    ) -> list[AliListItem]:
        # Warm up the session: visit the homepage once so the server sets the
        # _m_h5_tk cookie (needed for MTOP request signing).  Without this
        # cookie every API call returns TOKEN_EMPTY and the listing fails.
        if not self.session.cookies.get("_m_h5_tk"):
            try:
                self.session.get(
                    "https://zc-paimai.taobao.com/",
                    timeout=self.timeout,
                )
            except Exception:
                pass  # non-fatal; the first API call will report the real error
        target_count = None if limit is None or limit <= 0 else limit
        selected_channels = list(channels or ALI_LIST_CHANNELS)
        max_pages = max(1, int(pages_per_channel or 1))
        items: list[AliListItem] = []
        seen_item_ids: set[str] = set()

        for channel in selected_channels:
            for page_no in range(1, max_pages + 1):
                response = self._fetch_channel_page(channel, page_no)
                scheme_list = _extract_ali_datafront_scheme_list(response)
                metadata = _extract_ali_datafront_items_metadata(response)
                if not scheme_list:
                    break

                for raw_item in scheme_list:
                    item = _parse_ali_datafront_list_item(raw_item)
                    if not item.item_id or item.item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item.item_id)
                    item = _with_ali_channel_metadata(item, channel, page_no, metadata)
                    items.append(item)
                    if target_count is not None and len(items) >= target_count:
                        return items

                if metadata.get("hasNextPage") is False:
                    break

        return items

    def _fetch_channel_page(self, channel: AliListChannel, page_no: int) -> Mapping[str, Any]:
        if channel.key == ALI_DEFAULT_CHANNEL.key and not channel.spm:
            variables = {
                "pageId": channel.page_id,
                "moduleIds": channel.module_id,
                "context": {},
            }
            variables_text = json.dumps(variables, ensure_ascii=False, separators=(",", ":"))
            payload = {
                "dfApp": "auctionwalle",
                "dfApiName": ALI_LIST_DF_API_NAME,
                "dfVariables": variables_text,
                "dfUniqueId": f"{channel.page_id}.{channel.module_id}",
                "dfVariablesRecover": variables_text,
            }
        else:
            module_ids = f"{channel.module_id}:items~keywordSource"
            context = _build_ali_channel_context(channel, page_no)
            payload = _build_ali_datafront_payload(
                page_id=channel.page_id,
                module_ids=module_ids,
                context=context,
                unique_module_ids=module_ids,
            )
        return self._call_mtop(
            "mtop.taobao.datafront.invoke.auctionwalle",
            "1.0",
            payload,
        )

    def fetch_detail(self, list_item: AliListItem) -> AliDetailBundle:
        item_id = compact_item_id(list_item.item_id)
        if not item_id:
            raise RuntimeError("Ali mtop detail requires item_id")

        detail_json = self._call_mtop(
            "mtop.taobao.gov.auction.third.detail.get",
            "1.0",
            {"itemId": item_id},
        )
        detail_data = detail_json.get("data") if isinstance(detail_json, Mapping) else {}
        if not isinstance(detail_data, Mapping) or not detail_data:
            raise RuntimeError(f"Ali detail mtop returned empty data for {item_id}")

        description_json: Mapping[str, Any] = {}
        description = detail_data.get("description")
        if isinstance(description, Mapping):
            desc_key = description.get("file")
            desc_key2 = description.get("file2")
            desc_version = str(description.get("apiIdentity") or "3.0")
            if desc_key:
                try:
                    description_json = self._call_mtop(
                        "mtop.com.taobao.govdetail.description.content.get",
                        desc_version,
                        {"itemId": item_id, "key": desc_key, "key2": desc_key2},
                    )
                except Exception as exc:
                    description_json = {"error": str(exc)}

        attachments_json: Mapping[str, Any] = {}
        cat_id = detail_data.get("catId")
        if cat_id is not None:
            try:
                attachments_json = self._call_mtop(
                    "mtop.com.taobao.auction.item.attach.get",
                    "1.0",
                    {"itemId": item_id, "catId": cat_id},
                )
            except Exception as exc:
                attachments_json = {"error": str(exc)}

        summary_json: Mapping[str, Any] = {}
        try:
            summary_json = self._call_mtop(
                "mtop.taobao.paimai.detail.summary.query",
                "1.0",
                {"itemId": item_id},
            )
        except Exception as exc:
            summary_json = {"error": str(exc)}

        notice_json: Mapping[str, Any] = {}
        notice = detail_data.get("noticeDescription")
        if isinstance(notice, Mapping):
            notice_api = str(notice.get("api") or "mtop.com.taobao.auction.notice.content.get")
            notice_version = str(notice.get("version") or "1.0")
            notice_tfs = notice.get("tfsName")
            if notice_tfs:
                try:
                    notice_json = self._call_mtop(
                        notice_api,
                        notice_version,
                        {"itemId": item_id, "tfsName": notice_tfs},
                    )
                except Exception as exc:
                    notice_json = {"error": str(exc)}

        return _parse_ali_mtop_detail(
            detail_data,
            source_url=list_item.source_url or ALI_DEFAULT_DETAIL_URL.format(item_id=item_id),
            list_item=list_item,
            detail_json=detail_json,
            description_json=description_json,
            attachments_json=attachments_json,
            summary_json=summary_json,
            notice_json=notice_json,
        )

    def _call_mtop(self, api: str, version: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
        last_payload: Mapping[str, Any] | None = None
        for attempt in range(3):
            timestamp = str(int(time.time() * 1000))
            data_text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            sign = hashlib.md5(
                f"{self._token()}&{timestamp}&{self.app_key}&{data_text}".encode("utf-8")
            ).hexdigest()
            params = {
                "jsv": "2.7.2",
                "appKey": self.app_key,
                "t": timestamp,
                "sign": sign,
                "api": api,
                "v": version,
                "type": "originaljson",
                "dataType": "json",
                "timeout": "20000",
                "data": data_text,
            }
            url = f"https://h5api.m.taobao.com/h5/{api.lower()}/{version}/"
            response = self.session.get(url, params=params, timeout=self.timeout)
            payload = response.json()
            last_payload = payload
            ret_text = " ".join(payload.get("ret") or [])
            if ("TOKEN_EMPTY" in ret_text or "FAIL_SYS_TOKEN_EXOIRED" in ret_text) and attempt < 2:
                continue
            if not self._is_success(payload):
                raise RuntimeError(f"{api} failed: {payload.get('ret')}")
            return payload
        raise RuntimeError(f"{api} failed: {last_payload}")

    def _token(self) -> str:
        cookie = self.session.cookies.get("_m_h5_tk") or ""
        return cookie.split("_", 1)[0]

    @staticmethod
    def _is_success(payload: Mapping[str, Any]) -> bool:
        return any(str(ret).startswith("SUCCESS") for ret in payload.get("ret") or [])


class AliBrowserProfileFetcher:
    """Browser-profile fallback helper. Playwright is imported only when fetch_detail is called."""

    def __init__(self, *, profile_env: str = ALI_DEFAULT_PROFILE_ENV, headless: bool = False) -> None:
        self.profile_env = profile_env
        self.headless = headless

    def detect_block_state(self, html: str, url: str = "") -> tuple[str, str]:
        haystack_lower = f"{url}\n{html}".lower()
        haystack = f"{url}\n{html}"
        if "bxpunish" in haystack_lower:
            return "blocked", "bxpunish challenge detected"
        if "captcha" in haystack_lower or "验证码" in haystack or "滑块" in haystack:
            return "blocked", "captcha challenge detected"
        if (
            "login.taobao.com" in haystack_lower
            or "login-form" in haystack_lower
            or "login_form" in haystack_lower
            or "请登录" in haystack
            or "登录后查看" in haystack
        ):
            return "needs_manual_login", "login required or expired"
        return "ok", ""

    def parse_rendered_detail(self, html: str, url: str = "") -> AliDetailBundle:
        status, reason = self.detect_block_state(html, url)
        source_item_id = _extract_item_id_from_url_or_html(url, html)
        if status != "ok":
            return AliDetailBundle(
                source_item_id=source_item_id,
                source_url=url,
                rendered_html=html,
                status=status,
                block_reason=reason,
                data_source="ali_browser_profile",
            )

        parsed = _HTMLTextExtractor()
        parsed.feed(html or "")
        text = parsed.text()
        title = _extract_html_title(html, parsed.title_text)
        category = _extract_labeled_value(text, ("标的类型", "资产类型", "类目", "分类"))
        start_price_raw = _extract_labeled_value(text, ("起拍价", "起始价", "保证金", "挂牌价"))
        final_price_raw, price_basis, price_excerpt = _extract_effective_price_from_text(text)
        asset_location = _extract_labeled_value(text, ("所在地", "标的所在地", "项目所在地", "资产所在地"))
        project_status = _extract_labeled_value(text, ("项目状态", "竞价状态", "状态"))
        contact_info = _extract_labeled_value(text, ("联系方式", "咨询电话", "联系人", "联系电话"))
        special_notice = _extract_labeled_value(text, ("特别提示", "特别提醒", "特别告知", "重要提示", "注意事项"))
        asset_group = _classify_asset_group(category, title)
        assessment_price = _extract_labeled_value(text, ("评估价", "评估价格", "评估价值", "市场价", "市场价格", "参考价"))
        assessment_price_time = f"评估价：{_ensure_yuan_suffix(assessment_price)}" if assessment_price else ""

        return AliDetailBundle(
            source_item_id=source_item_id,
            source_url=url,
            title=title,
            category=category,
            asset_group=asset_group,
            asset_type=_asset_type_label(asset_group, category),
            asset_location=asset_location,
            project_status=project_status,
            start_price_raw=start_price_raw,
            final_price_raw=final_price_raw,
            price_basis=price_basis,
            source_excerpt=price_excerpt,
            contact_info=contact_info,
            special_notice=special_notice,
            assessment_price_time=assessment_price_time,
            attachments=parsed.attachments,
            image_urls=parsed.image_urls,
            rendered_html=html,
            rendered_text=text,
            page_text=text,
            status="ok",
            data_source="ali_browser_profile",
        )

    def fetch_detail(self, url: str, *, profile_path: Optional[str] = None, timeout_ms: int = 30_000) -> AliDetailBundle:
        user_data_dir = profile_path or os.environ.get(self.profile_env)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._fetch_detail_with_selenium(url, user_data_dir=user_data_dir, timeout_ms=timeout_ms)

        with sync_playwright() as playwright:
            if user_data_dir:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=self.headless,
                )
            else:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context()
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return self.parse_rendered_detail(page.content(), url)
            finally:
                context.close()

    def _fetch_detail_with_selenium(
        self,
        url: str,
        *,
        user_data_dir: Optional[str],
        timeout_ms: int,
    ) -> AliDetailBundle:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import TimeoutException, WebDriverException
        except ImportError as exc:
            raise RuntimeError("Ali browser-profile fetching requires Playwright or Selenium.") from exc

        last_error: Optional[Exception] = None
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
            if user_data_dir and browser_name == "chrome":
                options.add_argument(f"--user-data-dir={user_data_dir}")
            driver = None
            try:
                driver = driver_factory(options=options)
                if timeout_ms and timeout_ms > 0:
                    driver.set_page_load_timeout(max(1, timeout_ms / 1000))
                try:
                    driver.get(url)
                except TimeoutException:
                    driver.execute_script("window.stop()")
                time.sleep(5)
                return self.parse_rendered_detail(driver.page_source or "", url)
            except WebDriverException as exc:
                last_error = exc
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        raise RuntimeError(f"Ali detail browser fallback failed: {last_error}")


class AliAuctionAdapter:
    def __init__(
        self,
        *,
        top_fetcher: Optional[AliTopApiFetcher] = None,
        mtop_fetcher: Optional[AliMtopAuctionFetcher] = None,
        browser_fetcher: Optional[AliBrowserProfileFetcher] = None,
    ) -> None:
        self.top_fetcher = top_fetcher or AliTopApiFetcher()
        self.mtop_fetcher = mtop_fetcher or AliMtopAuctionFetcher()
        self.browser_fetcher = browser_fetcher or AliBrowserProfileFetcher()

    def parse_top_list(self, json_data: Mapping[str, Any]) -> list[AliListItem]:
        return self.top_fetcher.parse_list(json_data)

    def parse_top_detail(self, json_data: Mapping[str, Any]) -> AliDetailBundle:
        return self.top_fetcher.parse_detail(json_data)

    def fetch_mtop_list(
        self,
        *,
        limit: int = 10,
        channels: Optional[Iterable[AliListChannel]] = None,
        pages_per_channel: int = 2,
    ) -> list[AliListItem]:
        return self.mtop_fetcher.fetch_list(
            limit=limit,
            channels=channels,
            pages_per_channel=pages_per_channel,
        )

    def fetch_mtop_detail(self, list_item: AliListItem) -> AliDetailBundle:
        return self.mtop_fetcher.fetch_detail(list_item)

    def parse_rendered_detail(self, html: str, url: str = "") -> AliDetailBundle:
        return self.browser_fetcher.parse_rendered_detail(html, url)

    def merge_detail_bundles(self, primary: AliDetailBundle, fallback: AliDetailBundle) -> AliDetailBundle:
        """Merge browser-rendered evidence into the mtop bundle without overriding stronger API values."""

        if fallback.status != "ok":
            return primary

        primary.title = first_non_blank_any(primary.title, fallback.title)
        primary.category = first_non_blank_any(primary.category, fallback.category)
        primary.asset_location = first_non_blank_any(primary.asset_location, fallback.asset_location)
        primary.project_status = first_non_blank_any(primary.project_status, fallback.project_status)
        primary.signup_start_time = first_non_blank_any(primary.signup_start_time, fallback.signup_start_time)
        primary.signup_end_time = first_non_blank_any(primary.signup_end_time, fallback.signup_end_time)
        primary.disposal_party = first_non_blank_any(primary.disposal_party, fallback.disposal_party)
        primary.disposal_agency = first_non_blank_any(primary.disposal_agency, fallback.disposal_agency)
        primary.start_price_raw = first_non_blank_any(primary.start_price_raw, fallback.start_price_raw)
        primary.final_price_raw = first_non_blank_any(primary.final_price_raw, fallback.final_price_raw)
        primary.contact_info = first_non_blank_any(primary.contact_info, fallback.contact_info)
        primary.special_notice = first_non_blank_any(primary.special_notice, fallback.special_notice)
        primary.assessment_price_time = first_non_blank_any(primary.assessment_price_time, fallback.assessment_price_time)

        primary.attachments = _merge_attachment_lists(primary.attachments, fallback.attachments)
        primary.image_urls = _dedupe_texts([*primary.image_urls, *fallback.image_urls])
        primary.rendered_html = first_non_blank_any(primary.rendered_html, fallback.rendered_html)
        primary.rendered_text = _join_unique_sections(primary.rendered_text, fallback.rendered_text)
        primary.page_text = _join_unique_sections(primary.page_text, fallback.page_text)

        classification_text = " ".join(
            part
            for part in (
                primary.category,
                primary.title,
                str(primary.summary_fields or ""),
            )
            if part
        )
        primary.asset_group = _classify_asset_group(primary.category, classification_text)
        primary.asset_type = _asset_type_label(primary.asset_group, primary.category)
        if primary.data_source and "browser" not in primary.data_source:
            primary.data_source = f"{primary.data_source}+ali_browser_profile"
        return primary

    def build_ai_context(self, bundle: AliDetailBundle) -> AIExtractionContext:
        html_key_values = {
            key: str(value)
            for key, value in self.map_common_candidates(bundle).items()
            if value is not None and value != ""
        }
        raw_sections = []
        if bundle.top_json:
            raw_sections.append("TOP API JSON:\n" + _safe_json_dumps(bundle.top_json, max_chars=6000))
        if bundle.page_text or bundle.rendered_text:
            raw_sections.append("Rendered page text:\n" + (bundle.page_text or bundle.rendered_text))
        if bundle.notice_text:
            raw_sections.append("Auction notice text:\n" + bundle.notice_text)
        if bundle.attachments:
            raw_sections.append("Attachments:\n" + _safe_json_dumps(bundle.attachments, max_chars=2000))
        if bundle.image_urls:
            raw_sections.append("Images:\n" + "\n".join(bundle.image_urls[:50]))

        notice_parts = [bundle.special_notice, bundle.notice_text]
        if bundle.source_excerpt:
            notice_parts.append(f"price excerpt: {bundle.source_excerpt}")

        return AIExtractionContext(
            html_key_values=html_key_values,
            detail_text=_truncate("\n\n".join(part for part in raw_sections if part), 9000),
            notice_text=_truncate("\n".join(part for part in notice_parts if part), 4000),
            image_urls=list(bundle.image_urls),
            asset_group=bundle.asset_group or "other",
            paimai_id=bundle.source_item_id,
        )

    def map_common_candidates(self, bundle: AliDetailBundle) -> dict[str, Any]:
        asset_group = bundle.asset_group or _classify_asset_group(bundle.category, bundle.title)
        attachments_payload = {
            "attachments": bundle.attachments,
            "images": bundle.image_urls,
        }
        attachments_json = ""
        if bundle.attachments or bundle.image_urls:
            attachments_json = json.dumps(attachments_payload, ensure_ascii=False, sort_keys=True)

        return {
            "source_platform": ALI_SOURCE_PLATFORM,
            "source_item_id": bundle.source_item_id,
            "source_url": bundle.source_url,
            "asset_group": asset_group,
            "asset_type": bundle.asset_type or bundle.category,
            "project_name": bundle.title,
            "asset_location": bundle.asset_location,
            "project_status": bundle.project_status,
            "signup_start_time": bundle.signup_start_time,
            "signup_end_time": bundle.signup_end_time,
            "disposal_party": bundle.disposal_party,
            "disposal_agency": bundle.disposal_agency,
            "start_price_raw": bundle.start_price_raw,
            "final_price_raw": bundle.final_price_raw,
            "contact_info": bundle.contact_info,
            "special_notice": bundle.special_notice,
            "assessment_price_time": bundle.assessment_price_time,
            "attachments_json": attachments_json,
            "data_source": bundle.data_source or "ali_top_api",
            "price_basis": bundle.price_basis,
            "source_excerpt": bundle.source_excerpt,
        }

    def map_special_candidates(self, bundle: AliDetailBundle, asset_group: str) -> dict[str, Any]:
        summary = {str(key): str(value) for key, value in (bundle.summary_fields or {}).items()}
        text = "\n".join(
            part
            for part in (
                bundle.title,
                bundle.asset_location,
                bundle.page_text,
                bundle.notice_text,
                _safe_json_dumps(summary, max_chars=6000) if summary else "",
            )
            if part
        )
        images = "; ".join(bundle.image_urls[:80])
        values: dict[str, Any] = {}

        if asset_group == "real_estate":
            values.update(
                {
                    "right_certificate_no": first_non_blank_any(
                        _value_by_alias(summary, ("权证编号", "权证", "不动产权证", "证号", "产权证号")),
                        _extract_certificate_from_text(text),
                    ),
                    "building_area": first_non_blank_any(
                        _value_by_alias(summary, ("建筑面积", "房屋面积", "套内面积", "面积", "出租面积", "租赁面积")),
                        _extract_area_from_text(text),
                    ),
                    "property_use": first_non_blank_any(
                        _value_by_alias(summary, ("房产用途", "规划用途", "用途", "使用用途", "出租用途")),
                        _extract_labeled_value(text, ("房屋用途", "房产用途", "规划用途", "用途")),
                    ),
                    "use_term": first_non_blank_any(
                        _value_by_alias(summary, ("使用年限", "使用期限", "土地使用期限", "出租期限", "租赁期限", "承租期限")),
                        _extract_use_term_from_text(text),
                    ),
                    "property_location": first_non_blank_any(
                        _value_by_alias(summary, ("坐落", "位置", "地址", "所在地", "标的物所在地")),
                        bundle.asset_location,
                    ),
                    "property_structure": first_non_blank_any(
                        _value_by_alias(summary, ("房屋结构", "建筑结构", "结构")),
                        _extract_labeled_value(text, ("房屋结构", "建筑结构", "结构")),
                    ),
                    "property_status": first_non_blank_any(
                        _value_by_alias(summary, ("现状", "房产状态", "使用状态", "出租情况", "租赁情况")),
                        _extract_labeled_value(text, ("现状", "使用状态", "出租情况", "租赁情况")),
                    ),
                    "disclosed_defects": first_non_blank_any(
                        _value_by_alias(summary, ("瑕疵", "风险提示", "特别提示", "重大事项", "备注")),
                        bundle.special_notice,
                    ),
                    "site_images": images,
                    "property_type": first_non_blank_any(
                        _value_by_alias(summary, ("房产类型", "物业类型", "类型", "标的类型")),
                        _infer_property_type(text),
                    ),
                    "asset_highlights": _value_by_alias(summary, ("资产亮点", "亮点", "核心优势", "项目亮点")),
                }
            )
        elif asset_group == "land":
            values.update(
                {
                    "right_certificate_no": first_non_blank_any(
                        _value_by_alias(summary, ("土地证号", "不动产权证", "证号", "权证编号")),
                        _extract_certificate_from_text(text),
                    ),
                    "land_area": first_non_blank_any(
                        _value_by_alias(summary, ("土地面积", "宗地面积", "使用权面积", "面积")),
                        _extract_area_from_text(text),
                    ),
                    "land_use": first_non_blank_any(
                        _value_by_alias(summary, ("土地用途", "规划用途", "用途")),
                        _extract_labeled_value(text, ("土地用途", "规划用途", "用途")),
                    ),
                    "use_term": first_non_blank_any(
                        _value_by_alias(summary, ("使用期限", "土地使用期限", "使用年限")),
                        _extract_use_term_from_text(text),
                    ),
                    "land_location": first_non_blank_any(
                        _value_by_alias(summary, ("坐落", "位置", "地址", "所在地")),
                        bundle.asset_location,
                    ),
                    "right_holder": _value_by_alias(summary, ("权利人", "所有权人", "产权人")),
                    "land_status": first_non_blank_any(
                        _value_by_alias(summary, ("土地状态", "现状", "使用状态")),
                        _extract_labeled_value(text, ("土地状态", "现状", "使用状态")),
                    ),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "重大事项")), bundle.special_notice),
                    "site_images": images,
                    "land_type": _value_by_alias(summary, ("土地类型", "权利类型", "土地性质")),
                    "assessment_time_value": bundle.assessment_price_time,
                }
            )
        elif asset_group == "vehicle":
            values.update(
                {
                    "storage_location": first_non_blank_any(_value_by_alias(summary, ("停放地", "存放地", "所在地", "地址")), bundle.asset_location),
                    "vehicle_brand_model": first_non_blank_any(
                        _value_by_alias(summary, ("车辆品牌", "品牌型号", "车型品牌", "车辆型号", "厂牌型号")),
                        _extract_labeled_value(text, ("车辆品牌", "品牌型号", "车型品牌", "厂牌型号")),
                    ),
                    "vehicle_usage": first_non_blank_any(
                        _join_non_blank(
                            _value_by_alias(summary, ("行驶里程", "里程数")),
                            _value_by_alias(summary, ("出厂日期", "注册日期")),
                            _value_by_alias(summary, ("年检", "保险")),
                        ),
                        _extract_labeled_value(text, ("使用情况", "车辆现状")),
                    ),
                    "plate_number": first_non_blank_any(
                        _value_by_alias(summary, ("车牌号", "号牌号码", "牌照号")),
                        _extract_plate_number(text),
                    ),
                    "vehicle_configuration": _join_non_blank(
                        _value_by_alias(summary, ("排量", "配置")),
                        _value_by_alias(summary, ("颜色",)),
                        _value_by_alias(summary, ("车架号",)),
                    ),
                    "vehicle_status": first_non_blank_any(
                        _value_by_alias(summary, ("车辆状态", "现状", "使用状态")),
                        _extract_labeled_value(text, ("车辆状态", "现状", "使用状态")),
                    ),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "违章", "事故", "风险提示")), bundle.special_notice),
                    "vehicle_images": images,
                    "vehicle_type": first_non_blank_any(_value_by_alias(summary, ("车辆类型", "车辆种类", "类型")), "车辆"),
                }
            )
        elif asset_group == "equipment":
            values.update(
                {
                    "storage_location": first_non_blank_any(_value_by_alias(summary, ("存放地", "存放位置", "所在地", "地址")), bundle.asset_location),
                    "equipment_status": first_non_blank_any(
                        _value_by_alias(summary, ("设备状态", "现状", "使用状态")),
                        _extract_labeled_value(text, ("设备状态", "现状", "使用状态")),
                    ),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "重大事项")), bundle.special_notice),
                    "site_images": images,
                    "equipment_type": first_non_blank_any(_value_by_alias(summary, ("设备类型", "类型", "资产类别")), _infer_equipment_type(text)),
                }
            )
        elif asset_group == "debt":
            values.update(
                {
                    "debtor_name": first_non_blank_any(_value_by_alias(summary, ("债务人", "借款人", "主债务人")), _extract_labeled_value(text, ("债务人", "借款人", "主债务人"))),
                    "creditor": first_non_blank_any(_value_by_alias(summary, ("债权人", "权利人", "转让方")), _extract_labeled_value(text, ("债权人", "权利人", "转让方"))),
                    "guarantee_method": _value_by_alias(summary, ("担保方式", "保证方式", "抵押")),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "特别提示")), bundle.special_notice),
                    "litigation_status": _value_by_alias(summary, ("诉讼状态", "执行状态", "案件状态")),
                    "household_count": _value_by_alias(summary, ("户数", "债权笔数", "笔数")),
                    "benchmark_date": _value_by_alias(summary, ("基准日", "截至日", "截止日")),
                }
            )
        elif asset_group == "ip":
            values.update(
                {
                    "subject_name": first_non_blank_any(_value_by_alias(summary, ("名称", "知识产权名称", "标的名称")), bundle.title),
                    "ip_count": first_non_blank_any(_value_by_alias(summary, ("数量", "项数")), _extract_count_from_text(text)),
                    "specific_category": first_non_blank_any(_value_by_alias(summary, ("类别", "具体类别", "知识产权类型")), _infer_ip_category(text)),
                    "right_holder": _value_by_alias(summary, ("权利人", "所有权人", "著作权人", "专利权人")),
                    "subject_intro": first_non_blank_any(_value_by_alias(summary, ("简介", "基本情况", "标的简介")), bundle.title),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "重大事项")), bundle.special_notice),
                    "right_term": _value_by_alias(summary, ("有效期", "保护期限", "权利期限")),
                }
            )
        elif asset_group == "goods":
            values.update(
                {
                    "goods_category": first_non_blank_any(_value_by_alias(summary, ("物资种类", "类别", "类型")), _infer_goods_category(text)),
                    "goods_name": first_non_blank_any(_value_by_alias(summary, ("名称", "标的名称")), bundle.title),
                    "goods_location": first_non_blank_any(_value_by_alias(summary, ("所在地", "存放位置", "存放地", "地址")), bundle.asset_location),
                    "goods_details": first_non_blank_any(_value_by_alias(summary, ("详情", "规格", "数量", "标的物描述")), _extract_labeled_value(text, ("详情", "规格", "数量"))),
                    "right_holder": _value_by_alias(summary, ("权利人", "所有权人", "产权人")),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "重大事项")), bundle.special_notice),
                    "right_burden": _value_by_alias(summary, ("权利负担", "查封", "抵押")),
                }
            )
        elif asset_group == "usufruct":
            values.update(
                {
                    "right_category": first_non_blank_any(_value_by_alias(summary, ("权益种类", "权利类型", "类型")), _infer_usufruct_category(text)),
                    "subject_name": first_non_blank_any(_value_by_alias(summary, ("名称", "标的名称")), bundle.title),
                    "subject_location": first_non_blank_any(_value_by_alias(summary, ("所在地", "位置", "地址")), bundle.asset_location),
                    "subject_details": first_non_blank_any(_value_by_alias(summary, ("详情", "标的物详情")), bundle.title),
                    "valid_period": first_non_blank_any(_value_by_alias(summary, ("有效期", "期限", "权利期限")), _extract_use_term_from_text(text)),
                    "original_right_holder": _value_by_alias(summary, ("原权利人", "权利人")),
                    "disclosed_defects": first_non_blank_any(_value_by_alias(summary, ("瑕疵", "风险提示", "重大事项")), bundle.special_notice),
                    "right_burden": _value_by_alias(summary, ("权利负担", "查封", "抵押")),
                }
            )
        else:
            values.update(
                {
                    "raw_detail_text": text[:AI_DETAIL_TEXT_LIMIT],
                    "raw_table_pairs_json": _safe_json_dumps(summary, max_chars=12000),
                    "extracted_summary": first_non_blank_any(bundle.title, _extract_labeled_value(text, ("标的物详情", "标的详情"))),
                }
            )

        return {key: value for key, value in values.items() if not _is_blank(value)}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._link_href = ""
        self._link_parts: list[str] = []
        self.attachments: list[dict[str, str]] = []
        self.image_urls: list[str] = []

    @property
    def title_text(self) -> str:
        return _compact_text("".join(self._title_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag == "img":
            src = attr_map.get("src") or attr_map.get("data-src")
            if src:
                self.image_urls.append(src)
        elif tag == "a":
            self._link_href = attr_map.get("href", "")
            self._link_parts = []
        if tag in {"p", "div", "li", "tr", "br", "section", "article", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            label = _compact_text("".join(self._link_parts))
            if self._link_href and _looks_like_attachment(self._link_href, label):
                self.attachments.append({"name": label or self._link_href, "url": self._link_href})
            self._link_href = ""
            self._link_parts = []
        if tag in {"p", "div", "li", "tr", "section", "article", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._link_href:
            self._link_parts.append(data)
        self._parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self._parts))
        lines = [_compact_text(line) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def compact_item_id(value: Any) -> str:
    text = _to_text(value)
    match = re.search(r"\d{5,}", text)
    return match.group(0) if match else text


def _build_ali_channel_context(channel: AliListChannel, page_no: int) -> dict[str, Any]:
    item_context = {
        "spm": channel.spm,
        "userInfo": {},
        "page": str(page_no),
    }
    if channel.fcat_v4_ids:
        item_context["fcatV4Ids"] = list(channel.fcat_v4_ids)
    context: dict[str, Any] = {
        f"_b_{channel.module_id}:items": json.dumps(item_context, ensure_ascii=False, separators=(",", ":")),
        f"_b_{channel.module_id}:keywordSource": "null",
        "userInfo": "{}",
        "device": "pc",
    }
    if channel.scene_code:
        context["sceneCode"] = channel.scene_code
    return context


def _build_ali_datafront_payload(
    *,
    page_id: int,
    module_ids: str,
    context: Mapping[str, Any],
    unique_module_ids: Optional[str] = None,
    variables_recover: Optional[str] = None,
) -> dict[str, Any]:
    variables = {
        "pageId": page_id,
        "moduleIds": module_ids,
        "context": dict(context),
    }
    variables_text = json.dumps(variables, ensure_ascii=False, separators=(",", ":"))
    return {
        "dfApp": "auctionwalle",
        "dfApiName": ALI_LIST_DF_API_NAME,
        "dfVariables": variables_text,
        "dfUniqueId": f"{page_id}.{unique_module_ids or module_ids}",
        "dfVariablesRecover": variables_recover if variables_recover is not None else "{}",
    }


def _extract_ali_datafront_items_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    def walk(value: Any, path: str = "") -> Optional[dict[str, Any]]:
        if isinstance(value, Mapping):
            if path.endswith(".items") and isinstance(value.get("schemeList"), list):
                return {
                    "pageSize": value.get("pageSize"),
                    "totalCount": value.get("totalCount"),
                    "hasNextPage": value.get("hasNextPage"),
                }
            for key, child in value.items():
                next_path = f"{path}.{key}" if path else str(key)
                found = walk(child, next_path)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value[:5]):
                found = walk(child, f"{path}[{index}]")
                if found is not None:
                    return found
        return None

    return walk(payload) or {}


def _with_ali_channel_metadata(
    item: AliListItem,
    channel: AliListChannel,
    page_no: int,
    metadata: Mapping[str, Any],
) -> AliListItem:
    category_lower = (item.category or "").strip().lower()
    if not item.category or category_lower in {"gov", "sf", "zc", "pm", "auction"}:
        item.category = channel.label
    if channel.asset_group and channel.asset_group != "other":
        item.asset_group = channel.asset_group

    raw = dict(item.raw or {})
    raw["_ali_channel"] = {
        "key": channel.key,
        "label": channel.label,
        "asset_group": channel.asset_group,
        "pageId": channel.page_id,
        "moduleId": channel.module_id,
        "page": page_no,
        "pageSize": metadata.get("pageSize"),
        "totalCount": metadata.get("totalCount"),
        "hasNextPage": metadata.get("hasNextPage"),
        "url": channel.url,
    }
    item.raw = raw
    return item


def _extract_ali_datafront_scheme_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    def walk(value: Any, path: str = "") -> Optional[list[Mapping[str, Any]]]:
        if isinstance(value, Mapping):
            for key, child in value.items():
                next_path = f"{path}.{key}" if path else str(key)
                if key == "schemeList" and isinstance(child, list) and path.endswith(".items"):
                    return [item for item in child if isinstance(item, Mapping)]
                found = walk(child, next_path)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value[:5]):
                found = walk(child, f"{path}[{index}]")
                if found is not None:
                    return found
        return None

    return walk(payload) or []


def _parse_ali_datafront_list_item(item: Mapping[str, Any]) -> AliListItem:
    item_id = compact_item_id(item.get("itemId"))
    title = _to_text(item.get("auctionTitle") or item.get("title"))
    price = _join_price_unit(item.get("price"), item.get("priceUnit"))
    start_price = _join_price_unit(item.get("displayInitialPrice"), item.get("displayInitialPriceUnit")) or price
    status = _map_ali_status(_to_text(item.get("status")))
    source_url = _normalize_url(_to_text(item.get("auctionLink"))) or ALI_DEFAULT_DETAIL_URL.format(item_id=item_id)
    category = _to_text(item.get("bizType") or item.get("contentType") or item.get("auctionType"))
    return AliListItem(
        item_id=item_id,
        title=title or item_id,
        source_url=source_url,
        category=category,
        asset_group=_classify_asset_group(category, title),
        asset_location=_to_text(item.get("location") or item.get("cityName") or item.get("areaName")),
        project_status=status,
        start_price_raw=start_price,
        final_price_raw=price,
        price_basis=_ali_price_basis(status, _to_text(item.get("priceLabel"))),
        source_excerpt=_safe_json_dumps(item, max_chars=1200),
        raw=item,
    )


def _parse_ali_mtop_detail(
    detail_data: Mapping[str, Any],
    *,
    source_url: str,
    list_item: AliListItem,
    detail_json: Mapping[str, Any],
    description_json: Mapping[str, Any],
    attachments_json: Mapping[str, Any],
    summary_json: Mapping[str, Any],
    notice_json: Mapping[str, Any],
) -> AliDetailBundle:
    item_id = compact_item_id(detail_data.get("itemId") or list_item.item_id)
    title = _to_text(detail_data.get("title") or detail_data.get("realTitle") or list_item.title)
    category = _to_text(detail_data.get("itemBizType") or detail_data.get("auctionType") or list_item.category)
    summary_fields = _summary_field_map(summary_json)
    description_html = _to_text(_find_first_value(description_json, ("content", "descContent", "html")))
    description_parser = _HTMLTextExtractor()
    description_parser.feed(description_html or "")
    description_parser.close()
    description_text = description_parser.text()
    notice_html = _to_text(_find_first_value(notice_json, ("content", "html", "descContent")))
    if notice_html in {"没有拍卖须知", "没有竞买公告"}:
        notice_html = ""
    notice_parser = _HTMLTextExtractor()
    notice_parser.feed(notice_html or "")
    notice_parser.close()
    notice_text = notice_parser.text()

    attachments = _ali_attachments(attachments_json)
    image_urls = _ali_image_urls(detail_data)
    image_urls.extend(description_parser.image_urls)
    image_urls = _dedupe_texts(_normalize_url(url) for url in image_urls if url)

    final_price = _ali_cent_price_display(
        first_non_blank_any(
            detail_data.get("currentPriceLong"),
            detail_data.get("totalPrice"),
            _find_first_value(detail_data.get("chargeSummary"), ("propertyPlatformPrice",)),
        )
    )
    start_price = _ali_cent_price_display(detail_data.get("startPrice")) or list_item.start_price_raw
    assessment_price = _ali_assessment_price_display(detail_data, summary_fields, summary_json)
    price_basis = "current_price" if final_price else list_item.price_basis
    status = _map_ali_status(_to_text(detail_data.get("status") or detail_data.get("bidStatus"))) or list_item.project_status
    if list_item.project_status:
        status = list_item.project_status
    associated_unit = detail_data.get("associatedUnit") if isinstance(detail_data.get("associatedUnit"), Mapping) else {}
    contact_info = _join_non_blank(
        detail_data.get("connectPeople"),
        detail_data.get("phone"),
        detail_data.get("mobile"),
        detail_data.get("customerServiceDTO", {}).get("tel") if isinstance(detail_data.get("customerServiceDTO"), Mapping) else "",
    )
    special_notice = first_non_blank_any(
        _extract_special_notice_from_text(description_text),
        _extract_special_notice_from_text(notice_text),
    )
    top_json = {
        "detail": detail_json,
        "description": description_json,
        "attachments": attachments_json,
        "summary": summary_json,
        "notice": notice_json,
    }
    asset_group = _classify_asset_group(category, " ".join([title, str(summary_fields)]))
    asset_location = _best_ali_asset_location(
        asset_group=asset_group,
        title=title,
        detail_data=detail_data,
        summary_fields=summary_fields,
        description_text=description_text,
        notice_text=notice_text,
        list_item=list_item,
    )

    return AliDetailBundle(
        source_item_id=item_id,
        source_url=source_url,
        title=title,
        category=category,
        asset_group=asset_group,
        asset_type=_asset_type_label(asset_group, first_non_blank_any(summary_fields.get("类型"), category)),
        asset_location=_to_text(asset_location),
        project_status=status,
        signup_start_time=_to_text(detail_data.get("startTime") or detail_data.get("applyStartTime")),
        signup_end_time=_to_text(detail_data.get("endTime") or detail_data.get("applyEndTime")),
        disposal_party=_to_text(first_non_blank_any(detail_data.get("assetProvider"), detail_data.get("sellerName"))),
        disposal_agency=_to_text(associated_unit.get("orgName") if isinstance(associated_unit, Mapping) else ""),
        assessment_price_time=f"评估价：{assessment_price}" if assessment_price else "",
        start_price_raw=start_price,
        final_price_raw=final_price or list_item.final_price_raw,
        price_basis=price_basis,
        source_excerpt=_safe_json_dumps(
            {
                "startPrice": detail_data.get("startPrice"),
                "currentPriceLong": detail_data.get("currentPriceLong"),
                "totalPrice": detail_data.get("totalPrice"),
                "marketPrice": detail_data.get("marketPrice"),
                "consultPrice": detail_data.get("consultPrice"),
                "assessmentPrice": assessment_price,
                "priceDescription": _find_first_value(detail_data, ("priceDescription",)),
            },
            max_chars=1200,
        ),
        contact_info=contact_info,
        special_notice=special_notice,
        attachments=attachments,
        image_urls=image_urls,
        summary_fields=summary_fields,
        notice_html=notice_html,
        notice_text=notice_text,
        top_json=top_json,
        rendered_html=description_html,
        rendered_text=description_text,
        page_text="\n".join(
            part
            for part in [
                description_text,
                "竞买公告/须知:\n" + notice_text if notice_text else "",
                "摘要字段:\n" + _safe_json_dumps(summary_fields, max_chars=4000) if summary_fields else "",
            ]
            if part
        ),
        list_item=list_item,
        status="ok",
        data_source="ali_mtop",
    )


def _summary_field_map(summary_json: Mapping[str, Any]) -> dict[str, str]:
    field_list = _find_first_value(summary_json, ("fieldList",))
    result: dict[str, str] = {}
    if isinstance(field_list, list):
        for field in field_list:
            if not isinstance(field, Mapping):
                continue
            name = _to_text(field.get("fieldName") or field.get("key") or field.get("name"))
            value = _to_text(field.get("fieldValue") or field.get("value") or field.get("text"))
            if name and value:
                result[name] = value
    return result


def _ali_attachments(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    attaches = _find_first_value(
        payload,
        (
            "attaches",
            "attachments",
            "attachList",
            "fileList",
            "files",
            "materialList",
            "resourceList",
            "downloadList",
        ),
    )
    result: list[dict[str, str]] = []
    if isinstance(attaches, list):
        for attach in attaches:
            if not isinstance(attach, Mapping):
                continue
            result.append(_attachment_from_mapping(attach))
    elif isinstance(attaches, Mapping):
        result.append(_attachment_from_mapping(attaches))

    # Some Ali responses bury the files under arbitrary nested keys. Walk the
    # payload as a fallback so names and URLs are not silently lost.
    for attach in _walk_attachment_mappings(payload):
        result.append(_attachment_from_mapping(attach))
    return _merge_attachment_lists(result)


def _attachment_from_mapping(attach: Mapping[str, Any]) -> dict[str, str]:
    title = _to_text(
        first_non_blank_any(
            attach.get("title"),
            attach.get("name"),
            attach.get("fileName"),
            attach.get("attachmentName"),
            attach.get("materialName"),
        )
    )
    attach_id = _to_text(
        first_non_blank_any(
            attach.get("id"),
            attach.get("fileId"),
            attach.get("attachmentId"),
            attach.get("resourceId"),
        )
    )
    url = _normalize_url(
        _to_text(
            first_non_blank_any(
                attach.get("url"),
                attach.get("downloadUrl"),
                attach.get("downloadURL"),
                attach.get("fileUrl"),
                attach.get("fileURL"),
                attach.get("attachmentUrl"),
                attach.get("href"),
                attach.get("link"),
                attach.get("resourceUrl"),
            )
        )
    )
    return {
        "name": title or attach_id or url,
        "url": url,
        "id": attach_id,
        "fileType": _to_text(first_non_blank_any(attach.get("fileType"), attach.get("type"), attach.get("suffix"))),
    }


def _walk_attachment_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        keys = {str(key).lower() for key in value}
        if (
            keys & {"url", "downloadurl", "fileurl", "attachmenturl", "href", "link", "resourceurl"}
            and keys & {"name", "title", "filename", "attachmentname", "materialname"}
        ) or keys & {"fileid", "attachmentid", "resourceid"}:
            yield value
        for child in value.values():
            yield from _walk_attachment_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_attachment_mappings(child)


def _merge_attachment_lists(*lists: Iterable[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for items in lists:
        for item in items or []:
            if not isinstance(item, Mapping):
                continue
            normalized = _attachment_from_mapping(item)
            key = (normalized.get("url", ""), normalized.get("id", ""), normalized.get("name", ""))
            if not any(key) or key in seen:
                continue
            seen.add(key)
            result.append(normalized)
    return result


def _join_unique_sections(*sections: str) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for section in sections:
        text = _compact_text(section)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(section.strip())
    return "\n\n".join(result)


def _ali_image_urls(detail_data: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("pictUrl", "movieUrl", "pcMovieUrl"):
        value = _to_text(detail_data.get(key))
        if value:
            urls.append(value)
    head_media = detail_data.get("headMedia")
    if isinstance(head_media, Mapping):
        image_list = head_media.get("imageList")
        if isinstance(image_list, list):
            urls.extend(_to_text(url) for url in image_list if url)
        for key in ("pictUrl", "movieUrl", "pcMovieUrl"):
            value = _to_text(head_media.get(key))
            if value:
                urls.append(value)
    image_list = detail_data.get("imageList")
    if isinstance(image_list, list):
        urls.extend(_to_text(url) for url in image_list if url)
    media = detail_data.get("media")
    if isinstance(media, Mapping):
        urls.extend(str(value) for value in _walk_url_values(media))
    return urls


def _walk_url_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_url_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_url_values(child)
    elif isinstance(value, str) and ("alicdn.com" in value or value.startswith("//")):
        yield value


def _extract_special_notice_from_text(text: str) -> str:
    if not text:
        return ""
    patterns = (
        r"(特别提醒[:：\s][\s\S]{0,1200})",
        r"(特别提示[:：\s][\s\S]{0,1200})",
        r"(重要提示[:：\s][\s\S]{0,1200})",
        r"(风险提示[:：\s][\s\S]{0,1200})",
        r"((?:网上交保参与竞价)?注意事项[:：\s][\s\S]{0,1200})",
        r"(特别说明[:：\s][\s\S]{0,1200})",
        r"(特别声明[:：\s][\s\S]{0,1200})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _compact_text(match.group(1))
    return ""


def _join_price_unit(price: Any, unit: Any) -> Optional[str]:
    price_text = _to_text(price)
    if not price_text:
        return None
    unit_text = _to_text(unit)
    return f"{price_text}{unit_text}" if unit_text and unit_text not in price_text else price_text


def _ali_cent_price_display(value: Any) -> Optional[str]:
    if _is_blank(value):
        return None
    if isinstance(value, str):
        text = _compact_text(value)
        if re.search(r"[万亿￥元]", text):
            return text
        value = text.replace(",", "")
    try:
        cents = float(value)
    except (TypeError, ValueError):
        return _to_text(value)
    yuan = cents / 100
    return f"{yuan:,.2f} 元"


def _ali_assessment_price_display(
    detail_data: Mapping[str, Any],
    summary_fields: Mapping[str, str],
    summary_json: Mapping[str, Any],
) -> Optional[str]:
    for label in ("评估价", "市场价", "参考价"):
        value = summary_fields.get(label)
        if value and not _is_zero_price_value(value):
            return _ensure_yuan_suffix(value)
    labeled_value = _find_labeled_value(summary_json, ("评估价", "市场价", "参考价"))
    if labeled_value and not _is_zero_price_value(labeled_value):
        return _ensure_yuan_suffix(labeled_value)
    for key in ("marketPrice", "consultPrice", "assessmentPrice", "evaluatedPrice"):
        display = _ali_cent_price_display(_find_first_value(detail_data, (key,)))
        if display and not _is_zero_price_value(display):
            return display
    return None


def _best_ali_asset_location(
    *,
    asset_group: str,
    title: str,
    detail_data: Mapping[str, Any],
    summary_fields: Mapping[str, str],
    description_text: str,
    notice_text: str,
    list_item: AliListItem,
) -> str:
    candidates: list[Any] = []
    if asset_group in {"real_estate", "land"}:
        candidates.extend(
            [
                _extract_ali_location_phrase(description_text),
                _extract_ali_location_phrase(notice_text),
                _extract_ali_location_phrase(title),
            ]
        )
    candidates.extend(
        [
            _value_by_alias(summary_fields, ("坐落", "位置", "地址", "所在地", "标的物所在地", "标的物位置")),
            detail_data.get("auctionAddress"),
            detail_data.get("location"),
            summary_fields.get("精简位置"),
            list_item.asset_location,
        ]
    )
    cleaned = [_clean_ali_location(value) for value in candidates]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return ""
    cleaned.sort(key=_ali_location_score, reverse=True)
    return cleaned[0]


def _extract_ali_location_phrase(text: str) -> str:
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text)
    labeled_patterns = (
        r"(?:(?:变卖标的|拍卖标的)|(?:竞价资产|竞买资产)|(?:标的物|标的名称))\s*[:：为]?\s*(?:位于)?([^。；;\n]{4,180})",
        r"(?:(?:坐落|位置)|(?:地址|所在地))\s*[:：为]?\s*([^。；;\n]{4,160})",
    )
    for pattern in labeled_patterns:
        match = re.search(pattern, normalized)
        if match:
            value = _clean_ali_location(match.group(1))
            if _looks_like_ali_location(value):
                return value

    for sentence in re.split(r"[。；;\n]", normalized):
        value = _clean_ali_location(sentence)
        if _looks_like_ali_location(value):
            return value
    return ""


def _clean_ali_location(value: Any) -> str:
    text = _compact_text(_to_text(value))
    if not text:
        return ""
    text = re.sub(r"^[一二三四五六七八九十]+[、.．]\s*", "", text)
    text = re.sub(r"^(?:(?:变卖标的|拍卖标的)|(?:竞价资产|竞买资产)|(?:标的物|标的名称))\s*[:：为]?\s*", "", text)
    text = re.split(
        r"(?:\[证号|【(?:证号|证号)[:：]|(?:产权证|不动产权)|(?:建筑面积|房屋面积)|(?:土地面积|面积)[:：]|(?:评估价|起拍价)|(?:保证金|增价幅度)|，|,)",
        text,
        maxsplit=1,
    )[0]
    text = re.sub(r"\s+", "", text).strip("：:，,。；;、 ")
    return text[:160]


def _looks_like_ali_location(value: str) -> bool:
    text = _compact_text(value)
    if len(text) < 6 or len(text) > 120:
        return False
    if re.search(r"((?:评估价|起拍价)|(?:保证金|增价幅度)|(?:联系电话|联系人)|(?:竞买人|公告))", text):
        return False
    address_hits = sum(1 for keyword in ("省", "市", "区", "县", "镇", "街道", "路", "街", "大道", "号", "室", "楼", "栋", "幢", "座", "小区", "花园") if keyword in text)
    asset_hits = sum(1 for keyword in ("房", "商铺", "住宅", "储藏室", "车位", "车库", "土地", "厂房", "公寓", "写字楼") if keyword in text)
    return address_hits >= 2 and (asset_hits >= 1 or any(keyword in text for keyword in ("路", "街", "大道", "号", "室", "楼")))


def _ali_location_score(value: str) -> tuple[int, int]:
    text = _compact_text(value)
    detail_hits = sum(1 for keyword in ("路", "街", "大道", "号", "室", "楼", "栋", "幢", "座", "小区", "花园") if keyword in text)
    region_hits = sum(1 for keyword in ("省", "市", "区", "县", "镇", "街道") if keyword in text)
    return (detail_hits * 3 + region_hits * 2, min(len(text), 120))


def _is_zero_price_value(value: Any) -> bool:
    text = _compact_text(_to_text(value)).replace(",", "").replace("，", "").replace(" ", "")
    if not text:
        return False
    if text in {"无", "暂无", "-", "--", "/", "不详"}:
        return True
    return bool(re.fullmatch(r"[￥¥]?0+(?:\.0+)?(?:(?:元|万元)|亿元)?", text))


def _find_labeled_value(data: Any, labels: Iterable[str]) -> Optional[str]:
    wanted = {label.strip() for label in labels}
    if isinstance(data, Mapping):
        label = _to_text(data.get("fieldName") or data.get("key") or data.get("name") or data.get("label"))
        value = _to_text(data.get("fieldValue") or data.get("value") or data.get("text"))
        if label in wanted and value:
            return value
        for nested in data.values():
            found = _find_labeled_value(nested, labels)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_labeled_value(item, labels)
            if found:
                return found
    return None


def _ensure_yuan_suffix(value: Any) -> str:
    text = _compact_text(_to_text(value))
    if not text:
        return ""
    if re.search(r"((?:元|万元)|亿元)$", text):
        return text
    if re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", text):
        return f"{text} 元"
    return text


def _map_ali_status(value: str) -> str:
    text = (value or "").strip().lower()
    mapping = {
        "before": "未开始",
        "wait": "未开始",
        "1": "未开始",
        "ing": "竞价中",
        "bidding": "竞价中",
        "3": "竞价中",
        "end": "已结束",
        "done": "已结束",
        "finish": "已结束",
        "2": "已结束",
        "abort": "已撤回",
        "recall": "已撤回",
        "4": "已撤回",
        "5": "已撤回",
        "nobid": "未成交",
        "6": "未成交",
    }
    return mapping.get(text, value)


def _ali_price_basis(status: str, label: str = "") -> str:
    if "起拍" in label or status == "未开始":
        return "start_price"
    if "成交" in label:
        return "final_price"
    return "current_price"


def _normalize_url(value: str) -> str:
    text = _compact_text(value)
    if text.startswith("//"):
        return "https:" + text
    return text


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def first_non_blank_any(*values: Any) -> Any:
    for value in values:
        if not _is_blank(value):
            return value
    return ""


def _join_non_blank(*values: Any) -> str:
    return " ".join(_to_text(value) for value in values if not _is_blank(value))


def _parse_list_item(item: Mapping[str, Any]) -> AliListItem:
    item_id = _to_text(_find_first_value(item, ("itemId", "item_id", "id", "auctionId", "projectId")))
    title = _to_text(_find_first_value(item, ("title", "itemTitle", "subject", "projectName", "name")))
    category = _to_text(_find_first_value(item, ("categoryName", "category", "assetType", "itemType")))
    source_url = _source_url_from(item, item_id)
    start_price_raw = _extract_price_value(
        item,
        (
            "startPriceStr",
            "startPriceCN",
            "reservePriceStr",
            "initialPriceStr",
            "beginPriceStr",
            "startPrice",
            "reservePrice",
            "initialPrice",
            "beginPrice",
        ),
    )
    final_price_raw, price_basis, source_excerpt = _extract_effective_price_from_json(item)
    asset_location = _to_text(_find_first_value(item, ("location", "address", "assetLocation", "provinceName", "cityName")))
    project_status = _to_text(_find_first_value(item, ("status", "auctionStatus", "statusName", "projectStatus")))
    return AliListItem(
        item_id=item_id,
        title=title,
        source_url=source_url,
        category=category,
        asset_group=_classify_asset_group(category, title),
        asset_location=asset_location,
        project_status=project_status,
        start_price_raw=start_price_raw,
        final_price_raw=final_price_raw,
        price_basis=price_basis,
        source_excerpt=source_excerpt,
        raw=item,
    )


def _parse_detail_json(json_data: Mapping[str, Any]) -> AliDetailBundle:
    item_id = _to_text(_find_first_value(json_data, ("itemId", "item_id", "id", "auctionId", "projectId")))
    title = _to_text(_find_first_value(json_data, ("title", "itemTitle", "subject", "projectName", "name")))
    category = _to_text(_find_first_value(json_data, ("categoryName", "category", "assetType", "itemType")))
    start_price_raw = _extract_price_value(
        json_data,
        (
            "startPriceStr",
            "startPriceCN",
            "reservePriceStr",
            "initialPriceStr",
            "beginPriceStr",
            "startPrice",
            "reservePrice",
            "initialPrice",
            "beginPrice",
        ),
    )
    final_price_raw, price_basis, source_excerpt = _extract_effective_price_from_json(json_data)
    source_url = _source_url_from(json_data, item_id)
    attachments = _extract_collection(json_data, ("attachments", "attachmentList", "attachFiles", "files", "fileList"))
    image_urls = _extract_image_urls(json_data)
    contact_info = _extract_contact_info(json_data)
    special_notice = _to_text(_find_first_value(json_data, ("specialNotice", "importantNotice", "riskNotice", "notice")))
    asset_location = _to_text(_find_first_value(json_data, ("location", "address", "assetLocation", "provinceName", "cityName")))
    project_status = _to_text(_find_first_value(json_data, ("status", "auctionStatus", "statusName", "projectStatus")))
    asset_group = _classify_asset_group(category, title)

    return AliDetailBundle(
        source_item_id=item_id,
        source_url=source_url,
        title=title,
        category=category,
        asset_group=asset_group,
        asset_type=_asset_type_label(asset_group, category),
        asset_location=asset_location,
        project_status=project_status,
        start_price_raw=start_price_raw,
        final_price_raw=final_price_raw,
        price_basis=price_basis,
        source_excerpt=source_excerpt,
        contact_info=contact_info,
        special_notice=special_notice,
        attachments=attachments,
        image_urls=image_urls,
        top_json=json_data,
        status="ok",
        data_source="ali_top_api",
    )


def _find_item_dicts(data: Any) -> list[Mapping[str, Any]]:
    candidates: list[list[Mapping[str, Any]]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            dicts = [item for item in value if isinstance(item, Mapping)]
            if dicts and any(_looks_like_item(item) for item in dicts):
                candidates.append(dicts)
            for item in value:
                walk(item)
        elif isinstance(value, Mapping):
            for child in value.values():
                walk(child)

    walk(data)
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]
    if isinstance(data, Mapping) and _looks_like_item(data):
        return [data]
    return []


def _looks_like_item(item: Mapping[str, Any]) -> bool:
    keys = {str(key).lower() for key in item}
    has_id = bool(keys & {"itemid", "item_id", "id", "auctionid", "projectid"})
    has_title = bool(keys & {"title", "itemtitle", "subject", "projectname", "name"})
    has_price = any("price" in key.lower() for key in keys)
    return has_id and (has_title or has_price)


def _find_first_value(data: Any, keys: Iterable[str]) -> Any:
    found = _find_first_with_key(data, keys)
    return found[1] if found else None


def _find_first_with_key(data: Any, keys: Iterable[str]) -> Optional[tuple[str, Any]]:
    wanted = {key.lower() for key in keys}
    if isinstance(data, Mapping):
        for key, value in data.items():
            if str(key).lower() in wanted and not _is_blank(value):
                return str(key), value
        for value in data.values():
            nested = _find_first_with_key(value, keys)
            if nested:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _find_first_with_key(item, keys)
            if nested:
                return nested
    return None


def _extract_price_value(data: Any, keys: Iterable[str]) -> Optional[str]:
    value = _find_first_value(data, keys)
    return _price_to_text(value)


def _extract_effective_price_from_json(data: Any) -> tuple[Optional[str], str, str]:
    current_keys = (
        "currentPriceStr",
        "currentPriceCN",
        "latestPriceStr",
        "nowPriceStr",
        "currentPrice",
        "latestPrice",
        "nowPrice",
    )
    final_keys = (
        "dealPriceStr",
        "finalPriceStr",
        "successPriceStr",
        "成交价",
        "dealPrice",
        "finalPrice",
        "successPrice",
    )
    for keys, basis in ((current_keys, "current_price"), (final_keys, "final_price")):
        found = _find_first_with_key(data, keys)
        if found:
            key, value = found
            return _price_to_text(value), basis, _json_excerpt(key, value)
    return None, "", ""


def _extract_effective_price_from_text(text: str) -> tuple[Optional[str], str, str]:
    for labels, basis in (
        (("当前价", "最新价", "现价", "当前出价"), "current_price"),
        (("成交价", "最终价", "成交价格"), "final_price"),
    ):
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n\r]+)", text)
            if match:
                value = _compact_text(match.group(1))
                return value, basis, f"{label}：{value}"
    return None, "", ""


def _extract_labeled_value(text: str, labels: Iterable[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n\r]+)", text)
        if match:
            return _compact_text(match.group(1))
    return ""


def _value_by_alias(fields: Mapping[str, str], aliases: Iterable[str]) -> str:
    for alias in aliases:
        for key, value in fields.items():
            label = _compact_text(key)
            if alias in label:
                clean = _compact_text(value)
                if clean:
                    return clean
    return ""


def _extract_area_from_text(text: str) -> str:
    patterns = (
        r"(?:(?:建筑面积|房屋面积)|(?:套内面积|出租面积)|(?:租赁面积|土地面积)|(?:宗地面积|面积))\s*[:：为]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:平方米|㎡|(?:平米|m²)|平))",
        r"([0-9]+(?:\.[0-9]+)?\s*(?:平方米|㎡|(?:平米|m²)|平))",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return _compact_text(match.group(1))
    return ""


def _extract_certificate_from_text(text: str) -> str:
    patterns = (
        r"((?:(?:粤|京)|(?:沪|津)|(?:渝|冀)|(?:豫|云)|(?:辽|黑)|(?:湘|皖)|(?:鲁|新)|(?:苏|浙)|(?:赣|鄂)|(?:桂|甘)|(?:晋|蒙)|(?:陕|吉)|(?:闽|贵)|(?:青|藏)|(?:川|宁)|琼)[^，。；;\n]{0,30}(?:(?:不动产权|房权证)|产权证)[^，。；;\n]{0,40})",
        r"(?:(?:权证编号|权证号)|(?:不动产权证号|房产证号)|(?:土地证号|证号))\s*[:：]?\s*([^，。；;\n]{4,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return _compact_text(match.group(1))
    return ""


def _extract_use_term_from_text(text: str) -> str:
    patterns = (
        r"(?:(?:使用期限|土地使用期限)|(?:租赁期限|出租期限)|承租期限)\s*[:：为]?\s*([^。；;\n]{4,120})",
        r"([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日\s*(?:(?:起|至)|-|—|到)[^。；;\n]{4,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return _compact_text(match.group(1))
    return ""


def _extract_plate_number(text: str) -> str:
    match = re.search(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{4,6}", text or "")
    return match.group(0) if match else ""


def _extract_count_from_text(text: str) -> str:
    patterns = (
        r"([0-9]+)\s*项(?:(?:专利|商标)|(?:著作权|知识产权))?",
        r"(?:(?:专利|商标)|(?:著作权|知识产权))[^0-9]{0,20}([0-9]+)\s*项",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""


def _infer_property_type(text: str) -> str:
    for keyword in ("车位", "车库", "储藏室", "商铺", "厂房", "办公", "公寓", "住宅", "地下室"):
        if keyword in (text or ""):
            return keyword
    return "房地产"


def _infer_equipment_type(text: str) -> str:
    for keyword in ("机械", "设备", "生产线", "仪器", "车辆", "电脑", "软件"):
        if keyword in (text or ""):
            return keyword
    return "设备"


def _infer_ip_category(text: str) -> str:
    parts = [keyword for keyword in ("专利", "商标", "著作权", "软件著作权", "作品著作权", "域名") if keyword in (text or "")]
    return "、".join(dict.fromkeys(parts))


def _infer_goods_category(text: str) -> str:
    for keyword in ("酒水", "存货", "物资", "原材料", "设备", "珠宝", "饰品", "农产品"):
        if keyword in (text or ""):
            return keyword
    return "物资产品"


def _infer_usufruct_category(text: str) -> str:
    for keyword in ("租赁权", "经营权", "使用权", "收益权", "承租权", "采矿权"):
        if keyword in (text or ""):
            return keyword
    return "用益物权"


def _extract_html_title(html: str, title_text: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", flags=re.IGNORECASE | re.DOTALL)
    title = _strip_tags(match.group(1)) if match else title_text
    title = re.split(r"\s[-_|]\s*阿里拍卖|\s*[-_|]\s*淘宝", title, maxsplit=1)[0]
    return _compact_text(title)


def _extract_item_id_from_url_or_html(url: str, html: str) -> str:
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    for key in ("itemId", "id", "item_id"):
        if query.get(key):
            return query[key][0]
    match = re.search(r"['\"]?(?:(?:itemId|item_id)|id)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_-]+)", html or "")
    return match.group(1) if match else ""


def _source_url_from(data: Mapping[str, Any], item_id: str) -> str:
    url = _to_text(_find_first_value(data, ("detailUrl", "sourceUrl", "itemUrl", "auctionLink", "url", "link")))
    if url:
        return _normalize_url(url)
    if item_id:
        return ALI_DEFAULT_DETAIL_URL.format(item_id=item_id)
    return ""


def _extract_collection(data: Any, keys: Iterable[str]) -> list[Any]:
    value = _find_first_value(data, keys)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, Mapping):
        return [dict(value)]
    return []


def _extract_image_urls(data: Any) -> list[str]:
    image_values = _extract_collection(data, ("images", "imageUrls", "image_urls", "picUrls", "pics", "pictures", "mainPics"))
    urls: list[str] = []
    for value in image_values:
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, Mapping):
            url = _to_text(_find_first_value(value, ("url", "src", "imageUrl", "picUrl")))
            if url:
                urls.append(url)
    return urls


def _extract_contact_info(data: Any) -> str:
    value = _find_first_value(data, ("contactInfo", "contact", "contacts", "consultPhone", "phone", "tel"))
    if isinstance(value, Mapping):
        parts = []
        for key in ("name", "contactName", "person", "phone", "tel", "mobile"):
            text = _to_text(value.get(key))
            if text:
                parts.append(text)
        return " ".join(parts)
    if isinstance(value, list):
        return "；".join(_extract_contact_info(item) if isinstance(item, Mapping) else _to_text(item) for item in value)
    return _to_text(value)


def _classify_asset_group(category: str, title: str) -> str:
    category_text = _compact_text(category).lower()
    title_text = _compact_text(title).lower()
    strong_title_mapping = (
        (
            "debt",
            (
                "债权资产包",
                "债权资产",
                "债权转让",
                "抵押债权",
                "个人抵押债权",
                "不良资产",
                "应收账款",
                "应收款",
                "债务",
            ),
        ),
        ("equity", ("股权转让", "公司股权", "股东权益", "合伙份额", "出资权益", "%股权")),
        ("ip", ("知识产权", "软件著作权", "作品著作权", "著作权", "专利权", "商标权", "专利", "商标", "域名")),
        ("usufruct", ("租赁权", "经营权", "收益权", "承租权", "采矿权", "林权", "海域使用权")),
        ("equipment", ("挖掘机", "工程机械", "机械设备", "机器设备", "生产线", "压缩机", "切割机", "办公设备")),
        ("goods", ("边角料", "废料", "废旧", "原材料", "存货", "物资产品", "黄铜", "塑料", "橡胶", "茅台", "翡翠")),
        (
            "vehicle",
            (
                "车牌号",
                "牌照",
                "发动机",
                "车架号",
                "排量",
                "摩托车",
                "三轮车",
                "电动车",
                "游艇",
                "客船",
                "船舶",
                "奥迪",
                "宝马",
                "奔驰",
                "本田",
                "保时捷",
                "丰田",
                "大众",
                "宝骏",
            ),
        ),
        ("land", ("土地使用权", "建设用地使用权", "国有建设用地", "工业用地", "宗地", "地块")),
        (
            "real_estate",
            (
                "不动产",
                "房地产",
                "房产",
                "商铺",
                "住宅",
                "写字楼",
                "车位",
                "地下车库",
                "储藏室",
                "储藏间",
                "号楼",
                "号房",
                "套房",
            ),
        ),
    )
    category_mapping = (
        (
            "debt",
            (
                "债权资产包",
                "债权资产",
                "债权",
                "债权转让",
                "抵押债权",
                "个人抵押债权",
                "不良资产",
                "应收账款",
                "应收款",
                "应收",
                "债权",
                "债务",
            ),
        ),
        ("equity", ("股权", "股权转让", "股东权益", "合伙份额", "出资权益")),
        ("ip", ("知识产权", "商标", "专利", "著作权", "软件著作权", "域名")),
        ("land", ("土地", "地块", "建设用地", "工业用地", "土地使用权", "建设用地使用权")),
        ("usufruct", ("租赁权", "经营权", "收益权", "承租权", "采矿权", "林权", "海域使用权")),
        (
            "vehicle",
            (
                "车辆",
                "机动车",
                "汽车",
                "货车",
                "客车",
                "轿车",
                "小型车",
                "摩托车",
                "摩托",
                "二手车",
                "新车",
                "船舶",
            ),
        ),
        (
            "equipment",
            (
                "设备",
                "机器",
                "机械",
                "生产线",
                "仪器",
                "工程机械",
                "机械设备",
                "机器设备",
                "挖掘机",
                "铲斗",
                "压缩机",
                "切割机",
                "办公设备",
            ),
        ),
        (
            "goods",
            (
                "物资",
                "存货",
                "商品",
                "货物",
                "原材料",
                "废料",
                "边角料",
                "废旧",
                "塑料",
                "黄铜",
                "铜板",
                "橡胶",
                "半成品",
                "产成品",
                "周转箱",
                "蓄电池",
                "酒",
                "茅台",
                "翡翠",
                "手表",
            ),
        ),
        (
            "real_estate",
            (
                "房产",
                "房地产",
                "住宅",
                "商铺",
                "厂房",
                "房屋",
                "不动产",
                "公寓",
                "写字楼",
                "底商",
                "商用房",
                "商业用房",
                "商业房",
                "门面",
                "门市",
                "沿街",
                "临街",
                "商办",
                "办公用房",
                "车位",
                "停车位",
                "车库",
                "地下车库",
                "储藏室",
                "储藏间",
                "储物间",
                "地下室",
                "地下储藏室",
                "号房",
                "套房",
                "房源",
                "特价房",
                "号楼",
                "楼室",
                "中楼层",
                "精装修",
                "小区",
                "建材城",
                "商贸城",
                "五金城",
                "商业城",
                "综合市场",
                "面积",
                "平米",
                "㎡",
            ),
        ),
    )
    title_mapping = (
        (
            "debt",
            (
                "债权资产包",
                "债权资产",
                "债权转让",
                "抵押债权",
                "个人抵押债权",
                "不良资产",
                "应收账款",
                "应收款",
                "应收",
                "债权",
                "债务",
            ),
        ),
        ("equity", ("股权", "股权转让", "公司股权", "股东权益", "合伙份额", "出资权益")),
        ("ip", ("知识产权", "商标", "专利", "著作权", "软件著作权", "域名")),
        ("land", ("土地使用权", "建设用地使用权", "国有建设用地", "工业用地", "土地", "地块")),
        ("usufruct", ("租赁权", "经营权", "收益权", "承租权", "采矿权", "林权", "海域使用权")),
        (
            "real_estate",
            (
                "房产",
                "房地产",
                "住宅",
                "商铺",
                "厂房",
                "房屋",
                "不动产",
                "公寓",
                "写字楼",
                "底商",
                "商用房",
                "商业用房",
                "商业房",
                "门面",
                "门市",
                "沿街",
                "临街",
                "商办",
                "办公用房",
                "车位",
                "停车位",
                "车库",
                "地下车库",
                "储藏室",
                "储藏间",
                "储物间",
                "地下室",
                "地下储藏室",
                "号房",
                "套房",
                "房源",
                "特价房",
                "号楼",
                "楼室",
                "中楼层",
                "精装修",
                "小区",
                "建材城",
                "商贸城",
                "五金城",
                "商业城",
                "综合市场",
                "面积",
                "平米",
                "㎡",
            ),
        ),
        (
            "equipment",
            (
                "设备",
                "机器",
                "机械",
                "生产线",
                "仪器",
                "工程机械",
                "机械设备",
                "机器设备",
                "挖掘机",
                "铲斗",
                "压缩机",
                "切割机",
                "办公设备",
            ),
        ),
        (
            "vehicle",
            (
                "车辆",
                "机动车",
                "汽车",
                "货车",
                "客车",
                "轿车",
                "小型车",
                "摩托车",
                "摩托",
                "二手车",
                "新车",
                "上牌",
                "过户",
                "车牌号",
                "牌照",
                "发动机",
                "车架号",
                "排量",
                "三轮车",
                "电动车",
                "摩托艇",
                "游艇",
                "客船",
                "船舶",
                "奥迪",
                "宝马",
                "奔驰",
                "本田",
                "保时捷",
                "丰田",
                "大众",
                "宝骏",
            ),
        ),
        (
            "goods",
            (
                "物资",
                "存货",
                "商品",
                "货物",
                "原材料",
                "废料",
                "边角料",
                "废旧",
                "塑料",
                "黄铜",
                "铜板",
                "橡胶",
                "半成品",
                "产成品",
                "周转箱",
                "蓄电池",
                "酒",
                "茅台",
                "翡翠",
                "手表",
            ),
        ),
        category_mapping[-1],
    )
    for group, keywords in strong_title_mapping:
        if group == "debt" and any(token in title_text for token in ("债权投资", "债权融资")):
            strong_debt_tokens = ("债权资产", "债权转让", "抵押债权", "不良资产", "应收", "债务")
            if not any(token in title_text for token in strong_debt_tokens):
                continue
        if any(keyword.lower() in title_text for keyword in keywords):
            return group
    for group, keywords in title_mapping:
        if group == "debt" and any(token in title_text for token in ("债权投资", "债权融资")):
            strong_debt_tokens = ("债权资产", "债权转让", "抵押债权", "不良资产", "应收", "债务")
            if not any(token in title_text for token in strong_debt_tokens):
                continue
        if any(keyword.lower() in title_text for keyword in keywords):
            return group
    for group, keywords in category_mapping:
        if any(keyword.lower() in category_text for keyword in keywords):
            return group
    combined = f"{category_text} {title_text}"
    for group, keywords in title_mapping:
        if any(keyword.lower() in combined for keyword in keywords):
            return group
    return "other"


def _asset_type_label(asset_group: str, fallback: str = "") -> str:
    return ALI_ASSET_GROUP_LABELS.get(asset_group) or _compact_text(fallback) or "其他"


def _looks_like_attachment(url: str, label: str) -> bool:
    combined = f"{url} {label}".lower()
    return any(token in combined for token in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", "附件", "公告", "须知"))


def _strip_tags(fragment: str) -> str:
    return _compact_text(re.sub(r"<[^>]+>", " ", unescape(fragment or "")))


def _safe_json_dumps(value: Any, *, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _truncate(text, max_chars)


def _json_excerpt(key: str, value: Any) -> str:
    return json.dumps({key: value}, ensure_ascii=False, default=str)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def _price_to_text(value: Any) -> Optional[str]:
    if _is_blank(value):
        return None
    if isinstance(value, Mapping):
        nested = _find_first_value(value, ("display", "text", "value", "amount", "price"))
        return _price_to_text(nested)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _compact_text(str(value))


def _to_text(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return _compact_text(str(value))


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")
