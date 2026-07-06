import unittest

from platform_adapters.ejy365_adapter import Ejy365Adapter, Ejy365ListItem


LIST_HTML = """
<html>
  <body>
    <div class="jygg-list">
      <div class="jygg-item">
        <a class="title" href="/info/abc123">南京某银行债权资产包转让项目</a>
        <span>项目编号：N1543ZQ260016</span>
        <span>地区：江苏省 南京市</span>
        <span>挂牌价：6,000万元</span>
        <span>保证金：300万元</span>
        <span>状态：挂牌中</span>
        <span>报名截止：2026-07-15 17:00</span>
      </div>
    </div>
  </body>
</html>
"""


DETAIL_HTML = """
<html>
  <body>
    <h1>南京某银行债权资产包转让项目</h1>
    <table>
      <tr><th>项目编号</th><td>N1543ZQ260016</td><th>挂牌价格</th><td>6,000万元</td></tr>
      <tr><th>项目所在地</th><td>江苏省南京市</td><th>项目状态</th><td>挂牌中</td></tr>
      <tr><th>联系人</th><td>张三 025-12345678</td><th>保证金</th><td>300万元</td></tr>
    </table>
    <p>特别提示：受让方需自行核验债权真实性。</p>
    <a href="/uploads/debt-list.pdf">债权明细表.pdf</a>
  </body>
</html>
"""


