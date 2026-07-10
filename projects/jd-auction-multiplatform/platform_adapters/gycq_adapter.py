"""贵州产权交易所 (GZCQ) 适配器

数据来源：zz.prechina.net 前端 SPA (贵州阳光产权域名下的资产交易系统)
- 列表 API:  POST https://zz.prechina.net/api/dscq-project/search/page  (Client-Id: gycq)
- 详情 API:  GET  https://zz.prechina.net/api/dscq-project/{endpoint}?assetsId={id} (Client-Id: gycq)
- 与 gxcq_adapter 同属 dscq-project 多租户后端, 故直接复用 GxcqAdapter 的详情 API 与解析逻辑,
  仅重写列表抓取 (search/page) 与请求头 (client-id=gycq, host=zz.prechina.net)。

注意: 这是与 prechina 旧站(GP- 编号 HTML)不同的独立系统; 数据模型为 UUID 资产, 详情走 dscq RESTful API。
"""


import requests
from typing import Any, Dict, List, Optional

from platform_adapters.gxcq_adapter import (
    GxcqAdapter,
    GxcqListItem,
    GxcqDetailBundle,
    DETAIL_API_ENDPOINTS,
    DEFAULT_HEADERS,
    compact_text,
    first_non_blank,
)


# ===== 常量 =====
GYCQ_PLATFORM = "gycq"
GYCQ_DATA_SOURCE = "贵州阳光产权交易所有限公司"
GYCQ_BASE_URL = "https://zz.prechina.net"
GYCQ_CLIENT_ID = "gycq"

# 列表 API 分页上限(服务端实测每页最多返回 30 条)
GYCQ_LIST_PAGE_SIZE = 30


class GycqAdapter(GxcqAdapter):
    """贵州产权交易所适配器 — 复用 GxcqAdapter 的 dscq-project 详情 API 与解析。

    差异点:
      - 列表来自 POST /api/dscq-project/search/page (而非 gxcq 的 PHPCMF httpapi)
      - 详情 API host = zz.prechina.net, 请求头 client-id = gycq
    """

    source_platform = GYCQ_PLATFORM

    def __init__(
        self,
        *,
        base_url: str = GYCQ_BASE_URL,
        detail_base_url: str = GYCQ_BASE_URL,
        client_id: str = GYCQ_CLIENT_ID,
        session: Optional[requests.Session] = None,
        timeout: int = 20,
    ) -> None:
        # 复用父类构造(设置 base_url / detail_base_url / timeout / session)
        super().__init__(
            base_url=base_url,
            detail_base_url=detail_base_url,
            session=session,
            timeout=timeout,
        )
        self.client_id = client_id
        self.list_headers = {
            **DEFAULT_HEADERS,
            "Client-Id": self.client_id,
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": f"{self.base_url}/project?size=8&current=1",
            "Origin": self.base_url,
        }
        self.detail_headers = {
            **DEFAULT_HEADERS,
            "client-id": self.client_id,
            "Referer": f"{self.detail_base_url}/",
        }

    # ── 列表 API (POST search/page) ──
    def fetch_list_api(
        self,
        *,
        page: int = 1,
        size: int = GYCQ_LIST_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """获取项目列表 (zz.prechina.net dscq-project search/page)

        Returns:
            {code:200, success:true, data:{records:[...], total, size, current, pages}}
        """
        url = f"{self.base_url}/api/dscq-project/search/page"
        resp = self.session.post(
            url,
            params={"size": size, "current": page},
            json={},
            headers=self.list_headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") and data.get("code") != 200:
            raise RuntimeError(
                f"GYCQ list API error: code={data.get('code')}, msg={data.get('msg', '')}"
            )
        return data

    def parse_list_response(self, api_data: Dict[str, Any]) -> List[GxcqListItem]:
        """解析 search/page 返回的 records 数组。"""
        payload = api_data.get("data") or {}
        records = payload.get("records") or []
        items: List[GxcqListItem] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rid = compact_text(rec.get("id"))
            if not rid:
                continue
            title = compact_text(rec.get("assetsName")) or compact_text(rec.get("projectName"))
            if not title:
                continue
            price = rec.get("startPrice")
            price_unit = compact_text(rec.get("priceUnit"))
            if price is not None:
                price_raw = f"{price}{(' ' + price_unit) if price_unit else ''}"
                try:
                    price_num = float(price)
                except (TypeError, ValueError):
                    price_num = None
            else:
                price_raw = None
                price_num = None

            state = rec.get("state") or []
            status = "/".join(compact_text(s) for s in state) if isinstance(state, list) else compact_text(state)

            end_time = compact_text(rec.get("announcementEndTime")) or compact_text(rec.get("endTime"))
            start_time = compact_text(rec.get("announcementStartTime")) or compact_text(rec.get("startTime"))
            publish = compact_text(rec.get("publishDate"))

            atp = compact_text(rec.get("assetsTypeParent"))

            item = GxcqListItem(
                source_item_id=rid,
                source_url=f"{self.base_url}/projectDetail/{rid}.html",
                title=title,
                price_raw=price_raw,
                price_num=price_num,
                project_status=status,
                end_time=end_time,
                signup_deadline=end_time,
                assets_type_parent=atp,
                region=compact_text(rec.get("projectAttribution")) or compact_text(rec.get("province")),
                raw_json=rec,
            )
            # 额外保存时间字段供增量指纹/落库使用
            item.raw_json.setdefault("_publishDate", publish)
            item.raw_json.setdefault("_announcementStart", start_time)
            item.raw_json.setdefault("_announcementEnd", end_time)
            items.append(item)
        return items

    # ── 详情 API (覆盖父类, 使用 gycq 的 host 与 client-id) ──
    def fetch_detail_api(self, list_item: GxcqListItem) -> Dict[str, Any]:
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
                resp = self.session.get(url, headers=self.detail_headers, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") in (200, "200", 0, "0", True):
                    endpoint_key = endpoint.replace("/", "_").replace("-", "_")
                    merged[endpoint_key] = data.get("data")
            except Exception as e:
                errors.append(f"{endpoint}: {e}")

        if errors:
            success_count = len(DETAIL_API_ENDPOINTS) - len(errors)
            print(f"[GYCQ] Detail API: {success_count}/{len(DETAIL_API_ENDPOINTS)} succeeded")

        merged["_source"] = "detail_api"
        merged["_assets_id"] = assets_id
        merged["_errors"] = errors if errors else []
        return merged
