import unittest
from pathlib import Path
from types import SimpleNamespace


import jd_scraper_v2 as jd_v2
from jd.ai_extractor import AIExtractionContext, AIExtractionResult
from multi_platform_runner import (
    AliLiveHandler,
    CquaeLiveHandler,
    GxcqLiveHandler,
    JdLiveHandler,
    MultiPlatformRunner,
    PlatformRecord,
    SdcqjyLiveHandler,
    build_handlers,
    ejy365_asset_for_project_type,
    normalize_attachments_payload,
    split_ai_results,
)
from platform_adapters.gxcq_adapter import GxcqDetailBundle, GxcqListItem
from platform_adapters.ali_adapter import AliDetailBundle, AliListItem
from platform_adapters.jd_adapter import JDPlatformAdapter


class FakeDB:
    def __init__(self):
        self.batches = []
        self.raw_calls = []
        self.common_calls = []
        self.special_calls = []
        self.debt_details_calls = []
        self.ip_details_calls = []
        self.ai_enrichment_calls = []
        self.finished = []

    def start_batch(self, parameters):
        self.batches.append(parameters)
        return "batch-test"

    def finish_batch(self, batch_id, status, message=""):
        self.finished.append((batch_id, status, message))

    def upsert_raw_payloads(self, **kwargs):
        self.raw_calls.append(kwargs)

    def upsert_common_item(self, **kwargs):
        self.common_calls.append(kwargs)

    def upsert_special_item(self, **kwargs):
        self.special_calls.append(kwargs)

    def upsert_debt_details(self, **kwargs):
        self.debt_details_calls.append(kwargs)

    def upsert_ip_details(self, **kwargs):
        self.ip_details_calls.append(kwargs)

    def enqueue_ai_enrichment_task(self, **kwargs):
        self.ai_enrichment_calls.append(kwargs)


class FakeHandler:
    source_platform = "fake"
    source_site_name = "Fake Platform"

    def __init__(self):
        self.detail_calls = []

    def fetch_list(self, limit):
        return [
            {"id": "ok-1", "url": "https://fake.test/items/ok-1"},
            {"id": "bad", "url": "https://fake.test/items/bad"},
            {"id": "ok-2", "url": "https://fake.test/items/ok-2"},
        ][:limit]

    def fetch_detail(self, list_item):
        self.detail_calls.append(list_item["id"])
        if list_item["id"] == "bad":
            raise RuntimeError("detail failed")
        return {"id": list_item["id"], "url": list_item["url"], "html": "<html>detail</html>"}

    def build_record(self, detail_bundle):
        source_item_id = detail_bundle["id"]
        source_url = detail_bundle["url"]
        return PlatformRecord(
            source_platform=self.source_platform,
            source_site_name=self.source_site_name,
            source_item_id=source_item_id,
            source_url=source_url,
            asset_group="debt",
            category_id="ZQ",
            category_name="debt",
            common_values={
                "source_platform": self.source_platform,
                "source_item_id": source_item_id,
                "source_url": source_url,
                "source_site_name": self.source_site_name,
                "asset_type": "debt",
                "project_name": f"Project {source_item_id}",
                "start_price_raw": "100万元",
                "final_price_raw": "100万元",
                "data_source": self.source_site_name,
            },
            field_results={},
            special_values={"debtor_name": "Debtor A"},
            special_field_results={},
            raw_payloads={"detail_html": detail_bundle["html"], "list_json": {"id": source_item_id}},
            attachments_json={"media": [{"imageVideoArea": {"imageList": [{"imagePath": "jfs/t1/demo.jpg"}]}}]},
        )


class FakeAIExtractor:
    def is_available(self):
        return True

    def batch_extract(self, fields, context):
        return {
            "debt_package_details_json": AIExtractionResult(
                field_key="debt_package_details_json",
                field_label="debt details",
                value=[
                    {
                        "sequence_no": "1",
                        "debtor_name": "Debtor A",
                        "principal_balance": "100万元",
                        "interest_balance": "10万元",
                        "claim_total": "110万元",
                        "benchmark_date": "2026年1月1日",
                    }
                ],
                confidence=0.91,
                original_text="Debtor A 100万元 10万元 110万元 2026年1月1日",
            )
        }


