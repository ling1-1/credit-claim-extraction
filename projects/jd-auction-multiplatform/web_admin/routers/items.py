"""标的浏览 API"""

from typing import Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
import httpx

from ..config import WebConfig
from ..database import query_all, query_one

router = APIRouter(prefix="/api/items", tags=["标的浏览"])

_config: Optional[WebConfig] = None


def init(cfg: WebConfig) -> None:
    global _config
    _config = cfg


@router.get("")
def list_items(
    page: int = 1,
    size: int = 20,
    platform: str = "",
    asset_group: str = "",
    status: str = "",
    price_min: float = 0,
    price_max: float = 0,
    keyword: str = "",
):
    """标的列表"""
    where = []
    params: list[Any] = []
    if platform:
        where.append("i.source_platform = %s")
        params.append(platform)
    if asset_group:
        where.append("i.asset_group = %s")
        params.append(asset_group)
    if status:
        where.append("i.project_status = %s")
        params.append(status)
    if price_min > 0:
        where.append("COALESCE(i.start_price_amount, 0) >= %s")
        params.append(price_min)
    if price_max > 0:
        where.append("COALESCE(i.start_price_amount, 0) <= %s")
        params.append(price_max)
    if keyword:
        where.append("(i.project_name LIKE %s OR i.asset_location LIKE %s)")
        like = f"%{keyword}%"
        params.extend([like, like])

    where_clause = "WHERE " + " AND ".join(where) if where else ""

    count = query_one(
        _config,
        f"SELECT COUNT(*) AS cnt FROM auction_items i {where_clause}",
        params,
    ) or {"cnt": 0}

    offset = (page - 1) * size
    rows = query_all(
        _config,
        f"""
        SELECT i.item_id, i.source_platform, i.source_item_id,
               i.project_name, i.asset_group, i.asset_group_label,
               i.project_status, i.asset_location,
               i.start_price_display, i.final_price_display,
               i.start_price_amount, i.final_price_amount,
               i.last_crawled_at, i.batch_id
        FROM auction_items i
        {where_clause}
        ORDER BY i.item_id DESC
        LIMIT %s OFFSET %s
        """,
        params + [size, offset],
    )

    # 筛选选项
    platforms = query_all(
        _config,
        "SELECT DISTINCT source_platform FROM auction_items ORDER BY source_platform",
    )
    asset_groups = query_all(
        _config,
        "SELECT DISTINCT asset_group, asset_group_label FROM auction_items ORDER BY asset_group",
    )
    statuses = query_all(
        _config,
        "SELECT DISTINCT project_status FROM auction_items WHERE project_status IS NOT NULL AND project_status != '' ORDER BY project_status",
    )

    return {
        "total": count["cnt"],
        "page": page,
        "size": size,
        "items": rows,
        "filters": {
            "platforms": [r["source_platform"] for r in platforms],
            "asset_groups": [{"key": r["asset_group"], "label": r["asset_group_label"]} for r in asset_groups],
            "statuses": [r["project_status"] for r in statuses],
        },
    }


@router.get("/export")
def export_items(
    platform: str = "",
    asset_group: str = "",
    format: str = "csv",
):
    """导出标的列表（返回 CSV 文本）"""
    where = []
    params: list[Any] = []
    if platform:
        where.append("i.source_platform = %s")
        params.append(platform)
    if asset_group:
        where.append("i.asset_group = %s")
        params.append(asset_group)
    where_clause = "WHERE " + " AND ".join(where) if where else ""

    rows = query_all(
        _config,
        f"""
        SELECT i.source_platform, i.source_item_id, i.project_name,
               i.asset_group_label, i.project_status, i.asset_location,
               i.start_price_display, i.final_price_display,
               i.assessment_price_display, i.disposal_party,
               i.contact_info, i.last_crawled_at, i.batch_id
        FROM auction_items i
        {where_clause}
        ORDER BY i.item_id
        """,
        params,
    )

    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return output.getvalue()
    return rows


