import json
import unittest

from platform_adapters.ali_adapter import (
    AliAuctionAdapter,
    AliBrowserProfileFetcher,
    AliListChannel,
    AliListItem,
    AliMtopAuctionFetcher,
    AliTopApiFetcher,
    ALI_REAL_ESTATE_CHANNEL,
    _classify_asset_group,
    _ali_assessment_price_display,
    _ali_attachments,
    _extract_special_notice_from_text,
    _parse_ali_mtop_detail,
)


class AliAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = AliAuctionAdapter()

    def test_top_list_json_parses_id_title_and_prices(self):
        json_data = {
            "ali_asset_auction_list_response": {
                "result": {
                    "items": [
                        {
                            "itemId": "ALI-1001",
                            "title": "杭州市上城区一处商业房产",
                            "categoryName": "房产",
                            "startPrice": 1200000,
                            "currentPrice": 1450000,
                            "detailUrl": "https://zc-paimai.taobao.com/auction.htm?itemId=ALI-1001",
                        }
                    ]
                }
            }
        }

        items = self.adapter.parse_top_list(json_data)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "ALI-1001")
        self.assertEqual(items[0].title, "杭州市上城区一处商业房产")
        self.assertEqual(items[0].start_price_raw, "1200000")
        self.assertEqual(items[0].final_price_raw, "1450000")
        self.assertEqual(items[0].price_basis, "current_price")
        self.assertIn("currentPrice", items[0].source_excerpt)

    def test_top_detail_json_parses_effective_price_and_raw_bundle(self):
        json_data = {
            "item": {
                "id": 998877,
                "title": "某公司债权资产包转让",
                "category": "债权",
                "location": "浙江省杭州市",
                "status": "正在进行",
                "startPriceStr": "1,000万元",
                "currentPriceStr": "1,250万元",
                "contact": {"name": "李经理", "phone": "0571-12345678"},
                "specialNotice": "保证金以页面展示为准",
                "attachments": [{"name": "债权清单.xlsx", "url": "https://example.test/a.xlsx"}],
                "images": ["https://example.test/1.jpg"],
            }
        }

        bundle = self.adapter.parse_top_detail(json_data)

        self.assertEqual(bundle.source_item_id, "998877")
        self.assertEqual(bundle.title, "某公司债权资产包转让")
        self.assertEqual(bundle.asset_group, "debt")
        self.assertEqual(bundle.start_price_raw, "1,000万元")
        self.assertEqual(bundle.final_price_raw, "1,250万元")
        self.assertEqual(bundle.price_basis, "current_price")
        self.assertEqual(bundle.attachments[0]["name"], "债权清单.xlsx")
        self.assertEqual(bundle.image_urls, ["https://example.test/1.jpg"])
        self.assertIs(bundle.top_json, json_data)

    def test_browser_html_detects_bxpunish_as_blocked(self):
        fetcher = AliBrowserProfileFetcher()
        html = "<html><script>location.href='/bxpunish?x=1'</script>验证码</html>"

        bundle = fetcher.parse_rendered_detail(html, "https://zc-paimai.taobao.com/auction.htm?itemId=1")

        self.assertEqual(bundle.status, "blocked")
        self.assertIn("bxpunish", bundle.block_reason)

    def test_browser_html_detects_login_expired(self):
        fetcher = AliBrowserProfileFetcher()
        html = "<html><title>淘宝网 - 登录</title><form id='login-form'>请登录后查看</form></html>"

        bundle = fetcher.parse_rendered_detail(html, "https://login.taobao.com/member/login.jhtml")

        self.assertEqual(bundle.status, "needs_manual_login")
        self.assertIn("login", bundle.block_reason)

    def test_common_mapping_uses_final_price_for_effective_price_without_current_price_fields(self):
        bundle = self.adapter.parse_rendered_detail(
            """
            <html>
              <head><title>北京市朝阳区车辆一辆 - 阿里拍卖</title></head>
              <body>
                <h1>北京市朝阳区车辆一辆</h1>
                <p>标的类型：机动车</p>
                <p>所在地：北京市朝阳区</p>
                <p>起拍价：88,000 元</p>
                <p>当前价：91,500 元</p>
                <p>项目状态：竞价中</p>
                <p>咨询电话：010-88888888</p>
                <p>特别提示：车辆现状交付。</p>
                <a href="https://example.test/notice.pdf">竞买公告.pdf</a>
                <img src="https://example.test/car.jpg">
              </body>
            </html>
            """,
            url="https://zc-paimai.taobao.com/auction.htm?itemId=CAR-9",
        )

        common = self.adapter.map_common_candidates(bundle)

        self.assertEqual(common["source_platform"], "ali")
        self.assertEqual(common["source_item_id"], "CAR-9")
        self.assertEqual(common["asset_group"], "vehicle")
        self.assertEqual(common["start_price_raw"], "88,000 元")
        self.assertEqual(common["final_price_raw"], "91,500 元")
        self.assertEqual(common["price_basis"], "current_price")
        self.assertIn("当前价", common["source_excerpt"])
        self.assertNotIn("current_price", common)
        self.assertNotIn("current_price_display", common)
        self.assertNotIn("current_price_amount", common)

    def test_top_fetcher_builds_params_without_secret_or_cookie(self):
        fetcher = AliTopApiFetcher(app_key="public-app-key")

        params = fetcher.build_detail_params("ALI-42", fields="id,title,startPrice,currentPrice")

        self.assertEqual(params["app_key"], "public-app-key")
        self.assertEqual(params["item_id"], "ALI-42")
        self.assertNotIn("app_secret", params)
        self.assertNotIn("cookie", params)

    def test_mtop_list_uses_channel_items_context(self):
        fetcher = AliMtopAuctionFetcher()
        captured = {}

        def fake_call(api, version, data):
            captured["api"] = api
            captured["version"] = version
            captured["data"] = data
            return {
                "ret": ["SUCCESS::调用成功"],
                "data": {
                    "data": {
                        "GQL_getPageModulesData": {
                            "9018433170": {
                                "items": {
                                    "pageSize": 60,
                                    "totalCount": 17609678,
                                    "hasNextPage": True,
                                    "schemeList": [
                                        {
                                            "itemId": "1060875479188",
                                            "auctionTitle": "济南市历下区金茂府底层商铺",
                                            "displayInitialPrice": "188,800.00",
                                            "displayInitialPriceUnit": "元",
                                            "price": "188,800.00",
                                            "priceUnit": "元",
                                            "auctionLink": "https://zc-paimai.taobao.com/auction.htm?itemId=1060875479188",
                                            "location": "山东省济南市历下区",
                                        }
                                    ],
                                }
                            }
                        }
                    }
                },
            }

        fetcher._call_mtop = fake_call

        items = fetcher.fetch_list(limit=1, channels=[ALI_REAL_ESTATE_CHANNEL], pages_per_channel=1)

        variables = json.loads(captured["data"]["dfVariables"])
        item_context = json.loads(variables["context"]["_b_9018433170:items"])
        self.assertEqual(captured["api"], "mtop.taobao.datafront.invoke.auctionwalle")
        self.assertEqual(variables["pageId"], 1410667)
        self.assertEqual(variables["moduleIds"], "9018433170:items~keywordSource")
        self.assertEqual(item_context["page"], "1")
        self.assertEqual(variables["context"]["sceneCode"], "20200713C5R32B6N")
        self.assertEqual(items[0].item_id, "1060875479188")
        self.assertEqual(items[0].asset_group, "real_estate")
        self.assertEqual(items[0].category, "房地产")
        self.assertEqual(items[0].raw["_ali_channel"]["totalCount"], 17609678)

    def test_mtop_list_supports_additional_channel_definitions(self):
        fetcher = AliMtopAuctionFetcher()
        channel = AliListChannel(
            key="custom_debt",
            label="债权",
            asset_group="debt",
            page_id=1410667,
            scene_code="scene-x",
            spm="spm-x",
        )

        def fake_call(api, version, data):
            return {
                "ret": ["SUCCESS::调用成功"],
                "data": {
                    "data": {
                        "GQL_getPageModulesData": {
                            "9018433170": {
                                "items": {
                                    "pageSize": 60,
                                    "totalCount": 1,
                                    "hasNextPage": False,
                                    "schemeList": [
                                        {
                                            "itemId": "20001",
                                            "auctionTitle": "某公司债权资产包",
                                            "price": "100",
                                            "priceUnit": "万元",
                                        }
                                    ],
                                }
                            }
                        }
                    }
                },
            }

        fetcher._call_mtop = fake_call

        items = fetcher.fetch_list(limit=1, channels=[channel], pages_per_channel=1)

        self.assertEqual(items[0].asset_group, "debt")
        self.assertEqual(items[0].category, "债权")
        self.assertEqual(items[0].final_price_raw, "100万元")

    def test_mtop_detail_uses_consult_price_as_assessment_price(self):
        list_item = AliListItem(
            item_id="1060875479188",
            title="济南市历下区金茂府宸园21号楼107商铺",
            category="商业用房",
            asset_group="real_estate",
            start_price_raw="188,800.00 元",
        )
        bundle = _parse_ali_mtop_detail(
            {
                "itemId": "1060875479188",
                "title": "济南市历下区金茂府宸园21号楼107商铺",
                "itemBizType": "商业用房",
                "startPrice": 18880000,
                "currentPriceLong": 18880000,
                "consultPrice": 174616300,
            },
            source_url="https://zc-paimai.taobao.com/auction.htm?itemId=1060875479188",
            list_item=list_item,
            detail_json={},
            description_json={},
            attachments_json={},
            summary_json={"data": {"fieldList": [{"fieldName": "评估价", "fieldValue": "1,746,163"}]}},
            notice_json={},
        )

        self.assertEqual(bundle.asset_group, "real_estate")
        self.assertEqual(bundle.asset_type, "房地产")
        self.assertIn("评估价", bundle.assessment_price_time)
        self.assertIn("1,746,163", bundle.assessment_price_time)

    def test_ali_assessment_ignores_zero_price(self):
        display = _ali_assessment_price_display(
            {"marketPrice": 0, "consultPrice": 0, "assessmentPrice": 0},
            {},
            {},
        )

        self.assertIsNone(display)

    def test_mtop_detail_prefers_complete_location_from_description(self):
        list_item = AliListItem(
            item_id="1053599188591",
            title="济南市市中区建设路87号14号楼2--109储藏室",
            category="商业用房",
            asset_group="real_estate",
            asset_location="山东省济南市市中区",
        )
        bundle = _parse_ali_mtop_detail(
            {
                "itemId": "1053599188591",
                "title": "济南市市中区建设路87号14号楼2--109储藏室",
                "itemBizType": "商业用房",
                "auctionAddress": "建设路87号",
                "location": "山东省 济南市 市中区",
                "startPrice": 5746855,
                "currentPriceLong": 5746855,
            },
            source_url="https://zc-paimai.taobao.com/auction.htm?itemId=1053599188591",
            list_item=list_item,
            detail_json={},
            description_json={
                "data": {
                    "content": "<p>济南市市中区建设路 87 号 14 号楼 2--109 储藏室 [证号：济南20250234433] 面积：18.35平方米</p>"
                }
            },
            attachments_json={},
            summary_json={},
            notice_json={},
        )

        self.assertEqual(bundle.asset_location, "济南市市中区建设路87号14号楼2--109储藏室")

    def test_ali_special_notice_extracts_notice_matters_heading(self):
        text = (
            "五、咨询、展示看样的时间与方式。"
            "网上交保参与竞价注意事项：竞买人需仔细阅读公告，交纳保证金后参与竞价。"
            "后续普通条款。"
        )

        notice = _extract_special_notice_from_text(text)

        self.assertIn("网上交保参与竞价注意事项", notice)
        self.assertIn("交纳保证金", notice)

    def test_browser_rendered_detail_extracts_assessment_and_standard_asset_type(self):
        bundle = self.adapter.parse_rendered_detail(
            """
            <html>
              <head><title>济南市历下区金茂府商铺 - 阿里拍卖</title></head>
              <body>
                <h1>济南市历下区金茂府商铺</h1>
                <p>标的类型：商业用房</p>
                <p>所在地：山东省济南市历下区</p>
                <p>起拍价：188,800.00 元</p>
                <p>当前价：188,800.00 元</p>
                <p>评估价：1,746,163 元</p>
              </body>
            </html>
            """,
            url="https://zc-paimai.taobao.com/auction.htm?itemId=1060875479188",
        )

        self.assertEqual(bundle.asset_group, "real_estate")
        self.assertEqual(bundle.asset_type, "房地产")
        self.assertEqual(bundle.assessment_price_time, "评估价：1,746,163 元")

    def test_classify_prioritizes_title_asset_over_noisy_detail_text(self):
        self.assertEqual(
            _classify_asset_group(
                "2",
                "济南市历下区金茂府宸园21号楼107商铺 该页面其他区块提到债权投资",
            ),
            "real_estate",
        )

    def test_classify_ali_prop_titles_with_real_estate_and_vehicle_terms(self):
        self.assertEqual(
            _classify_asset_group("prop", "济南市槐荫区中骏尚城612号房 近邻地铁1/4号线"),
            "real_estate",
        )
        self.assertEqual(
            _classify_asset_group("prop", "济南槐荫区济微路30号2609.37㎡五层独栋写字楼"),
            "real_estate",
        )
        self.assertEqual(
            _classify_asset_group("prop", "24年上牌 本田NSS 350 摩托车 手续齐全 正常过户"),
            "vehicle",
        )
        self.assertEqual(
            _classify_asset_group("prop", "（特价房）济南市天桥区 金科·澜山公馆· 中楼层 含家具家电"),
            "real_estate",
        )
        self.assertEqual(
            _classify_asset_group("prop", "农用自卸三轮车 配置见描述"),
            "vehicle",
        )
        self.assertEqual(
            _classify_asset_group("prop", "（特价捡漏）济阳区济南浙江五金建材城，仅需28000，超低门槛刚需投资两不误"),
            "real_estate",
        )
        self.assertEqual(
            _classify_asset_group("prop", '停泊于青岛某水域的50米可载98人游艇级客船"虎鲨号"一艘'),
            "vehicle",
        )

    def test_ali_attachments_walks_nested_file_nodes(self):
        payload = {
            "data": {
                "materials": {
                    "fileList": [
                        {
                            "fileName": "竞买公告.pdf",
                            "downloadURL": "//example.test/notice.pdf",
                            "fileId": "F-1",
                        }
                    ]
                }
            }
        }

        attachments = _ali_attachments(payload)

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "竞买公告.pdf")
        self.assertEqual(attachments[0]["url"], "https://example.test/notice.pdf")
        self.assertEqual(attachments[0]["id"], "F-1")

    def test_classify_ali_prop_titles_prefers_specific_asset_groups(self):
        self.assertEqual(
            _classify_asset_group("prop", "山东立晨集团有限公司等25户债权资产包"),
            "debt",
        )
        self.assertEqual(
            _classify_asset_group("prop", "潮州农村商业银行股份有限公司0.8%股权"),
            "equity",
        )
        self.assertEqual(
            _classify_asset_group("prop", "某项目在建工程及土地使用权"),
            "land",
        )
        self.assertEqual(
            _classify_asset_group("prop", "山东省济南市长清第28加油站5年经营权出租"),
            "usufruct",
        )
        self.assertEqual(
            _classify_asset_group("prop", "黄铜带边角料等废料一批"),
            "goods",
        )
        self.assertEqual(
            _classify_asset_group("prop", "潍坊老炒匠食品有限公司5户11笔不良债权"),
            "debt",
        )
        self.assertEqual(
            _classify_asset_group("prop", "268卡特330GC液压挖掘机CAT00330HFEK00166有铲斗"),
            "equipment",
        )
        self.assertEqual(
            _classify_asset_group("车辆", "268卡特330GC液压挖掘机CAT00330HFEK00166有铲斗【GCJX】"),
            "equipment",
        )
        self.assertEqual(
            _classify_asset_group("prop", "268卡特330GC液压挖掘机CAT00330HFEK00166有铲斗【GCJX】 公告模板提到土地增值税和车辆过户"),
            "equipment",
        )
        self.assertEqual(
            _classify_asset_group("prop", "贵州花海拾味餐饮文化有限公司名下宝骏730一辆"),
            "vehicle",
        )
        self.assertEqual(
            _classify_asset_group("prop", "1辆别克GL8（车牌号苏BV9G69）"),
            "vehicle",
        )
        self.assertEqual(
            _classify_asset_group("prop", "中国石化销售股份有限公司山东济宁石油分公司土地使用权及地面资产"),
            "land",
        )


if __name__ == "__main__":
    unittest.main()