class FakeAIAdapter:
    def build_ai_context(self, detail_bundle):
        return AIExtractionContext(detail_text="debt detail table", asset_group="debt")


class FakeAIHandler(FakeHandler):
    def __init__(self):
        super().__init__()
        self.adapter = FakeAIAdapter()


class MultiPlatformRunnerTests(unittest.TestCase):
    def test_jd_live_handler_passes_limit_as_total_limit(self):
        class FakeJDAdapter:
            def __init__(self):
                self.kwargs = None

            def crawl_sample(self, **kwargs):
                self.kwargs = kwargs
                return {"batch_id": "batch-jd", "items_seen": 3, "errors": []}

        handler = JdLiveHandler.__new__(JdLiveHandler)
        handler.adapter = FakeJDAdapter()
        handler.output_dir = Path("outputs") / "test_jd_limit"
        handler.categories = None

        result = handler.crawl_with_db(FakeDB(), limit=3)

        self.assertEqual(result.scanned_count, 3)
        self.assertEqual(handler.adapter.kwargs["per_category_limit"], 3)
        self.assertEqual(handler.adapter.kwargs["total_limit"], 3)

    def test_jd_platform_adapter_forwards_total_limit_to_scraper(self):
        class FakeScraper:
            def __init__(self):
                self.kwargs = None

            def crawl_sample(self, **kwargs):
                self.kwargs = kwargs
                return {"batch_id": "batch-jd", "items_seen": 2, "errors": []}

        class FakeAdapter(JDPlatformAdapter):
            def __init__(self):
                self.scraper = FakeScraper()

            def create_scraper(self, db):
                return self.scraper

        adapter = FakeAdapter()
        adapter.crawl_sample(
            db=FakeDB(),
            per_category_limit=2,
            output_dir=Path("outputs") / "test_jd_adapter_limit",
            total_limit=2,
        )

        self.assertEqual(adapter.scraper.kwargs["per_category_limit"], 2)
        self.assertEqual(adapter.scraper.kwargs["total_limit"], 2)

    def test_runner_writes_successful_items_and_continues_after_detail_failure(self):
        db = FakeDB()
        handler = FakeHandler()
        runner = MultiPlatformRunner(db=db, handlers={"fake": handler}, ai_enabled=False)

        result = runner.crawl_platform("fake", limit=3)

        self.assertEqual(result.scanned_count, 3)
        self.assertEqual(result.success_count, 2)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(handler.detail_calls, ["ok-1", "bad", "ok-2"])
        self.assertEqual(len(db.raw_calls), 2)
        self.assertEqual(len(db.common_calls), 2)
        self.assertEqual(len(db.special_calls), 2)
        self.assertEqual(db.raw_calls[0]["source_platform"], "fake")
        self.assertEqual(db.raw_calls[0]["source_item_id"], "ok-1")
        self.assertEqual(db.common_calls[0]["paimai_id"], "ok-1")
        self.assertEqual(db.common_calls[0]["values"]["source_platform"], "fake")
        self.assertEqual(db.finished[0][1], "partial_success")

    def test_cquae_browser_fallback_reports_persistent_waf(self):
        class FakeClient:
            def get_text(self, url):
                return "knownsec anti-scraping challenge"

        class FakeBrowser:
            def fetch_html(self, url):
                return "<html>knownsec anti-scraping challenge</html>"

        handler = CquaeLiveHandler(use_browser=True)
        handler.client = FakeClient()
        handler.browser = FakeBrowser()

        with self.assertRaisesRegex(RuntimeError, "WAF challenge"):
            handler._fetch_html("https://www.cquae.com/Project")

    def test_gxcq_fetch_detail_uses_detail_api_not_spa_html(self):
        class ExplodingClient:
            def get_text(self, url):
                raise AssertionError("SPA html request should not be used")

        class FakeGxcqAdapter:
            def __init__(self):
                self.api_calls = []
                self.parse_calls = []

            def fetch_detail_api(self, list_item):
                self.api_calls.append(list_item.source_item_id)
                return {"code": 200, "data": {"title": "详情"}}

            def parse_detail_response(self, detail_data, list_item=None):
                self.parse_calls.append((detail_data, list_item.source_item_id if list_item else None))
                return GxcqDetailBundle(
                    source_item_id=list_item.source_item_id,
                    source_url=list_item.source_url,
                    title=list_item.title,
                    key_values={"项目名称": list_item.title},
                    attachments=[],
                    detail_text="详情",
                    list_item=list_item,
                    detail_json=detail_data,
                )

        handler = GxcqLiveHandler()
        handler.client = ExplodingClient()
        handler.adapter = FakeGxcqAdapter()
        list_item = GxcqListItem(
            source_item_id="SWZC260001",
            source_url="https://ljs.gxcq.com.cn/#/projectDetail?id=SWZC260001",
            title="测试项目",
        )

        detail = handler.fetch_detail(list_item)

        self.assertEqual(detail.source_item_id, "SWZC260001")
        self.assertEqual(handler.adapter.api_calls, ["SWZC260001"])
        self.assertEqual(handler.adapter.parse_calls[0][1], "SWZC260001")

    def test_gxcq_fetch_list_caps_items_to_requested_limit(self):
        class FakeGxcqAdapter:
            def fetch_list_api(self, page, size):
                return {"page": page, "size": size}

            def parse_list_response(self, api_data):
                return [
                    GxcqListItem(source_item_id=f"SWZC{i}", source_url=f"https://example.test/{i}", title=f"item {i}")
                    for i in range(5)
                ]

        handler = GxcqLiveHandler()
        handler.adapter = FakeGxcqAdapter()

        items = handler.fetch_list(2)

        self.assertEqual([item.source_item_id for item in items], ["SWZC0", "SWZC1"])

    def test_cquae_handler_does_not_store_adapter_field_results_as_common_field(self):
        class FakeCquaeAdapter:
            def map_common_candidates(self, detail_bundle):
                return {
                    "asset_group": "debt",
                    "asset_type": "债权",
                    "project_name": "测试债权",
                    "start_price_raw": "100万元",
                    "field_results": {
                        "project_name": {
                            "value": "测试债权",
                            "status": "extracted",
                            "source_payload_type": "detail_html",
                            "source_path": "title",
                        }
                    },
                }

            def classify_bundle(self, detail_bundle):
                return "debt"

            def map_special_candidates(self, detail_bundle, asset_group):
                return {}

        handler = CquaeLiveHandler(use_browser=False)
        handler.adapter = FakeCquaeAdapter()
        bundle = SimpleNamespace(
            source_item_id="SWZC260001",
            source_url="https://www.cquae.com/project/SWZC260001",
            list_item=None,
            raw_html="<html>测试债权</html>",
            attachments=[],
            image_urls=[],
        )

        record = handler.build_record(bundle)

        self.assertNotIn("field_results", record.common_values)
        self.assertEqual(record.field_results["project_name"]["source_path"], "title")

    def test_cquae_handler_does_not_fallback_to_sdcqjy(self):
        class ExplodingCquaeHandler(CquaeLiveHandler):
            def _fetch_html(self, url):
                raise RuntimeError("cquae waf")

        handler = ExplodingCquaeHandler(use_browser=False)

        with self.assertRaisesRegex(RuntimeError, "cquae waf"):
            handler.fetch_list(1)

    def test_sdcqjy_handler_uses_independent_platform_identity(self):
        class FakeSdcqjyAdapter:
            def map_common_candidates(self, detail_bundle):
                return {
                    "asset_group": "real_estate",
                    "asset_type": "房地产",
                    "project_name": "山东房产项目",
                    "start_price_raw": "10万元",
                    "source_site_name": "山东产权交易中心公开门户",
                }

            def classify_bundle(self, detail_bundle):
                return "real_estate"

            def map_special_candidates(self, detail_bundle, asset_group):
                return {}

        handler = SdcqjyLiveHandler()
        handler.adapter = FakeSdcqjyAdapter()
        bundle = SimpleNamespace(
            source_item_id="SWZC260001",
            source_url="http://www.sdcqjy.com/proj/tc/SWZC260001",
            list_item=SimpleNamespace(raw_fields={}),
            raw_html="<html>山东房产项目</html>",
            attachments=[],
            image_urls=[],
        )

        record = handler.build_record(bundle)

        self.assertEqual(record.source_platform, "sdcqjy")
        self.assertEqual(record.source_site_name, "山东产权交易中心公开门户")
        self.assertEqual(record.category_id, "sdcqjy")
        self.assertEqual(record.field_results["project_name"]["source_payload_type"], "detail_html")

    def test_build_handlers_registers_sdcqjy_separately(self):
        args = SimpleNamespace(
            ali_item_url=[],
            jd_categories="",
            request_timeout=0,
            output_dir=Path("outputs"),
            no_browser=True,
            cquae_headed=False,
            cquae_profile_path="",
            ali_profile_path="",
            ali_headless=True,
            browser_timeout_ms=0,
        )

        handlers = build_handlers(args)

        self.assertIn("cquae", handlers)
        self.assertIn("sdcqjy", handlers)
        self.assertNotEqual(handlers["cquae"].source_platform, handlers["sdcqjy"].source_platform)

    def test_ali_handler_does_not_store_adapter_field_results_as_common_field(self):
        class FakeAliAdapter:
            def map_common_candidates(self, detail_bundle):
                return {
                    "asset_group": "real_estate",
                    "asset_type": "房地产",
                    "project_name": "测试房产",
                    "start_price_raw": "10万元",
                    "field_results": {
                        "project_name": {
                            "value": "测试房产",
                            "status": "extracted",
                            "source_payload_type": "detail_html",
                            "source_path": "title",
                        }
                    },
                }

            def map_special_candidates(self, detail_bundle, asset_group):
                return {}

        handler = AliLiveHandler()
        handler.adapter = FakeAliAdapter()
        bundle = SimpleNamespace(
            status="ok",
            block_reason="",
            asset_group="real_estate",
            source_item_id="106000001",
            source_url="https://pages-fast.m.taobao.com/item/106000001",
            category="房地产",
            list_item=None,
            rendered_html="<html>测试房产</html>",
            notice_html="",
            top_json={},
            attachments=[],
            image_urls=[],
        )

        record = handler.build_record(bundle)

        self.assertNotIn("field_results", record.common_values)
        self.assertEqual(record.field_results["project_name"]["source_path"], "title")

    def test_ali_item_url_extracts_item_id_not_uniapp_id(self):
        handler = AliLiveHandler()

        item_id = handler._extract_item_id(
            "https://pages-fast.m.taobao.com/wow/z/app/pm/dzc-ice/dzc-detail?"
            "x-ssr=true&disableNav=YES&uniapp_id=1100093&itemId=1053599188591"
        )

        self.assertEqual(item_id, "1053599188591")

    def test_ejy365_project_type_mapping_covers_more_than_creditor_rights(self):
        self.assertEqual(ejy365_asset_for_project_type("ZQ"), ("debt", "债权"))
        self.assertEqual(ejy365_asset_for_project_type("FC"), ("real_estate", "房地产"))
        self.assertEqual(ejy365_asset_for_project_type("CL"), ("vehicle", "车辆"))
        self.assertEqual(ejy365_asset_for_project_type("ZSCQ"), ("ip", "知识产权"))
        self.assertEqual(ejy365_asset_for_project_type("UNKNOWN"), ("other", "其他"))

    def test_ali_fetch_detail_merges_browser_detail_when_mtop_succeeds(self):
        class FakeBrowserFetcher:
            def __init__(self):
                self.calls = []

            def fetch_detail(self, url, *, profile_path=None, timeout_ms=0):
                self.calls.append((url, profile_path, timeout_ms))
                return AliDetailBundle(
                    status="ok",
                    source_item_id="106000002",
                    source_url=url,
                    title="济南市某写字楼",
                    category="写字楼",
                    asset_location="山东省济南市",
                    rendered_text="页面文本",
                )

        class FakeAliAdapter:
            def __init__(self):
                self.browser_fetcher = FakeBrowserFetcher()
                self.merge_calls = []

            def fetch_mtop_detail(self, list_item):
                return AliDetailBundle(
                    status="ok",
                    source_item_id=list_item.item_id,
                    source_url=list_item.source_url,
                    title="济南市某写字楼",
                    category="prop",
                    asset_group="other",
                )

            def merge_detail_bundles(self, primary, fallback):
                self.merge_calls.append((primary, fallback))
                primary.asset_group = "real_estate"
                primary.asset_type = "房地产"
                primary.asset_location = fallback.asset_location
                primary.rendered_text = fallback.rendered_text
                return primary

        handler = AliLiveHandler(profile_path="C:/profile", timeout_ms=12345)
        handler.adapter = FakeAliAdapter()
        list_item = AliListItem(
            item_id="106000002",
            source_url="https://zc-paimai.taobao.com/auction.htm?itemId=106000002",
            title="济南市某写字楼",
        )

        detail = handler.fetch_detail(list_item)

        self.assertEqual(handler.adapter.browser_fetcher.calls[0][1], "C:/profile")
        self.assertEqual(handler.adapter.browser_fetcher.calls[0][2], 12345)
        self.assertEqual(len(handler.adapter.merge_calls), 1)
        self.assertEqual(detail.asset_group, "real_estate")
        self.assertEqual(detail.asset_location, "山东省济南市")
        self.assertEqual(detail.rendered_text, "页面文本")

    def test_normalize_attachments_payload_filters_entries_without_real_url(self):
        payload = normalize_attachments_payload(
            [
                {"name": "受让申请书.doc", "url": ""},
                {"name": "公告.pdf", "url": "https://example.test/notice.pdf"},
                "只有文件名没有链接.doc",
            ]
        )

        self.assertEqual(payload["files"], [{"name": "公告.pdf", "url": "https://example.test/notice.pdf"}])
        self.assertEqual(payload["media"], [])

    def test_normalize_attachments_payload_extracts_nested_files_and_media(self):
        payload = normalize_attachments_payload(
            {
                "data": {
                    "attachmentList": [
                        {
                            "attachmentName": "评估报告.pdf",
                            "attachmentAddress": "https://storage.jd.com/report.pdf",
                            "attachmentFormat": "pdf",
                        }
                    ],
                    "imageVideoArea": {
                        "imageList": [
                            {"imagePath": "jfs/t1/demo.jpg"},
                        ]
                    },
                }
            }
        )

        self.assertEqual(payload["files"][0]["name"], "评估报告.pdf")
        self.assertEqual(payload["files"][0]["url"], "https://storage.jd.com/report.pdf")
        self.assertTrue(payload["media"])

    def test_normalize_attachments_payload_drops_empty_media_shells(self):
        payload = normalize_attachments_payload(
            [],
            [{"imageVideoArea": {"imageList": []}}],
        )

        self.assertEqual(payload, {"files": [], "media": []})

    def test_runner_supports_item_concurrency_without_stopping_batch(self):
        db = FakeDB()
        handler = FakeHandler()
        runner = MultiPlatformRunner(db=db, handlers={"fake": handler}, ai_enabled=False, item_concurrency=2)

        result = runner.crawl_platform("fake", limit=3)

        self.assertEqual(result.scanned_count, 3)
        self.assertEqual(result.success_count, 2)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(len(db.raw_calls), 2)

    def test_runner_persists_ai_debt_detail_rows(self):
        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = FakeAIExtractor()
        try:
            db = FakeDB()
            handler = FakeAIHandler()
            runner = MultiPlatformRunner(db=db, handlers={"fake": handler}, ai_enabled=True)

            result = runner.crawl_platform("fake", limit=1)

            self.assertEqual(result.success_count, 1)
            self.assertEqual(len(db.debt_details_calls), 1)
            details = db.debt_details_calls[0]["details"]
            self.assertEqual(details[0]["debtor_name"], "Debtor A")
            self.assertEqual(db.special_calls[0]["values"]["household_count"], "1")
            self.assertEqual(db.debt_details_calls[0]["source_platform"], "fake")
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_runner_async_ai_writes_item_then_enqueues_enrichment(self):
        class ExplodingAIExtractor(FakeAIExtractor):
            def batch_extract(self, fields, context):
                raise AssertionError("sync AI should not run in async mode")

        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = ExplodingAIExtractor()
        try:
            db = FakeDB()
            handler = FakeAIHandler()
            runner = MultiPlatformRunner(db=db, handlers={"fake": handler}, ai_enabled=True, ai_mode="async")

            result = runner.crawl_platform("fake", limit=1)

            self.assertEqual(result.success_count, 1)
            self.assertEqual(len(db.common_calls), 1)
            self.assertEqual(len(db.ai_enrichment_calls), 1)
            queued = db.ai_enrichment_calls[0]
            self.assertEqual(queued["source_platform"], "fake")
            self.assertEqual(queued["source_item_id"], "ok-1")
            self.assertEqual(queued["asset_group"], "debt")
            self.assertEqual(queued["context"]["detail_text"], "debt detail table")
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_async_ai_context_uses_record_asset_group_not_adapter_default(self):
        class RealEstateHandler(FakeAIHandler):
            def build_record(self, detail_bundle):
                record = super().build_record(detail_bundle)
                record.asset_group = "real_estate"
                record.category_id = "FC"
                record.category_name = "房地产"
                record.common_values["asset_type"] = "房地产"
                return record

        db = FakeDB()
        handler = RealEstateHandler()
        runner = MultiPlatformRunner(db=db, handlers={"fake": handler}, ai_enabled=True, ai_mode="async")

        result = runner.crawl_platform("fake", limit=1)

        self.assertEqual(result.success_count, 1)
        queued = db.ai_enrichment_calls[0]
        self.assertEqual(queued["asset_group"], "real_estate")
        self.assertEqual(queued["context"]["asset_group"], "real_estate")

    def test_ai_enrichment_worker_fails_task_when_ai_returns_no_results(self):
        class QueueDB:
            def __init__(self):
                self.failed = []
                self.succeeded = []

            def fetch_ai_enrichment_tasks(self, limit, worker_id, task_types=None):
                return [
                    {
                        "ai_task_id": 1,
                        "source_platform": "fake",
                        "source_item_id": "item-1",
                        "asset_group": "debt",
                        "context_json": {"detail_text": "text", "asset_group": "debt"},
                    }
                ]

            def mark_ai_enrichment_task_failed(self, task_id, error):
                self.failed.append((task_id, str(error)))

            def mark_ai_enrichment_task_success(self, task_id, result_json):
                self.succeeded.append((task_id, result_json))

        class EmptyAIExtractor:
            def is_available(self):
                return True

            def batch_extract(self, fields, context):
                return {}

        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = EmptyAIExtractor()
        try:
            db = QueueDB()
            runner = MultiPlatformRunner(db=db, handlers={}, ai_enabled=True, ai_mode="sync")

            result = runner.process_ai_enrichment_queue(limit=1)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["success"], 0)
            self.assertEqual(db.succeeded, [])
            self.assertIn("returned no enrichment result", db.failed[0][1])
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_ai_enrichment_worker_marks_task_parsing_and_skips_paused_task(self):
        class QueueDB:
            def __init__(self):
                self.failed = []
                self.succeeded = []
                self.applied = []
                self.parsing_calls = []

            def fetch_ai_enrichment_tasks(self, limit, worker_id, task_types=None):
                return [
                    {
                        "ai_task_id": 1,
                        "source_platform": "fake",
                        "source_item_id": "item-paused",
                        "asset_group": "debt",
                        "context_json": {"detail_text": "paused", "asset_group": "debt"},
                    },
                    {
                        "ai_task_id": 2,
                        "source_platform": "fake",
                        "source_item_id": "item-ok",
                        "asset_group": "debt",
                        "context_json": {"detail_text": "ok", "asset_group": "debt"},
                    },
                ]

            def mark_ai_enrichment_task_parsing(self, task_id, **kwargs):
                self.parsing_calls.append((task_id, kwargs))
                return task_id == 2

            def apply_ai_enrichment_results(self, **kwargs):
                self.applied.append(kwargs)

            def mark_ai_enrichment_task_failed(self, task_id, error):
                self.failed.append((task_id, str(error)))

            def mark_ai_enrichment_task_success(self, task_id, result_json):
                self.succeeded.append((task_id, result_json))

        class SuccessAIExtractor:
            provider = "qwen"
            model_name = "qwen-plus"
            profile_name = "test-qwen"

            def __init__(self):
                self.calls = []

            def is_available(self):
                return True

            def batch_extract(self, fields, context):
                self.calls.append(context.paimai_id)
                return {
                    "asset_type": AIExtractionResult(
                        field_key="asset_type",
                        field_label="标的类型",
                        value="债权",
                        confidence=0.9,
                        original_text="债权",
                    )
                }

        original_extractor = getattr(jd_v2, "ai_extractor", None)
        extractor = SuccessAIExtractor()
        jd_v2.ai_extractor = extractor
        try:
            db = QueueDB()
            runner = MultiPlatformRunner(db=db, handlers={}, ai_enabled=True, ai_mode="sync")

            result = runner.process_ai_enrichment_queue(limit=2, worker_id="worker-a", concurrency=1)

            self.assertEqual(result["picked"], 2)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["success"], 1)
            self.assertEqual([call[0] for call in db.parsing_calls], [1, 2])
            self.assertEqual(db.parsing_calls[1][1]["profile_name"], "test-qwen")
            self.assertEqual(db.parsing_calls[1][1]["provider"], "qwen")
            self.assertEqual(db.parsing_calls[1][1]["model_name"], "qwen-plus")
            self.assertEqual(extractor.calls, ["fake:item-ok"])
            self.assertEqual([row[0] for row in db.succeeded], [2])
            self.assertEqual(db.failed, [])
            self.assertEqual(len(db.applied), 1)
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_ai_enrichment_worker_fails_task_when_ai_returns_only_error_values(self):
        class QueueDB:
            def __init__(self):
                self.failed = []
                self.succeeded = []
                self.applied = []

            def fetch_ai_enrichment_tasks(self, limit, worker_id, task_types=None):
                return [
                    {
                        "ai_task_id": 1,
                        "source_platform": "fake",
                        "source_item_id": "item-1",
                        "asset_group": "real_estate",
                        "context_json": {"detail_text": "text", "asset_group": "real_estate"},
                    }
                ]

            def apply_ai_enrichment_results(self, **kwargs):
                self.applied.append(kwargs)

            def mark_ai_enrichment_task_failed(self, task_id, error):
                self.failed.append((task_id, str(error)))

            def mark_ai_enrichment_task_success(self, task_id, result_json):
                self.succeeded.append((task_id, result_json))

        class ErrorOnlyAIExtractor:
            def is_available(self):
                return True

            def batch_extract(self, fields, context):
                return {
                    "asset_type": AIExtractionResult(
                        field_key="asset_type",
                        field_label="标的类型",
                        value=None,
                        confidence=0.0,
                        error="argument of type 'NoneType' is not iterable",
                    ),
                    "building_area": AIExtractionResult(
                        field_key="building_area",
                        field_label="建筑面积",
                        value=None,
                        confidence=0.0,
                        error="argument of type 'NoneType' is not iterable",
                    ),
                }

        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = ErrorOnlyAIExtractor()
        try:
            db = QueueDB()
            runner = MultiPlatformRunner(db=db, handlers={}, ai_enabled=True, ai_mode="sync")

            result = runner.process_ai_enrichment_queue(limit=1)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["success"], 0)
            self.assertEqual(db.succeeded, [])
            self.assertEqual(db.applied, [])
            self.assertIn("returned only empty/error enrichment values", db.failed[0][1])
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_ai_enrichment_worker_derives_single_ip_detail_from_special_fields(self):
        class QueueDB:
            def __init__(self):
                self.failed = []
                self.succeeded = []
                self.applied = []

            def fetch_ai_enrichment_tasks(self, limit, worker_id, task_types=None):
                return [
                    {
                        "ai_task_id": 1,
                        "source_platform": "fake",
                        "source_item_id": "ip-1",
                        "asset_group": "ip",
                        "context_json": {"detail_text": "single ip asset", "asset_group": "ip"},
                    }
                ]

            def apply_ai_enrichment_results(self, **kwargs):
                self.applied.append(kwargs)

            def mark_ai_enrichment_task_failed(self, task_id, error):
                self.failed.append((task_id, str(error)))

            def mark_ai_enrichment_task_success(self, task_id, result_json):
                self.succeeded.append((task_id, result_json))

        class SingleIPAIExtractor:
            def is_available(self):
                return True

            def batch_extract(self, fields, context):
                return {
                    "subject_name": AIExtractionResult(
                        field_key="subject_name",
                        field_label="标的名称",
                        value="测试软件著作权",
                        confidence=0.9,
                        original_text="作品名称：测试软件著作权",
                    ),
                    "certificate_no": AIExtractionResult(
                        field_key="certificate_no",
                        field_label="标的证号",
                        value="软著登字第001号",
                        confidence=0.9,
                        original_text="证书号：软著登字第001号",
                    ),
                    "ip_type": AIExtractionResult(
                        field_key="ip_type",
                        field_label="知产类型",
                        value="软件著作权",
                        confidence=0.9,
                        original_text="类型：软件著作权",
                    ),
                    "ip_details": AIExtractionResult(
                        field_key="ip_details",
                        field_label="知识产权逐项明细",
                        value=None,
                        confidence=0.0,
                        reasoning="单项标的，无表格明细",
                    ),
                }

        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = SingleIPAIExtractor()
        try:
            db = QueueDB()
            runner = MultiPlatformRunner(db=db, handlers={}, ai_enabled=True, ai_mode="sync")

            result = runner.process_ai_enrichment_queue(limit=1)

            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["success"], 1)
            self.assertEqual(len(db.applied), 1)
            details = db.applied[0]["ip_details"]
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["ip_name"], "测试软件著作权")
            self.assertEqual(details[0]["certificate_no"], "软著登字第001号")
            self.assertEqual(details[0]["ip_type"], "软件著作权")
        finally:
            jd_v2.ai_extractor = original_extractor

    def test_split_ai_results_ignores_attachment_json_without_verified_urls(self):
        common_values, common_results, special_values, special_results = split_ai_results(
            {
                "attachments_json": AIExtractionResult(
                    field_key="attachments_json",
                    field_label="附件材料",
                    value='[{"name":"受让申请书.doc","url":null}]',
                    confidence=0.8,
                    original_text="附件：受让申请书.doc",
                ),
                "contact_info": AIExtractionResult(
                    field_key="contact_info",
                    field_label="联系方式",
                    value="曹安琪 18970379959",
                    confidence=0.9,
                    original_text="联系人：曹安琪 联系电话：18970379959",
                ),
            },
            "debt",
        )

        self.assertNotIn("attachments_json", common_values)
        self.assertNotIn("attachments_json", common_results)
        self.assertEqual(common_values["contact_info"], "曹安琪 18970379959")
        self.assertEqual(special_values, {})
        self.assertEqual(special_results, {})

    def test_sync_ai_does_not_overwrite_structured_type_or_price(self):
        class NoisyAIExtractor:
            def is_available(self):
                return True

            def batch_extract(self, fields, context):
                return {
                    "asset_type": AIExtractionResult(
                        field_key="asset_type",
                        field_label="标的类型",
                        value="债权",
                        confidence=0.8,
                        original_text="页面侧边推荐出现债权",
                    ),
                    "start_price_raw": AIExtractionResult(
                        field_key="start_price_raw",
                        field_label="起拍价",
                        value="1元",
                        confidence=0.8,
                        original_text="无关推荐价格",
                    ),
                    "contact_info": AIExtractionResult(
                        field_key="contact_info",
                        field_label="联系方式",
                        value="李先生 13800000000",
                        confidence=0.9,
                        original_text="联系人：李先生 13800000000",
                    ),
                }

        record = PlatformRecord(
            source_platform="fake",
            source_site_name="Fake Platform",
            source_item_id="item-1",
            source_url="https://fake.test/item-1",
            asset_group="real_estate",
            common_values={"asset_type": "房地产", "start_price_raw": "100万元"},
            field_results={},
        )
        original_extractor = getattr(jd_v2, "ai_extractor", None)
        jd_v2.ai_extractor = NoisyAIExtractor()
        try:
            runner = MultiPlatformRunner(db=FakeDB(), handlers={}, ai_enabled=True)
            runner._apply_ai(record, FakeAIHandler(), {"id": "item-1"})
        finally:
            jd_v2.ai_extractor = original_extractor

        self.assertEqual(record.common_values["asset_type"], "房地产")
        self.assertEqual(record.common_values["start_price_raw"], "100万元")
        self.assertEqual(record.common_values["contact_info"], "李先生 13800000000")


if __name__ == "__main__":
    unittest.main()
