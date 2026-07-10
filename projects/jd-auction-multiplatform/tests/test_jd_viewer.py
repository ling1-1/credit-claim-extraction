import unittest

from jd_viewer import (
    apply_resource_summary_to_fields,
    render_debt_details,
    render_duplicates,
    render_ip_details,
    render_resources,
)


class ViewerDataTests(unittest.TestCase):
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

    def test_ip_detail_rows_are_rendered(self):
        html = render_ip_details(
            [
                {
                    "sequence_no": "1",
                    "ip_name": "测试软件",
                    "certificate_no": "软著登字第01号",
                    "ip_type": "软件著作权",
                    "application_date": "2024-05-10",
                    "status": "有效",
                }
            ]
        )

        self.assertIn("知识产权明细", html)
        self.assertIn("测试软件", html)
        self.assertIn("软著登字第01号", html)

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

    def test_duplicates_render_links_to_other_items(self):
        html = render_duplicates(
            [
                {
                    "source_platform": "jd",
                    "paimai_id": "3102",
                    "source_item_id": "3102",
                    "project_name": "测试房产1001室",
                    "asset_location": "苏州市工业园区",
                    "updated_at": "2026-07-08 10:00:00",
                }
            ]
        )

        self.assertIn("疑似重复资产", html)
        self.assertIn('href="/item/3102"', html)
        self.assertIn("测试房产1001室", html)


if __name__ == "__main__":
    unittest.main()