class Ejy365AdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Ejy365Adapter()

    def test_parse_list_html_extracts_project_no_title_detail_url_and_price(self):
        items = self.adapter.parse_list_html(LIST_HTML)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.project_no, "N1543ZQ260016")
        self.assertEqual(item.title, "南京某银行债权资产包转让项目")
        self.assertEqual(item.slug, "abc123")
        self.assertEqual(item.detail_url, "https://www.ejy365.com/info/abc123")
        self.assertEqual(item.price_raw, "6,000万元")
        self.assertEqual(item.deposit_raw, "300万元")
        self.assertEqual(item.region, "江苏省 南京市")
        self.assertEqual(item.status, "挂牌中")
        self.assertEqual(item.signup_deadline, "2026-07-15 17:00")

    def test_parse_detail_html_extracts_key_values_attachments_and_auxiliary_json(self):
        auxiliary_json = {"data": [{"projectNo": "N1543ZQ260016", "offer": "6,000万元"}]}

        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url="https://www.ejy365.com/info/abc123",
            auxiliary_json=auxiliary_json,
        )

        self.assertEqual(bundle.source_item_id, "N1543ZQ260016")
        self.assertEqual(bundle.title, "南京某银行债权资产包转让项目")
        self.assertEqual(bundle.key_values["项目编号"], "N1543ZQ260016")
        self.assertEqual(bundle.key_values["挂牌价格"], "6,000万元")
        self.assertEqual(bundle.key_values["项目所在地"], "江苏省南京市")
        self.assertIn("特别提示", bundle.detail_text)
        self.assertEqual(bundle.auxiliary_json, auxiliary_json)
        self.assertEqual(
            bundle.attachments,
            [
                {
                    "name": "债权明细表.pdf",
                    "url": "https://www.ejy365.com/uploads/debt-list.pdf",
                    "source_payload_type": "detail_html",
                    "source_path": "a[href]",
                    "source_excerpt": "债权明细表.pdf",
                }
            ],
        )

    def test_masked_project_no_falls_back_to_detail_slug(self):
        list_item = Ejy365ListItem(
            title="masked item",
            detail_url="https://www.ejy365.com/info/masked123",
            slug="masked123",
            project_no="********",
        )
        bundle = self.adapter.parse_detail_html(
            "<html><body><h1>masked item</h1><table><tr><th>project no</th><td>********</td></tr></table></body></html>",
            url=list_item.detail_url,
            list_item=list_item,
            auxiliary_json={"data": [{"projectNo": "********"}]},
        )

        common = self.adapter.map_common_candidates(bundle)
        context = self.adapter.build_ai_context(bundle)

        self.assertEqual(bundle.source_item_id, "masked123")
        self.assertEqual(common["source_item_id"], "masked123")
        self.assertEqual(context.paimai_id, "ejy365:masked123")

    def test_map_common_candidates_uses_ejy365_debt_fields_without_current_price(self):
        list_item = self.adapter.parse_list_html(LIST_HTML)[0]
        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url=list_item.detail_url,
            list_item=list_item,
            auxiliary_json={"data": [{"projectNo": "N1543ZQ260016", "offer": "6,000万元"}]},
        )

        common = self.adapter.map_common_candidates(bundle)

        self.assertEqual(common["source_platform"], "ejy365")
        self.assertEqual(common["source_item_id"], "N1543ZQ260016")
        self.assertEqual(common["source_url"], "https://www.ejy365.com/info/abc123")
        self.assertEqual(common["asset_group"], "debt")
        self.assertEqual(common["asset_type"], "债权")
        self.assertEqual(common["project_name"], "南京某银行债权资产包转让项目")
        self.assertEqual(common["asset_location"], "江苏省南京市")
        self.assertEqual(common["project_status"], "挂牌中")
        self.assertIsNone(common["start_price_raw"])
        self.assertEqual(common["final_price_raw"], "6,000万元")
        self.assertEqual(common["price_basis"], "挂牌价")
        self.assertEqual(common["data_source"], "e交易")
        self.assertIn("债权明细表.pdf", common["attachments_json"])
        self.assertNotIn("current_price", common)
        self.assertNotIn("current_price_raw", common)
        self.assertNotIn("current_price_display", common)
        self.assertNotIn("current_price_amount", common)

        results = common["field_results"]
        self.assertEqual(results["source_platform"]["source_payload_type"], "computed")
        self.assertEqual(results["final_price_raw"]["source_payload_type"], "detail_html")
        self.assertEqual(results["final_price_raw"]["source_path"], "key_values.挂牌价格")
        self.assertIn("挂牌价格：6,000万元", results["final_price_raw"]["source_excerpt"])
        self.assertEqual(results["attachments_json"]["source_payload_type"], "detail_html")

    def test_build_ai_context_keeps_original_payloads_for_ai_extraction(self):
        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url="https://www.ejy365.com/info/abc123",
            auxiliary_json={"data": [{"projectNo": "N1543ZQ260016", "offer": "6,000万元"}]},
        )

        context = self.adapter.build_ai_context(bundle)

        self.assertEqual(context.asset_group, "debt")
        self.assertEqual(context.paimai_id, "ejy365:N1543ZQ260016")
        self.assertIn("source_platform: ejy365", context.detail_text)
        self.assertIn("南京某银行债权资产包转让项目", context.detail_text)
        self.assertEqual(context.html_key_values["项目编号"], "N1543ZQ260016")
        self.assertIn("auxiliary_json", context.detail_text)

    def test_project_type_code_drives_non_debt_asset_group_and_ai_context(self):
        html = """
        <html>
          <body>
            <h1>某商业房产转让项目</h1>
            <table>
              <tr><th>项目编号</th><td>N0101FC260059</td><th>挂牌价</th><td>250万元</td></tr>
              <tr><th>项目所在地</th><td>江苏省常州市</td><th>项目状态</th><td>挂牌中</td></tr>
            </table>
          </body>
        </html>
        """
        list_item = Ejy365ListItem(
            title="某商业房产转让项目",
            detail_url="https://www.ejy365.com/info/fc123",
            slug="fc123",
            project_no="N0101FC260059",
        )
        setattr(list_item, "project_type_code", "FC")
        bundle = self.adapter.parse_detail_html(html, url=list_item.detail_url, list_item=list_item)

        common = self.adapter.map_common_candidates(bundle)
        context = self.adapter.build_ai_context(bundle)

        self.assertEqual(common["asset_group"], "real_estate")
        self.assertEqual(common["asset_type"], "房地产")
        self.assertEqual(context.asset_group, "real_estate")


if __name__ == "__main__":
    unittest.main()
