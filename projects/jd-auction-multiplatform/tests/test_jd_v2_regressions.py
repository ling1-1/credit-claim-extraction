import datetime as dt
import unittest
from decimal import Decimal

import jd_mysql_store
import jd_scraper
import jd_scraper_v2 as scraper


class V2FormalStorageRegressionTests(unittest.TestCase):
    def test_formal_schema_uses_v2_tables_and_excludes_legacy_tables(self):
        schema = jd_mysql_store.V2_SCHEMA_PATH.read_text(encoding="utf-8")

        for table_name in (
            "auction_items",
            "field_catalog",
            "field_extractions",
            "item_resources",
            "crawl_batches",
            "crawl_queue",
            "asset_real_estate",
            "asset_debt_details",
            "asset_ip_details",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table_name}", schema)

        for legacy_name in ("auction_items_common", "field_comments", "crawl_queue_items"):
            self.assertNotIn(f"CREATE TABLE IF NOT EXISTS {legacy_name}", schema)
            self.assertNotIn(legacy_name, jd_mysql_store.mysql_table_names())

    def test_reset_table_list_drops_legacy_tables_without_creating_them(self):
        self.assertNotIn("auction_items_common", jd_mysql_store.mysql_table_names())
        self.assertNotIn("field_comments", jd_mysql_store.mysql_table_names())
        self.assertNotIn("crawl_queue_items", jd_mysql_store.mysql_table_names())

        reset_names = jd_mysql_store.mysql_reset_table_names()
        self.assertIn("auction_items_common", reset_names)
        self.assertIn("field_comments", reset_names)
        self.assertIn("crawl_queue_items", reset_names)

    def test_legacy_sqlite_writer_and_importer_are_not_exposed(self):
        self.assertFalse(hasattr(jd_scraper, "JDScraperDatabase"))
        self.assertFalse(hasattr(scraper, "JDScraperDatabase"))
        self.assertFalse(hasattr(jd_mysql_store, "import_sqlite_to_mysql"))

    def test_money_normalization_supports_chinese_units(self):
        self.assertEqual(scraper.money_numeric("100万元"), Decimal("1000000"))
        self.assertEqual(scraper.money_numeric("1.5亿元"), Decimal("150000000.0"))

    def test_typed_field_extraction_values_keep_typed_columns(self):
        money = scraper.typed_field_extraction_values("start_price_raw", "100万元")
        self.assertEqual(money["value_type"], "money")
        self.assertEqual(money["numeric_value"], "1000000.00")

        when = scraper.typed_field_extraction_values("signup_start_time", "2026年6月29日10时")
        self.assertEqual(when["value_type"], "datetime")
        self.assertEqual(when["datetime_value"], "2026-06-29 10:00:00")

    def test_project_status_prefers_terminal_codes_and_active_realtime(self):
        now = dt.datetime(2026, 6, 29, 10, 0, 0)
        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=1,
                auction_end_time="2026-06-29 11:00:00",
                realtime_active=True,
                now=now,
            ),
            "竞价中",
        )
        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=5,
                auction_end_time="2026-06-29 11:00:00",
                realtime_active=True,
                now=now,
            ),
            "已撤回",
        )

    def test_auction_stage_prefers_terminal_status_before_round_code(self):
        self.assertEqual(scraper.compute_auction_stage(1, 5), "撤拍")
        self.assertEqual(scraper.compute_auction_stage(2, 7), "终止")
        self.assertEqual(scraper.compute_auction_stage(1, 0), "一拍")
        self.assertEqual(scraper.compute_auction_stage(4, 0), "变卖")

    def test_dedup_hash_is_stable_for_normalized_same_asset_identity(self):
        first = scraper.compute_dedup_hash(
            "real_estate",
            {"project_name": "测试房产", "asset_location": "苏州市 工业园区 东湖大郡花园136幢1001室"},
            {"building_area": "137.22 平方米", "right_certificate_no": "00448472"},
        )
        second = scraper.compute_dedup_hash(
            "real_estate",
            {"project_name": "测试房产", "asset_location": "苏州市工业园区东湖大郡花园136幢1001室"},
            {"building_area": "137.22㎡", "right_certificate_no": "00448472"},
        )
        third = scraper.compute_dedup_hash(
            "real_estate",
            {"project_name": "测试房产", "asset_location": "苏州市工业园区东湖大郡花园136幢1002室"},
            {"building_area": "137.22㎡", "right_certificate_no": "00448472"},
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(first or ""), 16)


if __name__ == "__main__":
    unittest.main()
