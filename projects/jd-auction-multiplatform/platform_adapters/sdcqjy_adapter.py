
import json
import re
from typing import Any, Dict, List
from urllib.parse import urljoin

from platform_adapters.cquae_adapter import (
    CquaeAdapter,
    CquaeListItem,
    _collect_payload_dicts,
    _find_by_alias,
    _json_dumps,
    _payload_source_id,
    _payload_title,
    compact_text,
    first_non_blank,
    is_blank,
    price_display_from_payload,
)


SDCQJY_BASE_URL = "http://www.sdcqjy.com"
SDCQJY_LIST_ENDPOINT = f"{SDCQJY_BASE_URL}/projlist/getdata"
SDCQJY_DATA_SOURCE = "山东产权交易中心公开门户"
SDCQJY_PLATFORM = "sdcqjy"


def sdcqjy_detail_url(payload: Dict[str, Any], base_url: str = SDCQJY_BASE_URL) -> str:
    item_id = compact_text(str(payload.get("id") or payload.get("code") or payload.get("proNo") or ""))
    item_type = compact_text(str(payload.get("type") or payload.get("assetType") or ""))
    if not item_id:
        return ""
    if item_type == "tc":
        return urljoin(base_url, f"/proj/tc/{item_id}")
    if item_type == "pw":
        return urljoin(base_url, f"/proj/pw/{item_id}")
    if item_type == "bid":
        bid_mode = compact_text(str(payload.get("bidMode") or ""))
        prefix = "biddingForEnergy" if bid_mode == "3" else "bidding/bidprice"
        return urljoin(base_url, f"/{prefix}/{item_id}")
    if item_type == "zzkg":
        return urljoin(base_url, f"/proj/zzkg/{item_id}")
    return urljoin(base_url, f"/proj/tc/{item_id}")


class SdcqjyAdapter(CquaeAdapter):
    source_platform = SDCQJY_PLATFORM
    source_site_name = SDCQJY_DATA_SOURCE

    def _parse_list_table_html(self, html: str, base_url: str = SDCQJY_BASE_URL) -> List[CquaeListItem]:
        """解析山东产权表格 HTML（<tr data-proId> + <a onclick> 格式）"""
        items: List[CquaeListItem] = []
        seen: set[str] = set()

        # 匹配 <tr data-proId="UUID">...<td>...</td>...</tr>
        row_pattern = re.compile(
            r'<tr[^>]+data-proId\s*=\s*"([^"]+)"[^>]*>(.*?)</tr>',
            re.I | re.S,
        )
        for m in row_pattern.finditer(html):
            pro_id = m.group(1)
            row_html = m.group(2)

            # 提取所有单元格文本
            cells = []
            for td in re.finditer(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.I | re.S):
                cell_text = compact_text(re.sub(r'<[^>]+>', '', td.group(1)))
                cells.append(cell_text)

            if len(cells) < 2:
                continue

            code = cells[0] if len(cells) > 0 else ""
            title = cells[1] if len(cells) > 1 else ""
            price_raw = cells[2] if len(cells) > 2 else ""
            date_text = cells[3] if len(cells) > 3 else ""
            status = cells[4] if len(cells) > 4 else ""

            # 用 pro_id 做稳定 ID
            stable_id = pro_id
            if stable_id in seen:
                continue
            seen.add(stable_id)

            detail_url = f"{base_url}/proj/tc/{pro_id}"
            items.append(
                CquaeListItem(
                    source_item_id=code or stable_id,
                    source_url=detail_url,
                    title=title,
                    project_type=None,
                    project_status=status or None,
                    price_raw=price_raw or None,
                    raw_text=" ".join(cells),
                )
            )
        return items

    def parse_list_html(self, html: str, base_url: str = SDCQJY_BASE_URL) -> List[CquaeListItem]:
        # 方案一：JSON 格式
        try:
            payload = json.loads(html)
        except (TypeError, ValueError):
            payload = None

        if payload is not None:
            items: List[CquaeListItem] = []
            seen: set[str] = set()
            for row in _collect_payload_dicts(payload):
                source_item_id = _payload_source_id(row)
                title = _payload_title(row)
                if not source_item_id and not title:
                    continue
                stable_id = source_item_id or title
                if stable_id in seen:
                    continue
                seen.add(stable_id)
                raw_url = first_non_blank(row.get("url"), row.get("link"), row.get("projectUrl"), row.get("detailUrl"))
                detail_url = urljoin(base_url, raw_url) if raw_url else sdcqjy_detail_url(row, base_url=base_url)
                items.append(
                    CquaeListItem(
                        source_item_id=source_item_id or detail_url or title,
                        source_url=detail_url,
                        title=title,
                        project_type=compact_text(
                            first_non_blank(row.get("assetType"), row.get("typeName"), row.get("projectType"))
                        ),
                        project_status=compact_text(first_non_blank(row.get("statusName"), row.get("status"), row.get("state"))),
                        price_raw=price_display_from_payload(row),
                        deposit_raw=compact_text(first_non_blank(row.get("deposit"), row.get("bond"), row.get("guarantee"))),
                        date_text=compact_text(first_non_blank(row.get("date"), row.get("pubDate"), row.get("startDate"))),
                        contact_info=compact_text(first_non_blank(row.get("contact"), row.get("contactName"), row.get("phone"))),
                        raw_fields={str(k): compact_text(v) for k, v in row.items() if not isinstance(v, (dict, list))},
                        raw_text=_json_dumps(row),
                    )
                )
            if items:
                return items

        # 方案二：HTML 表格格式（<tr data-proId>）
        table_items = self._parse_list_table_html(html, base_url)
        if table_items:
            return table_items

        # 方案三：CQUAE 兼容表格格式
        return super().parse_list_html(html, base_url=base_url)

    def map_common_candidates(self, bundle):
        common: Dict[str, Any] = super().map_common_candidates(bundle)
        common["source_platform"] = self.source_platform
        common["source_site_name"] = self.source_site_name
        common["data_source"] = self.source_site_name

        # SDCQJY 页面使用"挂牌开始日期/挂牌截止日期"而非"报名开始/截止时间"
        fields = bundle.key_values
        if is_blank(common.get("signup_start_time")):
            common["signup_start_time"] = _find_by_alias(fields, ("挂牌开始日期", "挂牌起始日期", "信息披露开始日期"))
        if is_blank(common.get("signup_end_time")):
            common["signup_end_time"] = _find_by_alias(fields, ("挂牌截止日期", "挂牌终止日期", "信息披露截止日期"))

        # SDCQJY 页面将转让底价同时作为起拍价
        if is_blank(common.get("start_price_raw")):
            common["start_price_raw"] = _find_by_alias(fields, ("转让底价", "挂牌价", "挂牌价格"))

        # SDCQJY 页面注册地（住所）作为标的所在地后备
        if is_blank(common.get("asset_location")):
            common["asset_location"] = _find_by_alias(fields, ("注册地（住所）", "注册地", "坐落", "所在地"))

        return common
