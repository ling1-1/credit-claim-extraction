import unittest
from pathlib import Path

from fastapi import HTTPException


class WebAdminFrontendTests(unittest.TestCase):
    def test_static_shell_has_clean_chinese_text_and_no_emoji_menu(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("<title>拍卖数据采集管理平台</title>", html)
        self.assertIn("任务管理", html)
        self.assertNotIn("鎷", html)
        self.assertNotIn("馃", html)
        self.assertNotIn("鈴", html)

    def test_frontend_design_doc_is_utf8_and_not_mojibake(self):
        matched_docs = []
        for path in Path("docs").glob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "定时任务平台前端设计方案" in text:
                matched_docs.append(text)

        self.assertTrue(matched_docs, "missing frontend design document")
        for text in matched_docs:
            self.assertIn("后台工作台", text)
            self.assertNotIn("鎷", text)
            self.assertNotIn("馃", text)
            self.assertNotIn("鈴", text)
            self.assertNotIn("�", text)

    def test_task_status_api_exposes_known_task(self):
        from web_admin.routers import tasks

        original = tasks.task_trigger.get_task_status
        try:
            tasks.task_trigger.get_task_status = lambda task_id: {
                "task_id": task_id,
                "status": "completed",
                "type": "crawl",
            }
            result = tasks.get_task("task-1")
        finally:
            tasks.task_trigger.get_task_status = original

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["status"], "completed")

    def test_task_status_api_returns_404_for_missing_task(self):
        from web_admin.routers import tasks

        original = tasks.task_trigger.get_task_status
        try:
            tasks.task_trigger.get_task_status = lambda task_id: None
            with self.assertRaises(HTTPException) as ctx:
                tasks.get_task("missing")
        finally:
            tasks.task_trigger.get_task_status = original

        self.assertEqual(ctx.exception.status_code, 404)

    def test_job_create_rejects_invalid_cron_before_database_write(self):
        from web_admin.routers import jobs

        job = jobs.JobCreate(
            job_name="bad cron",
            source_platform="jd",
            cron_expr="not a cron",
        )
        with self.assertRaises(HTTPException) as ctx:
            jobs.create_job(job)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Cron", str(ctx.exception.detail))

    def test_frontend_rejects_api_html_fallback(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn('url.startsWith("/api/")', html)
        self.assertIn("接口返回了非 JSON 内容", html)

    def test_dashboard_has_chart_containers(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn('ref="platformChartEl"', html)
        self.assertIn('ref="assetChartEl"', html)
        self.assertIn('ref="trendChartEl"', html)
        self.assertIn("renderCharts", html)

    def test_item_detail_uses_chinese_field_labels(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("const FIELD_LABELS", html)
        self.assertIn('right_certificate_no: "权证编号"', html)
        self.assertIn("fieldRows(data.value?.special || {})", html)
        self.assertIn(":label=\"col.label\"", html)

    def test_item_detail_renders_bid_records_as_table(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("bidRecords", html)
        self.assertIn("formatBidTime", html)
        self.assertIn("出价记录", html)
        self.assertIn("出价时间", html)
        self.assertIn("出价人", html)
        self.assertNotIn("[[{'price'", html)

    def test_item_detail_special_image_fields_are_previewable(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("specialImageUrls", html)
        self.assertIn("normalizeFieldImageUrl", html)
        self.assertIn("img30.360buyimg.com/popWaterMark", html)
        self.assertIn("openFieldImagePreview", html)
        self.assertIn("field-preview-image", html)
        self.assertIn("@click=\"openFieldImagePreview", html)

    def test_item_detail_source_url_is_not_rendered_with_v_html(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertNotIn("v-html", html)
        self.assertIn("safeSourceUrl", html)
        self.assertIn(':href="safeSourceUrl(row[1])"', html)

    def test_queue_page_shows_last_error(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("row.last_error || row.error_message", html)

    def test_ai_queue_page_exposes_parsing_pause_and_model_columns(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn('parsing: "warning"', html)
        self.assertIn('paused: "info"', html)
        self.assertIn("/api/queues/ai/pause", html)
        self.assertIn("/api/queues/ai/resume", html)
        self.assertIn("暂停未解析任务", html)
        self.assertIn("恢复暂停任务", html)
        self.assertIn("实际解析中", html)
        self.assertIn("已暂停", html)
        self.assertIn("modelText(row)", html)
        self.assertIn("running_profile_name", html)

    def test_frontend_has_auth_gate_for_future_remote_access(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("/api/auth/status", html)
        self.assertIn("/api/auth/login", html)
        self.assertIn("auth.enabled && !auth.authenticated", html)

    def test_login_page_has_polished_admin_layout(self):
        html = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertIn("login-shell", html)
        self.assertIn("login-visual", html)
        self.assertIn("login-card", html)
        self.assertIn("login-badge", html)
