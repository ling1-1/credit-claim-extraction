import sqlite3
import tempfile
import unittest
from pathlib import Path

from jd_scraper_v2 import JDScraperDatabase
from jd_viewer import (
    apply_resource_summary_to_fields,
    ensure_field_comments,
    get_item_detail,
    render_debt_details,
    render_duplicates,
    render_ip_details,
    render_resources,
)


class ViewerDataTests(unittest.TestCase):
    def test_comments_and_detail_include_common_and_special_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "viewer.sqlite"
            db = JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()
            db.upsert_raw_payloads(
                paimai_id="2001",
                batch_id="batch-1",
                source_url="https://paimai.jd.com/2001",
                list_json={"id": 2001},
                detail_json={"data": {"basicData": {"title": "债权测试项目"}}},
                realtime_json={},
                description_html="<p>主债务人名称：测试债务人</p>",
                attachments_json=[],
            )
            db.upsert_common_item(
                paimai_id="2001",
                batch_id="batch-1",
                asset_group="debt",
                jd_category_id="109",
                jd_category_name="债权",
                values={"project_name": "债权测试项目", "asset_type": "债权"},
                field_results={
                    "project_name": {
                        "value": "债权测试项目",
                        "status": "extracted",
                        "source_payload_type": "detail_json",
                    }
                },
            )
            db.upsert_special_item(
                paimai_id="2001",
                asset_group="debt",
                values={"debtor_name": "测试债务人", "creditor": "测试银行", "household_count": "1"},
                field_results={
                    "debtor_name": {
                        "value": "测试债务人",
                        "status": "extracted",
                        "source_payload_type": "description_html",
                    },
                    "creditor": {
                        "value": "测试银行",
                        "status": "extracted",
                        "source_payload_type": "description_html",
                    }
                },
            )
            db.upsert_debt_details(
                paimai_id="2001",
                details=[
                    {
                        "sequence_no": "1",
                        "debtor_name": "测试债务人",
                        "principal_balance": "100万元",
                        "guarantor": "测试保证人",
                    }
                ],
            )

            ensure_field_comments(db_path)
            detail = get_item_detail(db_path, "2001")

            self.assertEqual(detail["item"]["project_name"], "债权测试项目")
            self.assertTrue(any(field["label"] == "项目名称" for field in detail["common_fields"]))
            self.assertTrue(any(field["label"] == "债权人" for field in detail["special_fields"]))
            self.assertTrue(any(field["label"] == "主债务人名称" for field in detail["special_fields"]))
            self.assertFalse(any(field["key"] == "principal_balance" for field in detail["special_fields"]))
            self.assertEqual(detail["debt_details"][0]["debtor_name"], "测试债务人")
            self.assertTrue(
                all("comment" in field and field["comment"] for field in detail["common_fields"] + detail["special_fields"])
            )

            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM db_field_comments").fetchone()[0]
            finally:
                conn.close()
            self.assertGreater(count, 20)

    def test_debt_detail_table_hides_duplicate_benchmark_date_column(self):
        details = [
            {
                "sequence_no": "1",
                "debtor_name": "测试债务人",
                "principal_balance": "100万元",
                "benchmark_date": "2026-01-31",
            }
        ]

        html = render_debt_details(details, global_benchmark_date="2026-01-31")

        self.assertIn("测试债务人", html)
        self.assertNotIn("<th>基准日</th>", html)
        self.assertNotIn("<td>2026-01-31</td>", html)

    def test_debt_detail_table_shows_benchmark_date_when_detail_differs(self):
        details = [
            {
                "sequence_no": "1",
                "debtor_name": "测试债务人",
                "principal_balance": "100万元",
                "benchmark_date": "2026-02-28",
            }
        ]

        html = render_debt_details(details, global_benchmark_date="2026-01-31")

        self.assertIn("<th>基准日</th>", html)
        self.assertIn("<td>2026-02-28</td>", html)

    def test_ip_detail_rows_are_loaded_and_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "viewer-ip.sqlite"
            db = JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()
            db.upsert_raw_payloads(
                paimai_id="3001",
                batch_id="batch-ip",
                source_url="https://paimai.jd.com/3001",
                list_json={"id": 3001},
                detail_json={"data": {"basicData": {"title": "IP project"}}},
                realtime_json={},
                description_html="",
                attachments_json=[],
            )
            db.upsert_common_item(
                paimai_id="3001",
                batch_id="batch-ip",
                asset_group="ip",
                jd_category_id="116",
                jd_category_name="知识产权",
                values={"project_name": "IP project", "asset_type": "知识产权"},
                field_results={},
            )
            db.upsert_special_item(
                paimai_id="3001",
                asset_group="ip",
                values={"subject_name": "IP project", "ip_count": "1"},
                field_results={},
            )
            db.upsert_ip_details(
                paimai_id="3001",
                details=[
                    {
                        "sequence_no": "1",
                        "ip_name": "测试软件",
                        "certificate_no": "软著登字第001号",
                        "ip_type": "软件著作权",
                    }
                ],
            )

            detail = get_item_detail(db_path, "3001")
            html = render_ip_details(detail["ip_details"])

            self.assertEqual(detail["ip_details"][0]["ip_name"], "测试软件")
            self.assertIn("测试软件", html)
            self.assertIn("软著登字第001号", html)

    def test_resources_are_rendered_as_clickable_structured_table(self):
        html = render_resources(
            [
                {
                    "resource_type": "attachment",
                    "resource_role": "assessment_report",
                    "resource_name": "评估报告.pdf",
                    "resource_url": "https://storage.jd.com/report.pdf",
                    "source_section": "attachments_json",
                },
                {
                    "resource_type": "image",
                    "resource_role": "site_image",
                    "resource_name": "现场图片",
                    "resource_url": "https://img30.360buyimg.com/popWaterMark/jfs/t1/a.jpg",
                    "source_section": "imageVideoArea",
                },
            ]
        )

        self.assertIn("附件/图片/视频", html)
        self.assertIn("评估报告.pdf", html)
        self.assertIn("现场图片", html)
        self.assertIn('href="https://storage.jd.com/report.pdf"', html)
        self.assertIn('href="https://img30.360buyimg.com/popWaterMark/jfs/t1/a.jpg"', html)

    def test_attachment_field_uses_resource_summary_instead_of_raw_json(self):
        fields = [
            {
                "key": "attachments_json",
                "label": "attachments",
                "value": '{"files":[],"media":[{"imageVideoArea":{"imageList":[{"imagePath":"jfs/t1/a.jpg"}]}}]}',
                "source_excerpt": '{"files":[]}',
            }
        ]
        resources = [
            {"resource_type": "image", "resource_url": "https://img.example/a.jpg"},
            {"resource_type": "image", "resource_url": "https://img.example/b.jpg"},
            {"resource_type": "attachment", "resource_url": "https://file.example/report.pdf"},
        ]

        updated = apply_resource_summary_to_fields(fields, resources)

        self.assertEqual(updated[0]["value"], "附件 1 个；图片 2 个；详见下方“附件/图片/视频”")
        self.assertNotIn('"files":[]', updated[0]["value"])

    def test_normalized_comments_and_duplicate_hint_are_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "viewer-dup.sqlite"
            db = JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()

            for paimai_id, room in (("3101", "1001室"), ("3102", "1001室")):
                db.upsert_raw_payloads(
                    paimai_id=paimai_id,
                    batch_id="batch-dup",
                    source_url=f"https://paimai.jd.com/{paimai_id}",
                    list_json={},
                    detail_json={},
                    realtime_json={},
                    description_html="",
                    attachments_json=[],
                )
                db.upsert_common_item(
                    paimai_id=paimai_id,
                    batch_id="batch-dup",
                    asset_group="real_estate",
                    jd_category_id="101",
                    jd_category_name="住宅用房",
                    values={
                        "project_name": f"测试房产{room}",
                        "asset_type": "房地产",
                        "asset_location": f"苏州市工业园区东湖花园136幢{room}",
                    },
                    field_results={},
                    special_values={
                        "right_certificate_no": "00448472",
                        "building_area": "137.22平方米",
                        "property_location": f"苏州市工业园区东湖花园136幢{room}",
                    },
                )
                db.upsert_special_item(
                    paimai_id=paimai_id,
                    asset_group="real_estate",
                    values={
                        "right_certificate_no": "00448472",
                        "building_area": "137.22平方米",
                        "property_location": f"苏州市工业园区东湖花园136幢{room}",
                    },
                    field_results={},
                )

            ensure_field_comments(db_path)
            detail = get_item_detail(db_path, "3101")
            html = render_duplicates(detail["duplicates"])

            self.assertEqual(len(detail["duplicates"]), 1)
            self.assertIn("3102", html)

            conn = sqlite3.connect(db_path)
            try:
                comment = conn.execute(
                    """
                    SELECT comment FROM db_field_comments
                    WHERE table_name='asset_real_estate' AND column_name='building_area_sqm'
                    """
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(comment)
            self.assertIn("平方米", comment[0])


if __name__ == "__main__":
    unittest.main()
