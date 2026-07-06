import unittest

from platform_adapters.tpre_adapter import TpreAdapter, TpreListItem


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}, timeout))
        return _FakeResponse({"code": 0, "data": {"projectName": "接口详情"}})


class TpreAdapterTests(unittest.TestCase):
    def test_parse_detail_response_extracts_nested_contacts_and_keeps_agency_separate(self):
        adapter = TpreAdapter()
        item = TpreListItem(
            source_item_id="TPRE-1",
            source_url="https://trade.tpre.cn/transaction-view/formal-project-details?id=abc",
            title="某企业增资项目",
            real_id="abc",
            detail_type="increase-prepare-project-details",
            system_name="企业增资",
            price_raw="100万元",
        )
        api_data = {
            "_source": "api",
            "data": {
                "code": 0,
                "data": {
                    "projectName": "某企业增资项目",
                    "orgName": "市场二部",
                    "projectCommonInfo": {
                        "centerContactInformation": {
                            "handlerName": "陈凯",
                            "handlerTelephone": "022-58922158",
                            "leaderName": "耿鹏",
                            "leaderTelephone": "022-58922150",
                        }
                    },
                    "saIncreaseCompanyInfo": {
                        "enterpriseName": "天津某某科技有限公司",
                        "contactName": "范海英",
                        "contactTelephone": "18920966068",
                    },
                },
            },
        }

        bundle = adapter.parse_detail_response(api_data, list_item=item)
        common = adapter.map_common_candidates(bundle)

        self.assertIn("陈凯", common["contact_info"])
        self.assertIn("022-58922158", common["contact_info"])
        self.assertIn("范海英", common["contact_info"])
        self.assertIn("18920966068", common["contact_info"])
        self.assertEqual(common["disposal_agency"], "市场二部")
        self.assertEqual(common["disposal_party"], "天津某某科技有限公司")
        self.assertNotEqual(common["disposal_party"], "市场二部")

    def test_fetch_detail_api_routes_formal_property_right_projects_to_confirmed_endpoint(self):
        session = _RecordingSession()
        adapter = TpreAdapter(base_url="https://trade.tpre.cn", session=session)
        item = TpreListItem(
            source_item_id="G32026TJ1000026",
            source_url="https://trade.tpre.cn/transaction-view/data/formal-project-details?id=abc",
            title="某产权转让项目",
            real_id="abc",
            detail_type="formal-project-details",
            system_code="PROPERTY_RIGHT_TRANSFER",
            system_name="产权转让",
        )

        data = adapter.fetch_detail_api(item)

        self.assertEqual(data["_source"], "api")
        self.assertEqual(
            session.calls[0][0],
            "https://trade.tpre.cn/transaction/biz/sa/property/right/project/anmuas/get",
        )
        self.assertEqual(session.calls[0][1], {"viewId": "abc"})

    def test_fetch_detail_api_routes_prepare_property_right_projects_to_confirmed_endpoint(self):
        session = _RecordingSession()
        adapter = TpreAdapter(base_url="https://trade.tpre.cn", session=session)
        item = TpreListItem(
            source_item_id="G32026TJ1000025-0",
            source_url="https://trade.tpre.cn/transaction-view/data/prepare-project-details?id=def",
            title="某产权转让预披露项目",
            real_id="def",
            detail_type="prepare-project-details",
            system_code="PROPERTY_RIGHT_TRANSFER",
            system_name="产权转让",
        )

        data = adapter.fetch_detail_api(item)

        self.assertEqual(data["_source"], "api")
        self.assertEqual(
            session.calls[0][0],
            "https://trade.tpre.cn/transaction/biz/sa/property/right/prepare/anmuas/get",
        )
        self.assertEqual(session.calls[0][1], {"viewId": "def"})

    def test_parse_detail_response_extracts_view_attachment_download_urls(self):
        adapter = TpreAdapter(base_url="https://trade.tpre.cn")
        item = TpreListItem(
            source_item_id="G32026TJ1000026",
            source_url="https://trade.tpre.cn/transaction-view/data/formal-project-details?id=abc",
            title="某产权转让项目",
            real_id="abc",
            detail_type="formal-project-details",
            system_code="PROPERTY_RIGHT_TRANSFER",
            system_name="产权转让",
        )
        api_data = {
            "_source": "api",
            "data": {
                "code": 0,
                "data": {
                    "projectName": "某产权转让项目",
                    "viewAttachment": [
                        {
                            "businessTypeName": "正式披露",
                            "attachmentTypes": [
                                {
                                    "attachmentTypeName": "交易条件附件",
                                    "attachments": [
                                        {
                                            "pkId": "8a5034b22a44581c85470775a3b1bb7f",
                                            "attachmentName": "产权交易合同.zip",
                                            "attachmentSuffix": "zip",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            },
        }

        bundle = adapter.parse_detail_response(api_data, list_item=item)

        self.assertEqual(len(bundle.attachments), 1)
        self.assertEqual(bundle.attachments[0]["name"], "产权交易合同.zip")
        self.assertEqual(
            bundle.attachments[0]["url"],
            "https://trade.tpre.cn/attachment/api/download/8a5034b22a44581c85470775a3b1bb7f",
        )
        self.assertEqual(bundle.attachments[0]["source_payload_type"], "detail_api.viewAttachment")


if __name__ == "__main__":
    unittest.main()
