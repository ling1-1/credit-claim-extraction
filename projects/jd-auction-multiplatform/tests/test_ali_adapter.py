import unittest

from platform_adapters.ali_adapter import (
    AliAuctionAdapter,
    AliBrowserProfileFetcher,
    AliListItem,
    AliTopApiFetcher,
    _classify_asset_group,
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


if __name__ == "__main__":
    unittest.main()
