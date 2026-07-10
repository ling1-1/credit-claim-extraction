import unittest

import jd_scraper
from jd_scraper import (
    COMMON_FIELDS,
    SPECIAL_FIELDS,
    JDCategory,
    classify_category,
    extract_common_values,
    extract_key_values_from_html,
    extract_special_values,
    join_address,
    parse_debt_package_details,
)


class SchemaAndExtractionTests(unittest.TestCase):
    def test_html_parser_reads_tables_and_colon_paragraphs(self):
        html = """
        <table>
          <tr><td>权证编号</td><td>ABC-001</td><td>土地面积</td><td>120㎡</td></tr>
        </table>
        <p>主债务人名称：某某公司</p>
        <p>担保方式: 抵押、保证</p>
        """

        parsed = extract_key_values_from_html(html)

        self.assertEqual(parsed.key_values["权证编号"], "ABC-001")
        self.assertEqual(parsed.key_values["土地面积"], "120㎡")
        self.assertEqual(parsed.key_values["主债务人名称"], "某某公司")
        self.assertEqual(parsed.key_values["担保方式"], "抵押、保证")

    def test_html_parser_does_not_treat_table_headers_as_values(self):
        html = """
        <table>
          <tr><td>序号</td><td>借款人</td><td>担保人</td><td>担保物</td></tr>
          <tr><td>本金余额</td><td>利息金额（含罚息、复利）</td><td>实现债权费用金额</td><td>债权合计</td></tr>
          <tr><td>1</td><td>上海测试公司</td><td>保证人：张三</td><td>无</td><td>100.00</td><td>20.00</td><td>0</td><td>120.00</td></tr>
        </table>
        """

        parsed = extract_key_values_from_html(html)

        self.assertNotIn("本金余额", parsed.key_values)
        self.assertEqual(len(parse_debt_package_details(parsed)), 1)

    def test_common_location_prefers_full_structured_address_and_media_merges_into_attachments(self):
        parsed = extract_key_values_from_html("")
        values, _ = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={"productAddress": "龙华街道160街坊", "province": "上海", "city": "上海市"},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试项目",
                            "productAddressResult": {
                                "province": "上海",
                                "city": "上海市",
                                "county": "徐汇区",
                                "address": "龙华街道160街坊",
                            },
                        },
                        "imageVideoArea": {"imageList": [{"imagePath": "jfs/test.jpg"}]},
                    }
                },
                "realtime": {"data": {}},
                "attachments": [{"attachmentName": "附件.pdf"}],
                "notice_html": "",
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertEqual(values["asset_location"], "上海市徐汇区龙华街道160街坊")
        self.assertNotIn("media_json", [field.key for field in COMMON_FIELDS])
        self.assertIn("media", values["attachments_json"])

    def test_join_address_keeps_city_and_removes_duplicate_prefix(self):
        address = join_address(
            {
                "province": "\u6e56\u5317",
                "city": "\u6b66\u6c49\u5e02",
                "county": "\u6b66\u660c\u533a",
                "address": "\u6b66\u6c49\u5e02\u6b66\u660c\u533a\u5f90\u5bb6\u68da\u8857\u9053160\u8857\u574a",
            }
        )

        self.assertEqual(address, "\u6e56\u5317\u6b66\u6c49\u5e02\u6b66\u660c\u533a\u5f90\u5bb6\u68da\u8857\u9053160\u8857\u574a")

    def test_join_address_handles_municipality_with_district_in_city_slot(self):
        address = join_address(
            {
                "province": "\u4e0a\u6d77",
                "city": "\u5f90\u6c47\u533a",
                "county": "\u9f99\u534e\u8857\u9053",
                "address": "160\u8857\u574a",
            }
        )

        self.assertEqual(address, "\u4e0a\u6d77\u5e02\u5f90\u6c47\u533a\u9f99\u534e\u8857\u9053160\u8857\u574a")

    def test_debt_package_details_drive_aggregate_fields(self):
        html = """
        <table>
          <tr><td>序号</td><td>借款人</td><td>担保人</td><td>担保物</td><td>基准日：2025年6月20日 单位：人民币元</td></tr>
          <tr><td>本金余额</td><td>利息金额（含罚息、复利、迟延履行金）</td><td>实现债权费用金额</td><td>债权合计</td></tr>
          <tr><td>1</td><td>上海测试公司</td><td>保证人：张三</td><td>抵押物：房产</td><td>100.00</td><td>20.00</td><td>5.00</td><td>125.00</td></tr>
          <tr><td>2</td><td>江苏测试公司</td><td>保证人：李四</td><td>无</td><td>300.00</td><td>40.00</td><td>0</td><td>340.00</td></tr>
        </table>
        <p>特别提示：请投资人自行判断。</p>
        """
        parsed = extract_key_values_from_html(html)

        values, _, details = extract_special_values(
            asset_group="debt",
            parsed=parsed,
            notice_parsed=parsed,
            core={},
            paimai_id="test-id",
        )

        self.assertEqual(values["household_count"], "2")
        self.assertEqual(len(details), 2)
        self.assertIn("上海测试公司", values["debtor_name"])
        self.assertEqual(details[0]["principal_balance"], "100.00")
        self.assertEqual(details[1]["principal_balance"], "300.00")
        self.assertEqual(details[0]["interest_balance"], "20.00")
        self.assertEqual(details[1]["interest_balance"], "40.00")
        self.assertEqual(values["benchmark_date"], "2025年6月20日")
        self.assertIn("特别提示", values["disclosed_defects"])

    def test_category_classification_uses_jd_first_level_category(self):
        self.assertEqual(classify_category(JDCategory("109", "债权")), "debt")
        self.assertEqual(classify_category(JDCategory("112", "土地")), "land")
        self.assertEqual(classify_category(JDCategory("123", "其他财产")), "other")

    def test_legacy_sqlite_database_class_is_not_exposed(self):
        self.assertFalse(hasattr(jd_scraper, "JDScraperDatabase"))

if __name__ == "__main__":
    unittest.main()