@router.get("/{item_id}")
def get_item(item_id: int):
    """标的详情"""
    item = query_one(_config, "SELECT * FROM auction_items WHERE item_id = %s", (item_id,))
    if not item:
        raise HTTPException(404, "标的不存在")

    group = item["asset_group"]
    asset_tables = {
        "real_estate": "asset_real_estate",
        "land": "asset_land",
        "equipment": "asset_equipment",
        "vehicle": "asset_vehicle",
        "debt": "asset_debt",
        "equity": "asset_equity",
        "ip": "asset_ip",
        "goods": "asset_goods",
        "usufruct": "asset_usufruct",
        "other": "asset_other",
    }

    # 资产特有字段
    special_row = {}
    if group in asset_tables:
        special = query_one(
            _config,
            f"SELECT * FROM {asset_tables[group]} WHERE item_id = %s",
            (item_id,),
        )
        if special:
            special_row = special

    # 证据字段
    extractions = query_all(
        _config,
        """
        SELECT * FROM field_extractions
        WHERE item_id = %s
        ORDER BY field_namespace, field_key
        """,
        (item_id,),
    )

    # 原始数据
    raw = query_one(
        _config,
        "SELECT * FROM raw_payloads WHERE item_id = %s",
        (item_id,),
    )

    # 资源
    resources = query_all(
        _config,
        "SELECT * FROM item_resources WHERE item_id = %s ORDER BY resource_type",
        (item_id,),
    )

    # 债权明细 / 知产明细
    debt_details = []
    ip_details = []
    if group == "debt":
        debt_details = query_all(
            _config,
            "SELECT * FROM asset_debt_details WHERE item_id = %s ORDER BY detail_index",
            (item_id,),
        )
    if group == "ip":
        ip_details = query_all(
            _config,
            "SELECT * FROM asset_ip_details WHERE item_id = %s ORDER BY detail_index",
            (item_id,),
        )

    return {
        "item": item,
        "special": special_row,
        "extractions": extractions,
        "raw": raw,
        "resources": resources,
        "debt_details": debt_details,
        "ip_details": ip_details,
    }


# ── 资源代理预览 ──

_SAFE_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
_SAFE_PDF_EXT = {".pdf"}
_SAFE_VIDEO_EXT = {".mp4", ".webm", ".mov"}
_SAFE_OFFICE = {".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx"}
_SAFE_EXT = _SAFE_IMAGE_EXT | _SAFE_PDF_EXT | _SAFE_VIDEO_EXT | _SAFE_OFFICE

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


@router.get("/resource/{resource_id}/proxy")
async def proxy_resource(
    resource_id: int,
    download: bool = Query(False, description="以附件形式下载"),
    item_id: int = Query(0, description="可选校验归属"),
):
    """代理资源 URL，解决跨域问题，支持图片/PDF/视频预览"""
    if not _config:
        raise HTTPException(500, "配置未初始化")

    where = "resource_id = %s"
    params: list[Any] = [resource_id]
    if item_id:
        where += " AND item_id = %s"
        params.append(item_id)

    row = query_one(_config, f"SELECT * FROM item_resources WHERE {where}", params)
    if not row:
        raise HTTPException(404, "资源不存在")

    url = (row.get("resource_url") or "").strip()
    if not url:
        raise HTTPException(400, "资源 URL 为空")

    name = row.get("resource_name") or ""
    fmt = row.get("resource_format") or ""

    # 推断扩展名
    ext = ""
    if fmt and fmt.startswith("."):
        ext = fmt.lower()
    elif name:
        import os
        _, ext = os.path.splitext(name)
        ext = ext.lower()
    if not ext and url:
        from urllib.parse import urlparse, urlsplit
        path = urlsplit(url).path
        import os
        _, ext = os.path.splitext(path)
        ext = ext.lower()

    # 安全校验
    if not ext:
        raise HTTPException(400, "无法识别文件类型")
    if ext not in _SAFE_EXT:
        raise HTTPException(403, f"不支持预览此文件类型: {ext or '未知'}")

    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    disposition = f'attachment; filename="{name}"' if download else f'inline; filename="{name}"'

    try:
        client = httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": url.rsplit("/", 1)[0] + "/" if "/" in url else url,
            },
        )
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)

        if resp.status_code >= 400:
            raise HTTPException(502, f"上游资源不可用 (HTTP {resp.status_code})")

        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=200,
            media_type=content_type,
            headers={
                "Content-Disposition": disposition,
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"代理请求失败: {str(e)[:200]}")
