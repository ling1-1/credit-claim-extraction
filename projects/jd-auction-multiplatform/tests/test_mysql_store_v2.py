from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from jd_mysql_store import (
    MySQLConfig,
    MySQLJDScraperDatabase,
    V2_DROP_TABLES,
    V2_SCHEMA_PATH,
    _debt_detail_money_decimal,
    build_v2_common_item_row,
    build_v2_resource_rows,
    build_v2_special_row,
    get_item_detail_mysql,
    get_items_mysql,
    mysql_reset_table_names,
    mysql_table_names,
    parse_source_item_ref,
)
from jd_scraper_v2 import datetime_to_db


class MySQLStoreV2MappingTests(unittest.TestCase):
    def test_parse_source_item_ref_accepts_platform_prefixed_ids(self):
        self.assertEqual(parse_source_item_ref("ejy365:N0109ZQ260027"), ("ejy365", "N0109ZQ260027"))
        self.assertEqual(parse_source_item_ref("310826968"), (None, "310826968"))

    def test_v2_common_row_uses_final_price_only_and_rejects_invalid_assessment_text(self):
        row = build_v2_common_item_row(
            paimai_id="310000001",
            batch_id="batch-v2",
            asset_group="real_estate",
            jd_category_id="102",
            jd_category_name="房地产",
            values={
                "asset_type": "房地产",
                "asset_location": "江苏无锡市滨湖区建筑路672-674号",
                "project_status": "竞价中",
                "auction_stage": "一拍",
                "bid_records_json": "[]",
                "data_source": "京东拍卖",
                "project_name": "无锡市滨湖区建筑路672-674号（1F）三年租赁权",
                "signup_start_time": "2026-06-29 19:30:00",
                "signup_end_time": "2026-06-30 19:30:00",
                "disposal_party": "无锡市锦绣梁溪产业发展有限公司",
                "start_price_raw": "43.2万元",
                "final_price_raw": "432,000.00 元",
                "contact_info": "联系人：张三 13800000000",
                "assessment_price_time": "市场价2",
            },
            special_values={"right_holder": "权利人甲", "disclosed_defects": "存在租赁风险"},
        )

        self.assertEqual(row["source_platform"], "jd")
        self.assertEqual(row["source_item_id"], "310000001")
        self.assertEqual(row["source_category_id"], "102")
        self.assertEqual(row["start_price_amount"], Decimal("432000.00"))
        self.assertEqual(row["final_price_amount"], Decimal("432000.00"))
        self.assertEqual(row["final_price_display"], "432,000.00 元")
        self.assertIsNone(row["assessment_price_amount"])
        self.assertIsNone(row["assessment_price_display"])
        self.assertIsNone(row["assessment_date"])
        self.assertEqual(row["right_holder"], "权利人甲")
        self.assertEqual(row["disclosed_defects"], "存在租赁风险")
        self.assertFalse(any(key.startswith("current_price") for key in row))

    def test_v2_common_row_keeps_plain_assessment_amount_without_fake_date(self):
        row = build_v2_common_item_row(
            paimai_id="310700740",
            batch_id="batch-v2",
            asset_group="real_estate",
            jd_category_id="102",
            jd_category_name="商业用房",
            values={
                "asset_type": "房地产",
                "asset_location": "广东肇庆市德庆县龙母大街",
                "project_status": "竞价中",
                "auction_stage": "变卖",
                "bid_records_json": "[]",
                "data_source": "京东拍卖",
                "project_name": "德庆县龙母大街(龙湖东岸)聚龙湖畔地下室 158号摩托车位",
                "signup_start_time": "2026-06-05 10:00:00",
                "signup_end_time": "2026-08-04 10:00:00",
                "disposal_party": "德庆县人民法院",
                "start_price_raw": "5,000.00 元",
                "final_price_raw": "5,000.00 元",
                "assessment_price_time": "8100",
            },
            field_results={
                "assessment_price_time": {
                    "source_payload_type": "list_json",
                    "source_path": "assessmentPriceCN",
                    "source_excerpt": "8100",
                }
            },
            special_values={},
        )

        self.assertEqual(row["assessment_price_amount"], Decimal("8100.00"))
        self.assertEqual(row["assessment_price_display"], "8100")
        self.assertIsNone(row["assessment_date"])

    def test_v2_common_row_accepts_non_jd_platform_identity(self):
        row = build_v2_common_item_row(
            paimai_id="ZQ-1001",
            batch_id="batch-v2",
            asset_group="debt",
            jd_category_id="ZQ",
            jd_category_name="债权",
            values={
                "source_platform": "ejy365",
                "source_item_id": "ZQ-1001",
                "source_url": "https://www.ejy365.com/info/ZQ-1001",
                "source_site_name": "e交易",
                "asset_type": "债权",
                "asset_location": "江苏南京",
                "project_name": "南京某债权转让项目",
                "start_price_raw": "100万元",
                "final_price_raw": "100万元",
                "data_source": "e交易",
            },
            special_values={},
        )

        self.assertEqual(row["source_platform"], "ejy365")
        self.assertEqual(row["source_item_id"], "ZQ-1001")
        self.assertEqual(row["source_url"], "https://www.ejy365.com/info/ZQ-1001")
        self.assertEqual(row["source_site_name"], "e交易")
        self.assertEqual(row["source_category_id"], "ZQ")

    def test_v2_common_row_falls_back_final_price_to_start_price_when_final_missing(self):
        row = build_v2_common_item_row(
            paimai_id="N0101GQ260028",
            batch_id="batch-v2",
            asset_group="equity",
            jd_category_id="GQ",
            jd_category_name="股权",
            values={
                "source_platform": "ejy365",
                "source_item_id": "N0101GQ260028",
                "source_url": "https://www.ejy365.com/info/N0101GQ260028",
                "source_site_name": "e交易",
                "asset_type": "股权",
                "asset_location": "江苏江阴",
                "project_name": "江苏某股份公司股权项目",
                "start_price_raw": "24,000,000元",
                "data_source": "e交易",
            },
            special_values={},
        )

        self.assertEqual(row["start_price_display"], "24,000,000元")
        self.assertEqual(row["final_price_display"], "24,000,000元")
        self.assertEqual(row["final_price_amount"], Decimal("24000000.00"))
        self.assertEqual(row["price_basis"], "start_price_fallback")


    def test_ai_common_update_row_rejects_non_price_final_display(self):
        db = MySQLJDScraperDatabase(MySQLConfig(database="unused"))

        row = db._ai_common_update_row(
            {"final_price_raw": "保证金（元）"},
            {},
            {},
            "vehicle",
            existing_item={"final_price_display": "2,600元/辆"},
        )

        self.assertNotIn("final_price_display", row)
        self.assertNotIn("final_price_amount", row)


    def test_v2_resource_rows_split_attachments_and_media(self):
        rows = build_v2_resource_rows(
            item_id=12,
            attachments_json={
                "files": [
                    {
                        "attachmentName": "评估报告.pdf",
                        "attachmentAddress": "https://storage.jd.com/report.pdf",
                        "attachmentFormat": "pdf",
                        "attachmentSize": "100",
                    }
                ],
                "media": [
                    {"imageVideoArea": {"imageList": [{"imagePath": "jfs/t1/example.jpg"}]}},
                    {"videoPath": "https://example.com/intro.mp4"},
                ],
            },
        )

        roles = {(row["resource_type"], row["resource_role"]) for row in rows}
        urls = {row["resource_url"] for row in rows}

        self.assertIn(("attachment", "assessment_report"), roles)
        self.assertIn(("image", "site_image"), roles)
        self.assertIn(("video", "site_video"), roles)
        self.assertIn("https://storage.jd.com/report.pdf", urls)
        self.assertIn("https://img30.360buyimg.com/popWaterMark/jfs/t1/example.jpg", urls)
        self.assertIn("https://example.com/intro.mp4", urls)


    def test_v2_schema_includes_async_ocr_retry_queue(self):
        schema = V2_SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS ocr_retry_queue", schema)
        self.assertIn("resource_urls_json JSON", schema)
        self.assertIn("ocr_retry_queue", V2_DROP_TABLES)

    def test_v2_schema_includes_async_ai_enrichment_queue(self):
        schema = V2_SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS ai_enrichment_queue", schema)
        self.assertIn("context_json JSON", schema)
        self.assertIn("pending/running/parsing/paused/success/failed/skipped", schema)
        self.assertIn("running_profile_name VARCHAR(100)", schema)
        self.assertIn("running_provider VARCHAR(80)", schema)
        self.assertIn("running_model_name VARCHAR(200)", schema)
        self.assertIn("ai_enrichment_queue", V2_DROP_TABLES)

    def test_v2_schema_includes_resume_and_dead_letter_tables(self):
        schema = V2_SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS crawl_checkpoints", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS crawl_queue_events", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS dead_letter_queue", schema)
        self.assertIn("crawl_checkpoints", V2_DROP_TABLES)
        self.assertIn("crawl_queue_events", V2_DROP_TABLES)
        self.assertIn("dead_letter_queue", V2_DROP_TABLES)

    def test_formal_v2_schema_does_not_create_legacy_tables(self):
        schema = V2_SCHEMA_PATH.read_text(encoding="utf-8")

        self.assertNotIn("CREATE TABLE IF NOT EXISTS auction_items_common", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS field_comments", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS crawl_queue_items", schema)
        self.assertNotIn("auction_items_common", mysql_table_names())
        self.assertNotIn("field_comments", mysql_table_names())
        self.assertNotIn("crawl_queue_items", mysql_table_names())
        self.assertIn("auction_items_common", mysql_reset_table_names())
        self.assertIn("field_comments", mysql_reset_table_names())
        self.assertIn("crawl_queue_items", mysql_reset_table_names())

    def test_mysql_store_legacy_entrypoints_are_not_public(self):
        import jd_mysql_store

        self.assertFalse(hasattr(jd_mysql_store, "MYSQL_SCHEMA"))
        self.assertFalse(hasattr(jd_mysql_store, "ensure_legacy_mysql_schema"))
        self.assertFalse(hasattr(jd_mysql_store, "import_sqlite_to_mysql"))
        self.assertFalse(hasattr(jd_mysql_store, "clean_invalid_assessment_rows"))
        source = Path("jd_mysql_store.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("def get_items_mysql("), 1)
        self.assertEqual(source.count("def get_item_detail_mysql("), 1)

    def test_web_admin_crawl_queue_router_does_not_reference_legacy_queue_table(self):
        router_source = Path("web_admin/routers/queues.py").read_text(encoding="utf-8")

        self.assertNotIn("crawl_queue_items", router_source)


    def test_v2_special_rows_use_item_id_and_do_not_repeat_common_risk_fields(self):
        estate = build_v2_special_row(
            item_id=21,
            asset_group="real_estate",
            values={
                "right_certificate_no": "苏（2020）不动产权第001号",
                "building_area": "137.22平方米",
                "property_use": "商业",
                "disclosed_defects": "应进入主表",
                "site_images": "[...]",
            },
        )
        debt = build_v2_special_row(
            item_id=22,
            asset_group="debt",
            values={
                "debtor_name": "测试债务人",
                "principal_balance": "100万元",
                "interest_balance": "20万元",
                "claim_total": "120万元",
                "benchmark_date": "2025年6月20日",
            },
        )

        self.assertEqual(estate["item_id"], 21)
        self.assertEqual(estate["building_area_sqm"], Decimal("137.220000"))
        self.assertNotIn("disclosed_defects", estate)
        self.assertNotIn("site_images", estate)

        self.assertEqual(debt["item_id"], 22)
        self.assertEqual(debt["main_debtor_name"], "测试债务人")
        self.assertEqual(debt["principal_balance_amount"], Decimal("1000000.00"))
        self.assertEqual(debt["principal_balance_display"], "100万元")
        self.assertEqual(debt["interest_balance_amount"], Decimal("200000.00"))
        self.assertEqual(debt["claim_total_amount"], Decimal("1200000.00"))
        self.assertEqual(debt["benchmark_date"], "2025-06-20")

    def test_ai_common_update_backfills_final_price_from_start_only_when_existing_final_empty(self):
        store = MySQLJDScraperDatabase(MySQLConfig())

        row = store._ai_common_update_row(
            {"start_price_raw": "21,343,145.44元"},
            {},
            {},
            "debt",
            existing_item={"final_price_display": None},
        )

        self.assertEqual(row["start_price_display"], "21,343,145.44元")
        self.assertEqual(row["final_price_display"], "21,343,145.44元")
        self.assertEqual(row["final_price_amount"], Decimal("21343145.44"))

        row_with_existing_final = store._ai_common_update_row(
            {"start_price_raw": "100万元"},
            {},
            {},
            "debt",
            existing_item={"final_price_display": "120万元"},
        )

        self.assertNotIn("final_price_display", row_with_existing_final)

    def test_debt_detail_money_uses_table_unit_when_cell_has_no_unit(self):
        detail = {
            "principal_balance": "117.05",
            "interest_balance": "3.2",
            "source_excerpt": "债权明细表，单位：万元",
        }

        self.assertEqual(_debt_detail_money_decimal(detail["principal_balance"], detail), Decimal("1170500.00"))
        self.assertEqual(_debt_detail_money_decimal(detail["interest_balance"], detail), Decimal("32000.00"))

    def test_debt_detail_money_does_not_double_multiply_explicit_unit(self):
        detail = {
            "principal_balance": "117.05万元",
            "source_excerpt": "债权明细表，单位：万元",
        }

        self.assertEqual(_debt_detail_money_decimal(detail["principal_balance"], detail), Decimal("1170500.00"))

    def test_debt_detail_money_does_not_double_multiply_real_unicode_unit(self):
        detail = {
            "principal_balance": "1000.000000万元",
            "source_excerpt": "债权明细表，单位：万元",
        }

        self.assertEqual(_debt_detail_money_decimal(detail["principal_balance"], detail), Decimal("10000000.00"))

    def test_datetime_to_db_preserves_iso_t_time(self):
        self.assertEqual(datetime_to_db("2026-07-06T10:00:00"), "2026-07-06 10:00:00")
        self.assertEqual(datetime_to_db("2026-07-06T10:00"), "2026-07-06 10:00:00")

    def test_mysql_detail_includes_structured_item_resources(self):
        class FakeCursor:
            def __init__(self):
                self.result = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                if "FROM auction_items c" in sql:
                    self.result = {
                        "item_id": 88,
                        "source_platform": "jd",
                        "source_item_id": "310000088",
                        "asset_group": "real_estate",
                        "asset_group_label": "房地产",
                        "source_category_id": "102",
                        "source_category_name": "房地产",
                        "project_name": "测试房产",
                        "dedup_hash": None,
                        "start_price_raw": "10.00 元",
                        "final_price_raw": "10.00 元",
                    }
                elif "FROM `asset_real_estate`" in sql:
                    self.result = {}
                elif "FROM item_resources" in sql:
                    self.result = [
                        {
                            "resource_id": 1,
                            "item_id": 88,
                            "resource_type": "attachment",
                            "resource_role": "assessment_report",
                            "resource_name": "评估报告.pdf",
                            "resource_url": "https://storage.jd.com/report.pdf",
                            "source_section": "attachments_json",
                        },
                        {
                            "resource_id": 2,
                            "item_id": 88,
                            "resource_type": "image",
                            "resource_role": "site_image",
                            "resource_name": "现场图片",
                            "resource_url": "https://img30.360buyimg.com/popWaterMark/jfs/t1/a.jpg",
                            "source_section": "imageVideoArea",
                        },
                    ]
                elif "FROM raw_payloads" in sql:
                    self.result = []
                elif "information_schema.COLUMNS" in sql:
                    self.result = []
                elif "FROM (" in sql and "field_extractions" in sql:
                    self.result = []
                else:
                    raise AssertionError(f"Unexpected SQL: {sql}")

            def fetchone(self):
                return self.result

            def fetchall(self):
                return self.result

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return FakeCursor()

        with patch("jd_mysql_store.mysql_connection", return_value=FakeConnection()):
            detail = get_item_detail_mysql(MySQLConfig(), "310000088")

        self.assertEqual(len(detail["resources"]), 2)
        self.assertEqual(detail["resources"][0]["resource_type"], "attachment")
        self.assertEqual(detail["resources"][1]["resource_role"], "site_image")

    def test_mysql_list_filters_by_source_platform(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []
                self.result = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, tuple(params or ())))
                if "FROM auction_items c" in sql:
                    self.result = [
                        {
                            "paimai_id": "jd:310000001",
                            "source_platform": "jd",
                            "source_site_name": "京东拍卖",
                            "source_item_id": "310000001",
                            "asset_group": "real_estate",
                            "asset_group_label": "房地产",
                            "jd_category_id": "102",
                            "jd_category_name": "商业用房",
                            "project_name": "test",
                            "asset_location": "loc",
                            "project_status": "进行中",
                            "start_price_raw": "1.00 元",
                            "final_price_raw": "1.00 元",
                            "disposal_party": "party",
                            "total_fields": 1,
                            "extracted_fields": 1,
                            "issue_fields": 0,
                        }
                    ]
                elif "GROUP BY asset_group" in sql:
                    self.result = [{"asset_group": "real_estate", "asset_group_label": "房地产", "count": 1}]
                elif "SELECT DISTINCT project_status" in sql:
                    self.result = [{"project_status": "进行中"}]
                elif "GROUP BY source_platform" in sql:
                    self.result = [{"source_platform": "jd", "source_site_name": "京东拍卖", "count": 1}]
                else:
                    raise AssertionError(f"Unexpected SQL: {sql}")

            def fetchall(self):
                return self.result

        class FakeConnection:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self.cursor_obj

        fake_conn = FakeConnection()
        with patch("jd_mysql_store.mysql_connection", return_value=fake_conn):
            data = get_items_mysql(MySQLConfig(), {"source_platform": "jd"})

        main_sql, main_params = fake_conn.cursor_obj.calls[0]
        self.assertIn("c.source_platform = %s", main_sql)
        self.assertEqual(main_params, ("jd",))
        self.assertEqual(data["source_platforms"][0]["source_platform"], "jd")


if __name__ == "__main__":
    unittest.main()
