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
        self.assertEqual(common["start_price_raw"], "120.00 万元")
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


# ===== 新类型的 map_special_candidates 测试 =====

    EQUITY_DETAIL_HTML = """
    <html><body>
      <h1>四川某公司股权转让</h1>
      <table>
        <tr><td>项目名称</td><td>四川某公司股权转让</td></tr>
        <tr><td>转让方</td><td>四川能源发展集团</td></tr>
        <tr><td>标的企业</td><td>目标科技有限公司</td></tr>
        <tr><td>股权比例</td><td>70%</td></tr>
        <tr><td>企业性质</td><td>有限责任公司</td></tr>
        <tr><td>所属行业</td><td>科技推广和应用服务业</td></tr>
        <tr><td>经营范围</td><td>技术开发、技术咨询</td></tr>
        <tr><td>股权结构</td><td>四川能源发展集团有限责任公司 70%，中国建筑一局（集团）有限公司 20%，成都空港城市发展集团有限公司 10%</td></tr>
        <tr><td>财务指标</td><td>营业收入1910.63万元，利润总额402.63万元</td></tr>
        <tr><td>资产评估</td><td>资产总额33907.51万元，负债总额15610.40万元，净资产18297.11万元</td></tr>
        <tr><td>重大事项</td><td>无重大未披露事项</td></tr>
      </table>
    </body></html>
    """

    IP_DETAIL_HTML = """
    <html><body>
      <h1>一种新型专利技术转让</h1>
      <table>
        <tr><td>项目名称</td><td>一种新型专利技术转让</td></tr>
        <tr><td>标的名称</td><td>发明专利"新型材料制备方法"</td></tr>
        <tr><td>标的证号</td><td>ZL202310000001.X</td></tr>
        <tr><td>知产类型</td><td>发明专利</td></tr>
        <tr><td>权利人</td><td>某科技大学</td></tr>
        <tr><td>标的简介</td><td>本发明涉及新材料制备技术领域</td></tr>
        <tr><td>有效期</td><td>20年</td></tr>
      </table>
    </body></html>
    """

    EQUIPMENT_DETAIL_HTML = """
    <html><body>
      <h1>闲置机器设备一批转让</h1>
      <table>
        <tr><td>项目名称</td><td>闲置机器设备一批转让</td></tr>
        <tr><td>存放位置</td><td>重庆市江北区厂房内</td></tr>
        <tr><td>设备状态</td><td>闲置</td></tr>
        <tr><td>设备类型</td><td>数控机床</td></tr>
        <tr><td>重要信息披露</td><td>设备已停用2年，需检修</td></tr>
      </table>
    </body></html>
    """

    GOODS_DETAIL_HTML = """
    <html><body>
      <h1>库存钢材一批转让</h1>
      <table>
        <tr><td>项目名称</td><td>库存钢材一批转让</td></tr>
        <tr><td>物资种类</td><td>钢材</td></tr>
        <tr><td>物资名称</td><td>螺纹钢</td></tr>
        <tr><td>物资所在地</td><td>重庆市沙坪坝区仓库</td></tr>
        <tr><td>数量</td><td>500吨</td></tr>
        <tr><td>权利人</td><td>重庆钢铁集团</td></tr>
      </table>
    </body></html>
    """

    USUFRUCT_DETAIL_HTML = """
    <html><body>
      <h1>某商场经营权转让</h1>
      <table>
        <tr><td>项目名称</td><td>某商场经营权转让</td></tr>
        <tr><td>权益类型</td><td>经营权</td></tr>
        <tr><td>标的名称</td><td>江北区某商场20年经营权</td></tr>
        <tr><td>标的所在位置</td><td>重庆市江北区观音桥</td></tr>
        <tr><td>经营期限</td><td>20年</td></tr>
        <tr><td>原权利人</td><td>重庆某商业管理公司</td></tr>
      </table>
    </body></html>
    """

    def test_map_special_equity_extracts_fields(self):
        bundle = self.adapter.parse_detail_html(
            self.EQUITY_DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=1340441",
        )
        special = self.adapter.map_special_candidates(bundle, "equity")

        self.assertEqual(special["transferor"], "四川能源发展集团")
        self.assertEqual(special["target_company"], "目标科技有限公司")
        self.assertEqual(special["equity_ratio"], "70%")
        self.assertEqual(special["company_nature"], "有限责任公司")
        self.assertEqual(special["company_industry"], "科技推广和应用服务业")
        self.assertEqual(special["business_scope"], "技术开发、技术咨询")
        self.assertIn("70%", special["ownership_structure"])
        self.assertIn("四川能源发展集团有限责任公司", special["ownership_structure"])
        self.assertIn("1910.63万元", special["financial_metrics"])
        self.assertIn("33907.51万元", special["asset_valuation"])
        self.assertEqual(special["disclosure_items"], "无重大未披露事项")
        # 不包含默认分支字段
        self.assertNotIn("raw_detail_text", special)
        self.assertNotIn("raw_table_pairs_json", special)

    def test_map_special_ip_extracts_fields(self):
        bundle = self.adapter.parse_detail_html(
            self.IP_DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=ip001",
        )
        special = self.adapter.map_special_candidates(bundle, "ip")

        self.assertEqual(special["subject_name"], "发明专利\"新型材料制备方法\"")
        self.assertEqual(special["certificate_no"], "ZL202310000001.X")
        self.assertEqual(special["ip_type"], "发明专利")
        self.assertEqual(special["right_holder"], "某科技大学")
        self.assertEqual(special["subject_intro"], "本发明涉及新材料制备技术领域")
        self.assertEqual(special["right_term"], "20年")
        self.assertNotIn("raw_detail_text", special)

    def test_map_special_equipment_extracts_fields(self):
        bundle = self.adapter.parse_detail_html(
            self.EQUIPMENT_DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=eq001",
        )
        special = self.adapter.map_special_candidates(bundle, "equipment")

        self.assertEqual(special["storage_location"], "重庆市江北区厂房内")
        self.assertEqual(special["equipment_status"], "闲置")
        self.assertEqual(special["equipment_type"], "数控机床")
        self.assertEqual(special["disclosed_defects"], "设备已停用2年，需检修")
        self.assertNotIn("raw_detail_text", special)

    def test_map_special_goods_extracts_fields(self):
        bundle = self.adapter.parse_detail_html(
            self.GOODS_DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=gd001",
        )
        special = self.adapter.map_special_candidates(bundle, "goods")

        self.assertEqual(special["goods_category"], "钢材")
        self.assertEqual(special["goods_name"], "螺纹钢")
        self.assertEqual(special["goods_location"], "重庆市沙坪坝区仓库")
        self.assertEqual(special["goods_details"], "500吨")
        self.assertEqual(special["right_holder"], "重庆钢铁集团")
        self.assertNotIn("raw_detail_text", special)

    def test_map_special_usufruct_extracts_fields(self):
        bundle = self.adapter.parse_detail_html(
            self.USUFRUCT_DETAIL_HTML,
            url="https://www.cquae.com/Project/Show?id=us001",
        )
        special = self.adapter.map_special_candidates(bundle, "usufruct")

        self.assertEqual(special["right_category"], "经营权")
        self.assertEqual(special["subject_name"], "江北区某商场20年经营权")
        self.assertEqual(special["subject_location"], "重庆市江北区观音桥")
        self.assertEqual(special["valid_period"], "20年")
        self.assertEqual(special["original_right_holder"], "重庆某商业管理公司")
        self.assertNotIn("raw_detail_text", special)

    def test_classify_asset_group_detects_ip(self):
        """含知识产权关键词的标的应分类为 ip"""
        self.assertEqual(
            classify_asset_group("", "某公司专利权转让", ""),
            "ip",
        )
        self.assertEqual(
            classify_asset_group("", "某商标转让", ""),
            "ip",
        )
        self.assertEqual(
            classify_asset_group("", "软件著作权转让", ""),
            "ip",
        )

    def test_map_special_other_falls_back_to_raw(self):
        """未匹配的类型（other）仍返回默认 raw_detail_text"""
        html = """
        <html><body>
          <table><tr><td>项目名称</td><td>其他项目</td></tr></table>
        </body></html>
        """
        bundle = self.adapter.parse_detail_html(html, url="https://www.cquae.com/Project/Show?id=other001")
        special = self.adapter.map_special_candidates(bundle, "other")

        self.assertIn("raw_detail_text", special)
        self.assertIn("raw_table_pairs_json", special)
        self.assertNotIn("subject_name", special)


if __name__ == "__main__":
    unittest.main()
