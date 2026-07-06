import json
import unittest
from urllib.parse import parse_qs, urlparse

from platform_adapters.cquae_adapter import CquaeAdapter, classify_asset_group


LIST_HTML = """
<html>
  <body>
    <table class="project-list">
      <tr>
        <th>项目编号</th>
        <th>项目名称</th>
        <th>项目类型</th>
        <th>项目状态</th>
        <th>挂牌价</th>
        <th>保证金</th>
        <th>披露起止日期</th>
        <th>联系人</th>
      </tr>
      <tr>
        <td>CQ20260701001</td>
        <td><a href="/Project/Show?id=12345">渝北区某房产转让</a></td>
        <td>房产</td>
        <td>正式披露</td>
        <td>120.00 万元</td>
        <td>10.00 万元</td>
        <td>2026-07-01 至 2026-07-28</td>
        <td>张老师 023-63600000</td>
      </tr>
    </table>
  </body>
</html>
"""


DETAIL_HTML = """
<html>
  <body>
    <h1>渝北区某房产转让</h1>
    <table class="detail-table">
      <tr>
        <td>项目编号</td><td>CQ20260701001</td>
        <td>项目名称</td><td>渝北区某房产转让</td>
      </tr>
      <tr>
        <td>转让底价</td><td>120.00 万元</td>
        <td>披露起止日期</td><td>2026-07-01 至 2026-07-28</td>
      </tr>
      <tr>
        <td>联系人</td><td>张老师</td>
        <td>联系电话</td><td>023-63600000</td>
      </tr>
      <tr>
        <td>标的所在地</td><td>重庆市渝北区</td>
        <td>项目状态</td><td>正式披露</td>
      </tr>
      <tr>
        <td>重要信息披露</td><td>以现场踏勘为准</td>
      </tr>
    </table>
    <p>页面正文：本项目资产位置重庆市渝北区。</p>
    <img src="/upload/site-image-1.jpg" />
    <img data-src="https://img.example.test/site-image-2.jpg" />
    <a href="/FileDownLoad/dw.ashx?infoid=abc&categorynum=002">产权交易合同.pdf</a>
  </body>
</html>
"""


class CquaeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CquaeAdapter()

    def test_build_list_url_uses_expected_parameters(self):
        url = self.adapter.build_list_url(page=3, project_id=2, nt=3, price_id=32, type_id=7)

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.cquae.com")
        self.assertEqual(parsed.path, "/Project")
        self.assertEqual(query["q"], ["s"])
        self.assertEqual(query["projectID"], ["2"])
        self.assertEqual(query["nt"], ["3"])
        self.assertEqual(query["priceID"], ["32"])
        self.assertEqual(query["type"], ["7"])
        self.assertEqual(query["page"], ["3"])

    def test_waf_521_html_is_detected(self):
        waf_html = """
        <html><head><title>521</title></head>
        <body><script>document.cookie='__jsl_clearance_s=abc';</script>Knownsec</body></html>
        """

        self.assertTrue(self.adapter.is_waf_challenge(waf_html, status_code=521))
        self.assertFalse(self.adapter.is_waf_challenge("<html>正常页面</html>", status_code=200))

    def test_parse_list_html_extracts_stable_fields(self):
        items = self.adapter.parse_list_html(LIST_HTML)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source_item_id, "12345")
        self.assertEqual(item.source_url, "https://www.cquae.com/Project/Show?id=12345")
        self.assertEqual(item.title, "渝北区某房产转让")
        self.assertEqual(item.project_type, "房产")
        self.assertEqual(item.project_status, "正式披露")
        self.assertEqual(item.price_raw, "120.00 万元")
        self.assertEqual(item.deposit_raw, "10.00 万元")
        self.assertEqual(item.date_text, "2026-07-01 至 2026-07-28")
        self.assertEqual(item.contact_info, "张老师 023-63600000")

    def test_parse_detail_html_extracts_table_values_and_attachments(self):
        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=12345",
        )

        self.assertEqual(bundle.source_item_id, "12345")
        self.assertEqual(bundle.title, "渝北区某房产转让")
        self.assertEqual(bundle.key_values["项目编号"], "CQ20260701001")
        self.assertEqual(bundle.key_values["转让底价"], "120.00 万元")
        self.assertEqual(bundle.key_values["披露起止日期"], "2026-07-01 至 2026-07-28")
        self.assertEqual(bundle.key_values["联系人"], "张老师")
        self.assertEqual(bundle.key_values["联系电话"], "023-63600000")
        self.assertIn("页面正文", bundle.detail_text)
        self.assertEqual(
            bundle.image_urls,
            [
                "https://www.cquae.com/upload/site-image-1.jpg",
                "https://img.example.test/site-image-2.jpg",
            ],
        )
        self.assertEqual(
            bundle.attachments,
            [
                {
                    "name": "产权交易合同.pdf",
                    "url": "https://www.cquae.com/FileDownLoad/dw.ashx?infoid=abc&categorynum=002",
                }
            ],
        )

    def test_parse_detail_html_keeps_sdcqjy_attachment_links(self):
        html = """
        <html><body>
          <table><tr><td>项目名称</td><td>齐鲁银行5户债权资产包</td></tr></table>
          <a href="/attachment/noauthorizefiles/trade-manager/齐鲁银行5户债权资产包.docx">查看附件材料</a>
          <a href="/attachment/noauthorizefiles/website-manager/承诺函.docx">承诺函</a>
        </body></html>
        """

        bundle = self.adapter.parse_detail_html(
            html,
            url="http://www.sdcqjy.com/proj/tc/d94d09f1db7b4a8f8b1a60668b60dfe8",
        )

        self.assertEqual(len(bundle.attachments), 2)
        self.assertEqual(bundle.attachments[0]["name"], "查看附件材料")
        self.assertIn("/attachment/noauthorizefiles/trade-manager/", bundle.attachments[0]["url"])
        self.assertEqual(bundle.attachments[1]["name"], "承诺函")

    def test_asset_type_ignores_company_entity_type(self):
        html = """
        <html><body>
          <h1>齐鲁银行5户债权资产包</h1>
          <table>
            <tr><td>项目名称</td><td>齐鲁银行5户债权资产包</td></tr>
            <tr><td>企业类型</td><td>有限责任公司</td></tr>
            <tr><td>转让底价</td><td>12127.66万元</td></tr>
          </table>
        </body></html>
        """

        bundle = self.adapter.parse_detail_html(html, url="https://www.cquae.com/Project/Show?id=abc")
        common = self.adapter.map_common_candidates(bundle)

        self.assertEqual(common["asset_group"], "debt")
        self.assertNotEqual(common["asset_type"], "有限责任公司")

    def test_sdcqjy_debt_title_beats_noisy_lease_and_company_text(self):
        html = """
        <html><body>
          <h1>齐鲁银行5户债权资产包</h1>
          <table>
            <tr><td>标的名称</td><td>齐鲁银行5户债权资产包</td></tr>
            <tr><td>企业类型</td><td>股份有限公司</td></tr>
            <tr><td>重大事项及其他披露内容</td><td>抵押房产所在商场供配电设施由案外人控制，转供电价约为市场价2倍，显著影响招租及运营成本。</td></tr>
            <tr><td>转让底价</td><td>12,127.66万元</td></tr>
          </table>
          <a href="/attachment/noauthorizefiles/trade-manager/debt-list.docx">查看附件材料</a>
          <a href="/attachment/noauthorizefiles/website-manager/promise.docx">承诺函</a>
        </body></html>
        """

        bundle = self.adapter.parse_detail_html(
            html,
            url="http://www.sdcqjy.com/proj/tc/d94d09f1db7b4a8f8b1a60668b60dfe8",
        )
        common = self.adapter.map_common_candidates(bundle)

        self.assertEqual(common["asset_group"], "debt")
        self.assertEqual(common["asset_type"], "债权")
        self.assertEqual(len(bundle.attachments), 2)
        self.assertIn("/attachment/noauthorizefiles/trade-manager/debt-list.docx", bundle.attachments[0]["url"])

    def test_common_mapping_uses_cquae_source_and_omits_current_price_fields(self):
        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=12345",
        )

        common = self.adapter.map_common_candidates(bundle)

        self.assertEqual(common["source_platform"], "cquae")
        self.assertEqual(common["source_item_id"], "12345")
        self.assertEqual(common["source_url"], "https://www.cquae.com/Project/Show?id=12345")
        self.assertEqual(common["asset_group"], "real_estate")
        self.assertEqual(common["asset_type"], "房产")
        self.assertEqual(common["project_name"], "渝北区某房产转让")
        self.assertEqual(common["asset_location"], "重庆市渝北区")
        self.assertEqual(common["project_status"], "正式披露")
        self.assertIsNone(common["start_price_raw"])
        self.assertEqual(common["final_price_raw"], "120.00 万元")
        self.assertIn("张老师", common["contact_info"])
        self.assertIn("023-63600000", common["contact_info"])
        self.assertEqual(common["special_notice"], "以现场踏勘为准")
        self.assertEqual(common["data_source"], "重庆联合产权交易所/重庆产权交易网")
        self.assertEqual(json.loads(common["attachments_json"]), bundle.attachments)
        self.assertNotIn("current_price", common)
        self.assertNotIn("current_price_display", common)
        self.assertNotIn("current_price_amount", common)

    def test_build_ai_context_uses_unified_ai_context(self):
        bundle = self.adapter.parse_detail_html(
            DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=12345",
        )

        context = self.adapter.build_ai_context(bundle)

        self.assertEqual(context.asset_group, "real_estate")
        self.assertEqual(context.paimai_id, "cquae:12345")
        self.assertEqual(context.html_key_values["项目编号"], "CQ20260701001")
        self.assertIn("source_platform: cquae", context.detail_text)
        self.assertIn("产权交易合同.pdf", context.detail_text)
        self.assertEqual(
            context.image_urls,
            [
                "https://www.cquae.com/upload/site-image-1.jpg",
                "https://img.example.test/site-image-2.jpg",
            ],
        )

    def test_classify_asset_group_treats_purple_clay_pots_as_goods(self):
        self.assertEqual(
            classify_asset_group("紫砂壶", "紫砂壶3把-92--一批紫砂壶（GM202602）", ""),
            "goods",
        )


if __name__ == "__main__":
    unittest.main()
