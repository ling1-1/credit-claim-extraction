
import argparse
import html
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from jd_mysql_store import MySQLConfig, ensure_mysql_schema, get_item_detail_mysql, get_items_mysql


STATUS_LABELS = {
    "extracted": "已提取",
    "missing_on_page": "页面未提供",
    "empty_on_page": "页面字段为空",
    "parse_error": "解析失败",
    "conflict": "多来源冲突",
}

VIEWER_SOURCE_LABEL = "MySQL V2 正式表"


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def pretty_json(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def short_text(value: Any, limit: int = 140) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


DataSource = MySQLConfig


def get_items_for_source(source: DataSource, filters: dict[str, str] | None = None) -> dict[str, Any]:
    return get_items_mysql(source, filters)


def get_item_detail_for_source(source: DataSource, paimai_id: str) -> dict[str, Any]:
    return get_item_detail_mysql(source, paimai_id)


def render_layout(title: str, body: str) -> bytes:
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #202124;
      --muted: #69707a;
      --line: #d8dde5;
      --accent: #176b5b;
      --accent-soft: #e7f4ef;
      --warn: #9a5b00;
      --bad: #a33333;
      --ok: #1f7a4f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      padding: 12px 24px;
      display: flex;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
    }}
    .brand {{ font-size: 18px; font-weight: 700; }}
    .wrap {{ max-width: 1500px; margin: 0 auto; padding: 18px 24px 32px; }}
    .toolbar {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      grid-template-columns: minmax(220px, 2fr) repeat(3, minmax(150px, 1fr)) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }}
    label {{ color: var(--muted); font-size: 12px; display: block; margin-bottom: 4px; }}
    input, select {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--text);
    }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 7px 12px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      white-space: nowrap;
    }}
    .button.secondary {{ background: #fff; color: var(--accent); }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 14px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .metric strong {{ display: block; font-size: 20px; margin-top: 2px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
    }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 9px; vertical-align: top; text-align: left; }}
    th {{ background: #eef1f4; font-weight: 700; color: #363a40; position: sticky; top: 57px; z-index: 2; }}
    tr:hover td {{ background: #fbfcfd; }}
    .muted {{ color: var(--muted); }}
    .status {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #eef1f4; }}
    .status.ok {{ background: #e8f5ee; color: var(--ok); }}
    .status.warn {{ background: #fff4dc; color: var(--warn); }}
    .status.bad {{ background: #fbe7e7; color: var(--bad); }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 14px;
      overflow: hidden;
    }}
    .section h2 {{ margin: 0; padding: 12px 14px; font-size: 16px; border-bottom: 1px solid var(--line); background: #f0f3f5; }}
    .kv {{
      display: grid;
      grid-template-columns: 180px 1fr 110px 170px;
      border-bottom: 1px solid var(--line);
      min-height: 46px;
    }}
    .kv > div {{ padding: 9px 12px; border-right: 1px solid var(--line); overflow-wrap: anywhere; }}
    .kv > div:last-child {{ border-right: 0; }}
    .field-name strong {{ display: block; }}
    .field-name span {{ color: var(--muted); font-size: 12px; }}
    .value-empty {{ color: var(--muted); font-style: italic; }}
    details {{ padding: 10px 14px; border-top: 1px solid var(--line); }}
    summary {{ cursor: pointer; font-weight: 700; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f7f8fa;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      max-height: 420px;
      overflow: auto;
    }}
    pre.json-field {{
      max-height: 300px;
      margin: 0;
      font-size: 12px;
      line-height: 1.45;
    }}
    .title-row {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }}
    .title-row h1 {{ margin: 0; font-size: 22px; }}
    @media (max-width: 900px) {{
      .toolbar, .summary {{ grid-template-columns: 1fr; }}
      .kv {{ grid-template-columns: 1fr; }}
      .kv > div {{ border-right: 0; }}
      th {{ position: static; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">京东资产拍卖数据查看器</div>
    <div class="muted">{VIEWER_SOURCE_LABEL}</div>
  </div>
  <main class="wrap">{body}</main>
</body>
</html>"""
    return html_text.encode("utf-8")


def render_index(source: DataSource, query: dict[str, str]) -> bytes:
    data = get_items_for_source(source, query)
    items = data["items"]
    total = len(items)
    issue_total = sum(int(item["issue_fields"] or 0) for item in items)
    extracted_total = sum(int(item["extracted_fields"] or 0) for item in items)
    fields_total = sum(int(item["total_fields"] or 0) for item in items)
    completion = f"{(extracted_total / fields_total * 100):.1f}%" if fields_total else "0%"

    asset_options = ['<option value="">全部类型</option>']
    for group in data["asset_groups"]:
        selected = " selected" if query.get("asset_group") == group["asset_group"] else ""
        asset_options.append(
            f'<option value="{esc(group["asset_group"])}"{selected}>{esc(group["asset_group_label"])} ({group["count"]})</option>'
        )
    source_options = ['<option value="">全部来源</option>']
    for platform in data.get("source_platforms") or []:
        selected = " selected" if query.get("source_platform") == platform.get("source_platform") else ""
        source_label = platform.get("source_site_name") or platform.get("source_platform")
        source_options.append(
            f'<option value="{esc(platform.get("source_platform"))}"{selected}>{esc(source_label)} ({platform.get("count")})</option>'
        )
    status_options = ['<option value="">全部状态</option>']
    for status in data["statuses"]:
        selected = " selected" if query.get("project_status") == status else ""
        status_options.append(f'<option value="{esc(status)}"{selected}>{esc(status)}</option>')
    issue_selected = " selected" if query.get("issue") == "missing" else ""

    rows = []
    for item in items:
        issue = int(item["issue_fields"] or 0)
        total_fields = int(item["total_fields"] or 0)
        extracted = int(item["extracted_fields"] or 0)
        row_status = "ok" if issue == 0 else "warn"
        rows.append(
            f"""
            <tr>
              <td><a href="/item/{quote(str(item['paimai_id']))}">{esc(item['paimai_id'])}</a><div class="muted">{esc(item.get('source_site_name') or item.get('source_platform') or '')}</div></td>
              <td><strong>{esc(short_text(item['project_name'], 80))}</strong><div class="muted">{esc(short_text(item['asset_location'], 80))}</div></td>
              <td>{esc(item['asset_group_label'])}<div class="muted">{esc(item['jd_category_id'])} / {esc(item['jd_category_name'])}</div></td>
              <td>{esc(item['project_status'])}</td>
              <td>{esc(item['start_price_raw'])}</td>
              <td>{esc(item['final_price_raw'])}</td>
              <td><span class="status {row_status}">{extracted}/{total_fields}</span><div class="muted">缺失/异常 {issue}</div></td>
              <td><a class="button secondary" href="/item/{quote(str(item['paimai_id']))}">查看详情</a></td>
            </tr>
            """
        )

    body = f"""
    <form class="toolbar" method="get" action="/">
      <div>
        <label>关键词</label>
        <input name="q" value="{esc(query.get('q', ''))}" placeholder="项目名称、所在地、处置方、字段值">
      </div>
      <div>
        <label>数据来源</label>
        <select name="source_platform">{''.join(source_options)}</select>
      </div>
      <div>
        <label>资产类型</label>
        <select name="asset_group">{''.join(asset_options)}</select>
      </div>
      <div>
        <label>项目状态</label>
        <select name="project_status">{''.join(status_options)}</select>
      </div>
      <div>
        <label>字段状态</label>
        <select name="issue">
          <option value="">全部字段</option>
          <option value="missing"{issue_selected}>有缺失/异常</option>
        </select>
      </div>
      <button type="submit">筛选</button>
    </form>
    <div class="summary">
      <div class="metric"><span class="muted">当前列表</span><strong>{total}</strong></div>
      <div class="metric"><span class="muted">字段完整度</span><strong>{completion}</strong></div>
      <div class="metric"><span class="muted">字段总数</span><strong>{fields_total}</strong></div>
      <div class="metric"><span class="muted">缺失/异常字段</span><strong>{issue_total}</strong></div>
    </div>
    <table>
      <thead>
        <tr>
          <th>标的ID</th>
          <th>项目</th>
          <th>类型/类目</th>
          <th>状态</th>
          <th>起拍价</th>
          <th>最终价/当前价</th>
          <th>字段</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>{''.join(rows) if rows else '<tr><td colspan="8" class="muted">没有符合条件的数据</td></tr>'}</tbody>
    </table>
    """
    return render_layout("京东资产拍卖数据查看器", body)


def render_fields(fields: list[dict[str, Any]]) -> str:
    rows = []
    for field in fields:
        value = field.get("value")
        if value in (None, ""):
            value_html = '<span class="value-empty">空</span>'
        else:
            # 检测并格式化 JSON 字符串，避免原始 JSON 直接展示
            formatted = _try_format_json(value)
            if formatted is not None:
                value_html = f'<pre class="json-field">{esc(formatted)}</pre>'
            else:
                value_html = esc(value)
        status = field.get("status")
        status_label = STATUS_LABELS.get(status, field.get("status_label") or status)
        status_class = "ok" if status == "extracted" else "warn"
        source = " / ".join(filter(None, [field.get("source_payload_type"), field.get("source_path")]))
        rows.append(
            f"""
            <div class="kv">
              <div class="field-name">
                <strong>{esc(field.get('label'))}</strong>
                <span>{esc(field.get('comment'))}</span>
              </div>
              <div>{value_html}</div>
              <div><span class="status {status_class}">{esc(status_label)}</span></div>
              <div>
                <div>{esc(source)}</div>
                <div class="muted">{esc(field.get('missing_reason') or field.get('source_excerpt') or '')}</div>
              </div>
            </div>
            """
        )
    return "".join(rows)


def _try_format_json(value: Any) -> Optional[str]:
    """尝试将值解析为 JSON 并格式化返回，非 JSON 则返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not (text.startswith("{") or text.startswith("[")):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def summarize_resources(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return ""
    labels = {
        "attachment": "附件",
        "image": "图片",
        "video": "视频",
    }
    counts: dict[str, int] = {}
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "other")
        counts[resource_type] = counts.get(resource_type, 0) + 1
    parts = [
        f"{labels.get(resource_type, resource_type)} {count} 个"
        for resource_type, count in sorted(counts.items())
    ]
    if not parts:
        return ""
    return "；".join(parts) + "；详见下方“附件/图片/视频”"


def apply_resource_summary_to_fields(fields: list[dict[str, Any]], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = summarize_resources(resources)
    if not summary:
        return fields
    updated: list[dict[str, Any]] = []
    for field in fields:
        if field.get("key") != "attachments_json":
            updated.append(field)
            continue
        new_field = dict(field)
        new_field["value"] = summary
        new_field["source_excerpt"] = summary
        updated.append(new_field)
    return updated


def render_debt_details(details: list[dict[str, Any]], global_benchmark_date: Any = None) -> str:
    if not details:
        return ""
    global_benchmark = "" if global_benchmark_date is None else str(global_benchmark_date).strip()
    detail_benchmark_values = {
        str(detail.get("benchmark_date") or "").strip()
        for detail in details
        if str(detail.get("benchmark_date") or "").strip()
    }
    show_detail_benchmark = bool(
        detail_benchmark_values
        and not (global_benchmark and detail_benchmark_values == {global_benchmark})
    )
    rows = []
    for detail in details:
        debtor_name = detail.get("debtor_name") or detail.get("debtor_or_asset")
        guarantor = detail.get("guarantor") or detail.get("guarantor_or_related_party")
        benchmark_cell = f"<td>{esc(detail.get('benchmark_date'))}</td>" if show_detail_benchmark else ""
        rows.append(
            f"""
            <tr>
              <td>{esc(detail.get('sequence_no'))}</td>
              <td>{esc(debtor_name)}</td>
              <td>{esc(detail.get('principal_balance'))}</td>
              <td>{esc(detail.get('interest_balance'))}</td>
              <td>{esc(detail.get('recovery_fee'))}</td>
              <td>{esc(detail.get('claim_total'))}</td>
              <td>{esc(detail.get('collateral'))}</td>
              <td>{esc(guarantor)}</td>
              <td>{esc(detail.get('litigation_status'))}</td>
              {benchmark_cell}
            </tr>
            """
        )
    benchmark_header = "<th>基准日</th>" if show_detail_benchmark else ""
    return f"""
    <section class="section">
      <h2>债权包明细</h2>
      <table>
        <thead>
          <tr>
            <th>原表序号</th>
            <th>债务人名称</th>
            <th>本金余额</th>
            <th>利息余额</th>
            <th>实现债权费用</th>
            <th>债权合计</th>
            <th>抵质押物</th>
            <th>保证人</th>
            <th>诉讼状态</th>
            {benchmark_header}
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_ip_details(details: list[dict[str, Any]]) -> str:
    if not details:
        return ""
    rows = []
    for detail in details:
        rows.append(
            f"""
            <tr>
              <td>{esc(detail.get('sequence_no'))}</td>
              <td>{esc(detail.get('ip_name'))}</td>
              <td>{esc(detail.get('certificate_no'))}</td>
              <td>{esc(detail.get('ip_type'))}</td>
              <td>{esc(detail.get('application_date'))}</td>
              <td>{esc(detail.get('patent_type'))}</td>
              <td>{esc(detail.get('status'))}</td>
            </tr>
            """
        )
    return f"""
    <section class="section">
      <h2>知识产权明细</h2>
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>单项名称</th>
            <th>证号/登记号/申请号</th>
            <th>知产类型</th>
            <th>申请日/登记日期</th>
            <th>专利类型</th>
            <th>法律状态</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_resources(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return """
        <section class="section">
          <h2>附件/图片/视频</h2>
          <p class="muted">无结构化资源</p>
        </section>
        """

    type_labels = {
        "attachment": "附件",
        "image": "图片",
        "video": "视频",
    }
    role_labels = {
        "assessment_report": "评估报告",
        "announcement": "公告",
        "notice": "须知",
        "inventory": "清单",
        "contract": "合同/协议",
        "site_image": "现场图片",
        "site_video": "现场视频",
        "other": "其他",
    }
    rows = []
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "")
        resource_role = str(resource.get("resource_role") or "")
        resource_name = resource.get("resource_name") or resource.get("file_name") or resource_role or resource_type
        resource_url = str(resource.get("resource_url") or "").strip()
        if resource_url:
            link_html = (
                f'<a href="{esc(resource_url)}" target="_blank" rel="noreferrer">打开</a>'
                f'<div class="muted">{esc(short_text(resource_url, 120))}</div>'
            )
        else:
            link_html = '<span class="value-empty">空</span>'
        rows.append(
            f"""
            <tr>
              <td>{esc(type_labels.get(resource_type, resource_type))}</td>
              <td>{esc(role_labels.get(resource_role, resource_role))}</td>
              <td>{esc(resource_name)}</td>
              <td>{link_html}</td>
              <td>{esc(resource.get('source_section'))}</td>
            </tr>
            """
        )

    return f"""
    <section class="section">
      <h2>附件/图片/视频</h2>
      <table>
        <thead>
          <tr>
            <th>类型</th>
            <th>用途</th>
            <th>名称</th>
            <th>链接</th>
            <th>来源区块</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_duplicates(duplicates: list[dict[str, Any]]) -> str:
    if not duplicates:
        return ""
    rows = []
    for duplicate in duplicates:
        rows.append(
            f"""
            <tr>
              <td>{esc(duplicate.get('source_platform'))}</td>
              <td><a href="/item/{quote(str(duplicate.get('paimai_id')))}">{esc(duplicate.get('source_item_id'))}</a></td>
              <td>{esc(short_text(duplicate.get('project_name'), 90))}</td>
              <td>{esc(short_text(duplicate.get('asset_location'), 90))}</td>
              <td>{esc(duplicate.get('updated_at'))}</td>
            </tr>
            """
        )
    return f"""
    <section class="section">
      <h2>疑似重复资产</h2>
      <table>
        <thead>
          <tr>
            <th>来源平台</th>
            <th>平台ID</th>
            <th>项目名称</th>
            <th>所在地</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def render_detail(source: DataSource, paimai_id: str) -> bytes:
    try:
        detail = get_item_detail_for_source(source, paimai_id)
    except KeyError as exc:
        return render_layout("找不到标的", f'<div class="section"><h2>找不到标的</h2><p>{esc(exc)}</p><p><a href="/">返回列表</a></p></div>')

    item = detail["item"]
    raw = detail["raw"]
    resources = detail.get("resources") or []
    common_fields = apply_resource_summary_to_fields(detail["common_fields"], resources)
    source_url = item.get("source_url") or f"https://paimai.jd.com/{paimai_id}"
    body = f"""
    <div class="title-row">
      <div>
        <h1>{esc(item.get('project_name') or paimai_id)}</h1>
        <div class="muted">标的ID：{esc(paimai_id)}　资产类型：{esc(item.get('asset_group_label'))}　京东类目：{esc(item.get('jd_category_name'))}</div>
      </div>
      <div><a class="button secondary" href="/">返回列表</a></div>
    </div>

    <section class="section">
      <h2>快速概览</h2>
      <div class="kv"><div>来源页面</div><div><a href="{esc(source_url)}" target="_blank">{esc(source_url)}</a></div><div>项目状态</div><div>{esc(item.get('project_status'))}</div></div>
      <div class="kv"><div>所在地</div><div>{esc(item.get('asset_location'))}</div><div>处置方</div><div>{esc(item.get('disposal_party'))}</div></div>
      <div class="kv"><div>起拍价</div><div>{esc(item.get('start_price_raw'))}</div><div>最终价/当前价</div><div>{esc(item.get('final_price_raw'))}</div></div>
    </section>

    <section class="section">
      <h2>共有字段</h2>
      {render_fields(common_fields)}
    </section>

    <section class="section">
      <h2>{esc(detail['asset_group_label'])}特有字段</h2>
      {render_fields(detail['special_fields'])}
    </section>

    {render_debt_details(detail.get('debt_details') or [], (detail.get('special_row') or {}).get('benchmark_date'))}
    {render_ip_details(detail.get('ip_details') or [])}
    {render_resources(resources)}
    {render_duplicates(detail.get('duplicates') or [])}

    <section class="section">
      <h2>原始证据</h2>
      <details><summary>列表原始 JSON</summary><pre>{esc(pretty_json(raw.get('list_json')))}</pre></details>
      <details><summary>详情原始 JSON</summary><pre>{esc(pretty_json(raw.get('detail_json')))}</pre></details>
      <details><summary>商品基础信息原始 JSON</summary><pre>{esc(pretty_json(raw.get('product_basic_json')))}</pre></details>
      <details><summary>实时原始 JSON</summary><pre>{esc(pretty_json(raw.get('realtime_json')))}</pre></details>
      <details><summary>标的详情 HTML</summary><pre>{esc(raw.get('description_html'))}</pre></details>
      <details><summary>竞买须知 HTML</summary><pre>{esc(raw.get('notice_html'))}</pre></details>
      <details><summary>竞买公告 HTML</summary><pre>{esc(raw.get('announcement_html'))}</pre></details>
      <details><summary>附件原始 JSON</summary><pre>{esc(pretty_json(raw.get('attachments_json')))}</pre></details>
    </section>
    """
    return render_layout(str(item.get("project_name") or paimai_id), body)


class ViewerHandler(BaseHTTPRequestHandler):
    data_source: DataSource

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values and values[0]}
        if parsed.path == "/":
            self._send(render_index(self.data_source, query))
            return
        if parsed.path.startswith("/item/"):
            paimai_id = unquote(parsed.path.removeprefix("/item/"))
            self._send(render_detail(self.data_source, paimai_id))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(source: DataSource, host: str, port: int, open_browser: bool) -> None:
    global VIEWER_SOURCE_LABEL
    ensure_mysql_schema(source)
    source_label = f"MySQL {source.host}:{source.port}/{source.database}"
    VIEWER_SOURCE_LABEL = source_label
    ViewerHandler.data_source = source
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    url = f"http://{host}:{port}/"
    print(f"京东资产拍卖数据查看器已启动：{url}")
    print(f"数据库：{source_label}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拍卖数据 MySQL V2 本地查看器")
    parser.add_argument("--mysql-host", default="127.0.0.1", help="MySQL 主机")
    parser.add_argument("--mysql-port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--mysql-user", default="root", help="MySQL 用户")
    parser.add_argument("--mysql-password", default="root", help="MySQL 密码")
    parser.add_argument("--mysql-database", default="auction_data", help="MySQL 数据库")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source: DataSource = MySQLConfig(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.mysql_database,
    )
    run_server(source, args.host, args.port, args.open)


if __name__ == "__main__":
    main()
