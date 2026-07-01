import tempfile
import sys
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

import jd.logger as logger_module
import jd_scraper_v2 as scraper
from jd.config import LogConfig
from jd.conflict_detector import ConflictDetector, ConflictSeverity
from jd.field_standardizer import FieldStandardizer
from jd.ai_extractor import AIExtractionContext, AIExtractionResult, AIFieldExtractor
from jd.logger import get_logger
from jd_scraper_v2 import (
    JDCategory,
    extract_common_values,
    extract_key_values_from_html,
    extract_special_values,
    field_result_value,
    sync_common_special_values,
)
from jd_mysql_store import MYSQL_SCHEMA, clean_invalid_assessment_rows


class FakeBatchAIExtractor:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def is_available(self):
        return True

    def batch_extract(self, fields, context):
        self.calls.append((fields, context))
        results = {}
        for field_key, field_label, _ in fields:
            fixture = self.values.get(field_key)
            if isinstance(fixture, dict) and "value" in fixture:
                value = fixture.get("value")
                original_text = fixture.get("source_text", str(value) if value is not None else "")
            else:
                value = fixture
                original_text = str(value) if value is not None else ""
            results[field_key] = AIExtractionResult(
                field_key=field_key,
                field_label=field_label,
                value=value,
                confidence=0.91 if value is not None else 0.0,
                reasoning="fixture",
                original_text=original_text,
            )
        return results

    def extract_field(self, field_key, field_label, field_description, context):
        value = self.values.get(field_key)
        if isinstance(value, dict) and "value" in value:
            raw_value = value.get("value")
            original_text = value.get("source_text", str(raw_value) if raw_value is not None else "")
        else:
            raw_value = value
            original_text = str(raw_value) if raw_value is not None else ""
        return AIExtractionResult(
            field_key=field_key,
            field_label=field_label,
            value=raw_value,
            confidence=0.91 if raw_value is not None else 0.0,
            reasoning="fixture",
            original_text=original_text,
        )


class FakeVisionAIExtractor(FakeBatchAIExtractor):
    def __init__(self, values, image_details):
        super().__init__(values)
        self.image_details = image_details
        self.image_calls = []

    def extract_ip_details_from_images(self, image_urls, context):
        self.image_calls.append((image_urls, context))
        return AIExtractionResult(
            field_key="ip_details",
            field_label="知识产权逐项明细",
            value=self.image_details,
            confidence=0.93,
            reasoning="fixture image table",
            original_text="图片表格",
        )


class V2RegressionTests(unittest.TestCase):
    def tearDown(self):
        scraper.ai_extractor = None
        if logger_module._global_logger:
            logger_module._global_logger.close()
        logger_module._global_logger = None

    def test_money_conflict_uses_severity_order_not_string_order(self):
        detector = ConflictDetector()

        same = detector.detect_field_conflict(
            "principal_balance",
            "本金余额",
            api_value="100万元",
            html_value="1000000元",
            field_type="money",
        )
        different = detector.detect_field_conflict(
            "principal_balance",
            "本金余额",
            api_value="100万元",
            html_value="200万元",
            field_type="money",
        )

        self.assertEqual(same.severity, ConflictSeverity.NONE)
        self.assertEqual(different.severity, ConflictSeverity.HIGH)
        self.assertTrue(different.needs_review)

    def test_phone_standardizer_keeps_multiple_numbers_separate(self):
        result = FieldStandardizer.phone("朱经理 021-64450262/15801908761")

        self.assertEqual(result.phone_type, "multiple")
        self.assertIn("021-64450262", result.display)
        self.assertIn("158 0190 8761", result.display)
        self.assertIn(";", result.pure_number)

    def test_logger_reconfigures_when_new_config_is_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.log"
            second_path = Path(tmp) / "second.log"

            first = get_logger(LogConfig(log_level="ERROR", log_file=first_path, console_output=False))
            first.debug("debug_hidden")
            first.error("error_visible")

            second = get_logger(LogConfig(log_level="DEBUG", log_file=second_path, console_output=False))
            second.debug("debug_visible")
            second.close()

            self.assertIn("error_visible", first_path.read_text(encoding="utf-8"))
            self.assertNotIn("debug_hidden", first_path.read_text(encoding="utf-8"))
            self.assertIn("debug_visible", second_path.read_text(encoding="utf-8"))

    def test_logger_console_write_failure_does_not_raise(self):
        class BrokenStderr:
            def write(self, _):
                raise OSError(22, "Invalid argument")

            def flush(self):
                pass

        original_stderr = sys.stderr
        try:
            sys.stderr = BrokenStderr()
            logger = get_logger(LogConfig(log_level="INFO", console_output=True))
            logger.info("console_failure_test", "should not raise")
        finally:
            sys.stderr = original_stderr

    def test_ai_context_keeps_activity_time_hints_and_truncates_text(self):
        detail_text = "详情" * 2600
        notice_text = (
            "普通公告内容" * 700
            + "本院将于2026年6月29日10时起至2026年6月30日10时止进行公开拍卖活动。"
            + "后续说明" * 700
        )

        context = scraper.build_ai_context(
            scraper.ParsedHTML({}, detail_text, []),
            scraper.ParsedHTML({}, notice_text, []),
            "real_estate",
            "test-time-hints",
        )

        self.assertLessEqual(len(context.detail_text), 5000)
        self.assertLessEqual(len(context.notice_text), 4000)
        hints = context.html_key_values.get("_activity_time_hints", "")
        self.assertIn("2026年6月29日10时起至2026年6月30日10时止", hints)
        self.assertIn("公开拍卖活动", hints)

    def test_ai_prompt_allows_auction_time_from_notice(self):
        extractor = AIFieldExtractor()
        prompt = extractor._build_prompt(
            "signup_start_time",
            "报名开始时间",
            "挂牌、竞价或拍卖活动开始时间",
            AIExtractionContext(
                html_key_values={},
                detail_text="",
                notice_text="竞买公告：本院将于2026年6月29日10时起至2026年6月30日10时止进行公开拍卖活动。",
                asset_group="real_estate",
                paimai_id="test-prompt",
            ),
        )

        self.assertIn("竞买公告/须知中的'竞价时间''拍卖时间'就是正确的起止时间来源", prompt)
        self.assertIn("不要提取公告发布日期、展示看样期、资质审核截止日等非竞价时段的时间", prompt)
        self.assertNotIn("不要把公告期、展示期、资质审核截止时间误当成起止时间。", prompt)

    def test_auction_stage_does_not_display_raw_auction_type_number(self):
        self.assertEqual(
            scraper.compute_auction_stage(None, 2, "柔锋机械科技名下知识产权按现状整体拍卖"),
            "拍卖",
        )

    def test_v2_debt_package_stores_details_with_primary_debtor_summary(self):
        html = """
        <table>
          <tr><td>序号</td><td>借款人</td><td>担保人</td><td>担保物</td><td>基准日：2025年6月20日 单位：人民币元</td></tr>
          <tr><td>本金余额</td><td>利息金额</td><td>实现债权费用金额</td><td>债权合计</td></tr>
          <tr><td>1</td><td>A公司</td><td>保证人：张三</td><td>无</td><td>1000000.00</td><td>200000.00</td><td>0</td><td>1200000.00</td></tr>
        </table>
        """
        parsed = extract_key_values_from_html(html)

        values, results, details = extract_special_values(
            asset_group="debt",
            parsed=parsed,
            notice_parsed=parsed,
            core={},
            paimai_id="test-id",
        )

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["debtor_name"], "A公司")
        self.assertEqual(details[0]["guarantor"], "保证人：张三")
        self.assertEqual(values["debtor_name"], "A公司")
        self.assertNotIn("principal_balance", values)
        self.assertNotIn("interest_balance", values)
        self.assertEqual(values["household_count"], "1")
        self.assertEqual(results["debtor_name"]["source_path"], "from_debt_details_debtor_name")

    def test_v2_debt_package_parses_name_total_principal_interest_table(self):
        html = """
        <table>
          <tr><td>序号</td><td>名称</td><td>债权合计</td><td>本金余额</td><td>欠息</td><td>担保方式</td><td>备注</td></tr>
          <tr><td>1</td><td>白银市平川区长征供热有限公司</td><td>6669.94</td><td>4951.23</td><td>1718.71</td><td>抵押</td><td>债权</td></tr>
          <tr><td>2</td><td>甘肃三鼎乳业有限责任公司</td><td>1776.78</td><td>1500.00</td><td>276.78</td><td>抵押</td><td>债权</td></tr>
          <tr><td>合计</td><td>8446.72</td><td>6451.23</td><td>1995.49</td></tr>
        </table>
        """
        parsed = extract_key_values_from_html(html)

        values, _, details = extract_special_values(
            asset_group="debt",
            parsed=parsed,
            notice_parsed=parsed,
            core={},
            paimai_id="test-id",
        )

        self.assertEqual(len(details), 2)
        self.assertEqual(details[0]["debtor_name"], "白银市平川区长征供热有限公司")
        self.assertEqual(details[1]["debtor_name"], "甘肃三鼎乳业有限责任公司")
        self.assertEqual(values["debtor_name"], "白银市平川区长征供热有限公司；甘肃三鼎乳业有限责任公司")
        self.assertNotIn("principal_balance", values)
        self.assertNotIn("interest_balance", values)
        self.assertEqual(values["household_count"], "2")

    def test_v2_common_special_notice_stays_empty_without_explicit_heading(self):
        notice_html = """
        <p>\u4e94\u3001\u5176\u4ed6\u8bf4\u660e</p>
        <p>\u672c\u6b21\u7ade\u4ef7\u662f\u7ecf\u6cd5\u5b9a\u516c\u544a\u671f\u548c\u5c55\u793a\u671f\u540e\u4e3e\u884c\u7684\uff0c
        \u5df2\u5c31\u672c\u6b21\u5904\u7f6e\u6807\u7684\u7269\u5df2\u77e5\u53ca\u53ef\u80fd\u5b58\u5728\u7684\u7455\u75b5\u4f5c\u4e86\u5ba2\u89c2\u3001\u8be6\u5c3d\u7684\u8bf4\u660e\u3002</p>
        <p>\u8d44\u4ea7\u8f6c\u8ba9\u8fc7\u7a0b\u4e2d\u51fa\u73b0\u4e0b\u5217\u60c5\u5f62\u7684\uff0c\u5904\u7f6e\u65b9\u53ef\u4ee5\u8981\u6c42\u7acb\u5373\u4e2d\u6b62\u3002</p>
        """
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(notice_html)

        values, results = extract_common_values(
            category=JDCategory("109", "\u503a\u6743"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "\u6d4b\u8bd5\u9879\u76ee"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertNotIn("special_notice", values)
        self.assertNotEqual(results.get("special_notice", {}).get("source_path"), "risk_notice_section")

    def test_v2_common_special_notice_uses_explicit_heading(self):
        notice_html = """
        <p>\u7279\u522b\u63d0\u793a\uff1a\u8bf7\u7ade\u4e70\u4eba\u81ea\u884c\u5c3d\u8c03\uff0c\u8f6c\u8ba9\u65b9\u4e0d\u627f\u62c5\u6807\u7684\u7455\u75b5\u8d23\u4efb\u3002</p>
        """
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(notice_html)

        values, results = extract_common_values(
            category=JDCategory("109", "\u503a\u6743"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "\u6d4b\u8bd5\u9879\u76ee"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertIn("\u81ea\u884c\u5c3d\u8c03", values["special_notice"])
        self.assertEqual(results["special_notice"]["source_path"], "special_notice_section")

    def test_v2_common_special_notice_prefers_body_section_over_short_api_title(self):
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html("""
        <p>三、重大风险提示（现状拍卖，风险自负）</p>
        <p>债权金额仅为账面计算数值，不代表债务人实际具备还款能力。</p>
        <p>四、报名规则</p>
        <p>按平台要求报名。</p>
        """)

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目", "specialNotice": "重要提示（风险揭示）"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertIn("账面计算数值", values["special_notice"])
        self.assertEqual(results["special_notice"]["source_path"], "special_notice_section")

    def test_v2_common_special_notice_skips_short_body_heading_and_uses_full_notice(self):
        parsed = extract_key_values_from_html("<p>重要提示（风险揭示）</p>")
        notice_parsed = extract_key_values_from_html("""
        <p>三、重大风险提示（现状拍卖，风险自负）</p>
        <p>竞买人受让债权后存在无法收回、无法全额收回债权本金及利息的巨大风险。</p>
        <p>四、报名规则</p>
        <p>按平台要求报名。</p>
        """)

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertIn("无法全额收回", values["special_notice"])
        self.assertNotEqual(values["special_notice"], "重要提示（风险揭示）")

    def test_v2_common_html_fields_use_ai_batch_override_for_auction_times(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_start_time": "2026-06-29 16:00:00",
                "signup_end_time": "2026-06-29 17:00:00",
                "contact_info": "张经理 18919893090；王经理 13900000000",
            }
        )
        notice_html = """
        <p>公告时间为2026年06月16日至2026年06月28日。</p>
        <p>我行将于2026年06月29日16时至2026年06月29日17时止（延时的除外）
        在京东资产拍卖网络平台上组织公开竞价拍卖活动。</p>
        <p>咨询电话：张经理 18919893090；王经理 13900000000</p>
        """
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(notice_html)

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 16:00:00")
        self.assertEqual(values["signup_end_time"], "2026-06-29 17:00:00")

    def test_v2_common_time_parses_chinese_hour_range_with_qi_zhi(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_start_time": {
                    "value": "2026年6月29日",
                    "source_text": "定于2026年6月29日9时起至2026年6月30日9时止（延时除外）在京东资产交易网络平台上进行公开竞价活动",
                },
                "signup_end_time": {
                    "value": "2026年6月30日",
                    "source_text": "定于2026年6月29日9时起至2026年6月30日9时止（延时除外）在京东资产交易网络平台上进行公开竞价活动",
                },
            }
        )
        parsed = extract_key_values_from_html("")

        values, _ = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 09:00:00")
        self.assertEqual(values["signup_end_time"], "2026-06-30 09:00:00")

    def test_v2_common_time_parses_chinese_morning_range_from_ai_source(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_start_time": {
                    "value": "2026年6月29日",
                    "source_text": "将于2026年6月29日上午10时起至2026年6月30日上午10时止（延时的除外）进行公开拍卖活动",
                },
                "signup_end_time": {
                    "value": "2026年6月30日",
                    "source_text": "将于2026年6月29日上午10时起至2026年6月30日上午10时止（延时的除外）进行公开拍卖活动",
                },
            }
        )
        parsed = extract_key_values_from_html("")

        values, _ = extract_common_values(
            category=JDCategory("112", "土地"),
            asset_group="land",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "土地项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-land",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 10:00:00")
        self.assertEqual(values["signup_end_time"], "2026-06-30 10:00:00")

    def test_v2_common_time_rejects_viewing_deadline_as_signup_end(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_end_time": {
                    "value": "2026年6月26日10:00",
                    "source_text": "该标的：开拍前（2026年6月26日10:00）统一看样，请自行预约好工作人员。",
                }
            }
        )
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("101", "住宅用房"),
            asset_group="real_estate",
            list_item={},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试房产",
                            "auctionStartTime": "2026-06-29 19:30:00",
                        }
                    }
                },
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-viewing-time",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 19:30:00")
        self.assertNotIn("signup_end_time", values)
        self.assertNotEqual(results.get("signup_end_time", {}).get("source_path"), "llm_batch")

    def test_v2_common_special_notice_reads_announcement_heading(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(
            """
            <p>五、变卖方式</p>
            <p>六、特别提示</p>
            <p>1、买受人应自行办理过户手续，相关税费由买受人承担。</p>
            <p>2、标的物可能存在占用、欠费等情况，请竞买人自行核实。</p>
            <p>七、其他事项</p>
            """
        )

        values, results = extract_common_values(
            category=JDCategory("101", "房地产"),
            asset_group="real_estate",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "房地产项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-real-estate",
        )

        self.assertIn("买受人应自行办理过户手续", values["special_notice"])
        self.assertEqual(results["special_notice"]["source_path"], "special_notice_section")

    def test_v2_common_special_notice_uses_physical_asset_risk_paragraph_without_heading(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(
            """
            <p>八、本次拍卖是经法定公告期和展示期后举行的，就拍卖标的物已知及可能存在的瑕疵已在本次拍卖资料中作出说明。拍卖人对拍卖标的物所作的说明和提供的视频资料、图片等，仅供竞买人参考，不构成对标的物的任何担保。</p>
            <p>九、拍卖成交后，买受人应及时付款。</p>
            """
        )

        values, results = extract_common_values(
            category=JDCategory("105", "车辆"),
            asset_group="vehicle",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "车辆项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-vehicle",
        )

        self.assertIn("不构成对标的物的任何担保", values["special_notice"])
        self.assertEqual(results["special_notice"]["source_path"], "risk_notice_section")

    def test_v2_common_contact_deduplicates_same_phone(self):
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html("<p>咨询电话：任经理 18839118790</p>")

        values, _ = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {
                    "data": {
                        "basicData": {"title": "测试项目"},
                        "contactName": "任经理",
                        "contactPhone": "18839118790",
                    }
                },
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-id",
        )

        self.assertEqual(values["contact_info"], "任经理 18839118790")

    def test_v2_common_contact_extracts_multiple_people_from_notice(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(
            "<p>咨询看样时间：2026年6月23日至2026年6月29日。</p>"
            "<p>联系人：李先生；联系电话：19133837691、姬素莹15031848306。</p>"
        )

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-contact-multi",
        )

        self.assertIn("李先生 19133837691", values["contact_info"])
        self.assertIn("姬素莹 15031848306", values["contact_info"])
        self.assertEqual(results["contact_info"]["source_path"], "contact_lines")

    def test_v2_common_contact_merges_product_basic_and_notice_contacts(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html("<p>联系电话：姬素莹15031848306。</p>")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "product_basic": {
                    "data": {
                        "judicatureBasicInfoResult": {
                            "consultName": "李先生",
                            "consultTel": "19133837691",
                        }
                    }
                },
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-contact-product-basic",
        )

        self.assertIn("李先生 19133837691", values["contact_info"])
        self.assertIn("姬素莹 15031848306", values["contact_info"])
        self.assertEqual(results["contact_info"]["source_path"], "merged_contacts")

    def test_v2_common_status_changes_to_ended_when_end_time_passed(self):
        parsed = extract_key_values_from_html("")

        values, _ = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {"auctionStatus": 1}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )
        values["signup_end_time"] = "2000-01-01 10:00:00"
        scraper.adjust_project_status_by_time(values, {})

        self.assertEqual(values["project_status"], "已结束")

    def test_v2_common_special_notice_ai_requires_explicit_heading(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "special_notice": {
                    "value": "本项目债权资产存在计算误差，请投资人自行调查判断。",
                    "source_text": "本项目债权资产存在计算误差，请投资人自行调查判断。",
                }
            }
        )
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertNotIn("special_notice", values)
        self.assertNotEqual(results.get("special_notice", {}).get("source_path"), "llm_batch")

    def test_v2_common_contact_ai_without_phone_is_not_applied(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "contact_info": "代理审判长胡世友、书记员罗曼",
            }
        )
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertNotIn("contact_info", values)
        self.assertNotEqual(results.get("contact_info", {}).get("source_path"), "llm_batch")

    def test_v2_common_assessment_zero_is_left_blank(self):
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={"assessmentPriceCN": "0", "assessmentPrice": 0},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目", "assessmentPriceCN": "0"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertNotIn("assessment_price_time", values)
        self.assertEqual(results["assessment_price_time"]["status"], "missing_on_page")

    def test_v2_common_assessment_ignores_market_price_multiplier(self):
        parsed = extract_key_values_from_html("")
        notice = extract_key_values_from_html(
            "<p>抵押房产所在商场供配电设施由案外人控制，转供电价约为市场价2倍，"
            "显著影响招租及运营成本。</p>"
        )

        value, excerpt = scraper.extract_assessment_text(notice.text)
        self.assertIsNone(value)
        self.assertIsNone(excerpt)
        self.assertFalse(scraper.is_valid_assessment_price_time("市场价2", "市场价2"))

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=notice,
            paimai_id="test-id",
        )

        self.assertNotIn("assessment_price_time", values)
        self.assertEqual(results["assessment_price_time"]["status"], "missing_on_page")

    def test_v2_clean_invalid_assessment_rows_clears_market_multiplier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO auction_items_common
                      (paimai_id, batch_id, source_url, asset_group, asset_group_label,
                       assessment_price_time, assessment_price_amount, assessment_amount, assessment_date)
                    VALUES
                      ('310865048', 'batch1', 'https://paimai.jd.com/310865048', 'debt', '债权',
                       '市场价2', 2, 2, NULL)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO field_extractions
                      (paimai_id, field_namespace, asset_group, field_key, field_label,
                       raw_value, normalized_value, status, method, confidence,
                       source_payload_type, source_path, source_excerpt, missing_reason, extracted_at)
                    VALUES
                      ('310865048', 'common', 'debt', 'assessment_price_time', '评估价格及时间',
                       '市场价2', '市场价2', 'extracted', 'html_text_regex', 0.95,
                       'notice_html', 'text_regex', '转供电价约为市场价2倍', '', '2026-06-30 00:00:00')
                    """
                )

            cleared = clean_invalid_assessment_rows(db_path)

            self.assertEqual(cleared, 1)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                common = conn.execute(
                    "SELECT assessment_price_time, assessment_price_amount, assessment_amount, assessment_date "
                    "FROM auction_items_common WHERE paimai_id='310865048'"
                ).fetchone()
                extraction = conn.execute(
                    "SELECT status, method, source_path FROM field_extractions "
                    "WHERE paimai_id='310865048' AND field_key='assessment_price_time'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(common["assessment_price_time"], "")
            self.assertIsNone(common["assessment_price_amount"])
            self.assertIsNone(common["assessment_amount"])
            self.assertIsNone(common["assessment_date"])
            self.assertEqual(extraction["status"], "missing_on_page")
            self.assertEqual(extraction["source_path"], "invalid_assessment_filtered")

    def test_v2_clean_invalid_assessment_rows_keeps_structured_assessment_price(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO auction_items_common
                      (paimai_id, batch_id, source_url, asset_group, asset_group_label,
                       assessment_price_time, assessment_price_amount, assessment_amount, assessment_date)
                    VALUES
                      ('310648924', 'batch1', 'https://paimai.jd.com/310648924', 'usufruct', '用益物权',
                       '3.885399亿', 388539900, 388539900, NULL)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO field_extractions
                      (paimai_id, field_namespace, asset_group, field_key, field_label,
                       raw_value, normalized_value, status, method, confidence,
                       source_payload_type, source_path, source_excerpt, missing_reason, extracted_at)
                    VALUES
                      ('310648924', 'common', 'usufruct', 'assessment_price_time', '评估价格及时间',
                       '3.885399亿', '3.885399亿', 'extracted', 'api', 0.95,
                       'list_json', 'assessmentPriceCN', '3.885399亿', '', '2026-06-30 00:00:00')
                    """
                )

            cleared = clean_invalid_assessment_rows(db_path)

            self.assertEqual(cleared, 0)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                common = conn.execute(
                    "SELECT assessment_price_time, assessment_price_amount FROM auction_items_common "
                    "WHERE paimai_id='310648924'"
                ).fetchone()
                extraction = conn.execute(
                    "SELECT status, source_path FROM field_extractions "
                    "WHERE paimai_id='310648924' AND field_key='assessment_price_time'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(common["assessment_price_time"], "3.885399亿")
            self.assertEqual(common["assessment_price_amount"], 388539900)
            self.assertEqual(extraction["status"], "extracted")
            self.assertEqual(extraction["source_path"], "assessmentPriceCN")

    def test_v2_mysql_schema_uses_typed_columns_for_core_values(self):
        schema = "\n".join(MYSQL_SCHEMA)

        self.assertIn("signup_start_time DATETIME", schema)
        self.assertIn("signup_end_time DATETIME", schema)
        self.assertIn("start_price_amount DECIMAL(18,2)", schema)
        self.assertIn("assessment_date DATE", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS asset_debt_details", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS asset_ip_details", schema)

    def test_v2_common_assessment_ai_rejects_start_price_without_assessment_signal(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "assessment_price_time": {
                    "value": "评估价：9,380,894.56元；评估基准日：2025年11月11日",
                    "source_text": "起拍价格：9,380,894.56元 基准日：2025年11月11日 债权金额：13,401,277.95元",
                }
            }
        )
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertNotIn("assessment_price_time", values)
        self.assertNotEqual(results.get("assessment_price_time", {}).get("source_path"), "llm_batch")

    def test_v2_common_assessment_ai_accepts_explicit_assessment_text(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "assessment_price_time": {
                    "value": "评估价：100万元；评估基准日：2025年6月20日",
                    "source_text": "评估价：100万元；评估基准日：2025年6月20日",
                }
            }
        )
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertIn("评估价", values["assessment_price_time"])
        self.assertEqual(results["assessment_price_time"]["source_path"], "llm_batch")

    def test_v2_common_time_uses_range_source_excerpt_when_ai_returns_date_only(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_start_time": {
                    "value": "2026年06月29日",
                    "source_text": "竞价时间为2026年06月29日16:00起2026年06月29日17:00止（延时除外）",
                },
                "signup_end_time": {
                    "value": "2026年06月29日",
                    "source_text": "竞价时间为2026年06月29日16:00起2026年06月29日17:00止（延时除外）",
                },
            }
        )
        parsed = extract_key_values_from_html("")

        values, _ = extract_common_values(
            category=JDCategory("109", "债权"),
            asset_group="debt",
            list_item={},
            bundle={
                "core": {"data": {"basicData": {"title": "测试项目"}}},
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-id",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 16:00:00")
        self.assertEqual(values["signup_end_time"], "2026-06-29 17:00:00")

    def test_v2_debt_special_fields_and_details_use_ai_batch_when_available(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "household_count": "2",
                "creditor": "测试银行股份有限公司",
                "debt_package_details_json": [
                    {
                        "sequence_no": "1",
                        "debtor_name": "甲公司",
                        "principal_balance": "100.00",
                        "interest_balance": "10.00",
                        "guarantor": "保证人甲",
                        "claim_total": "110.00",
                        "amount_unit": "万元",
                    },
                    {
                        "sequence_no": "2",
                        "debtor_name": "乙公司",
                        "principal_balance": "200.00",
                        "interest_balance": "20.00",
                        "guarantor": "保证人乙",
                        "claim_total": "220.00",
                        "amount_unit": "万元",
                    },
                ],
            }
        )
        parsed = extract_key_values_from_html("甲公司本金100万元，乙公司本金200万元。")

        values, results, details = extract_special_values(
            asset_group="debt",
            parsed=parsed,
            notice_parsed=parsed,
            core={},
            paimai_id="test-id",
        )

        self.assertEqual(values["creditor"], "测试银行股份有限公司")
        self.assertEqual(values["household_count"], "2")
        self.assertEqual(values["debtor_name"], "甲公司；乙公司")
        self.assertNotIn("principal_balance", values)
        self.assertNotIn("interest_balance", values)
        self.assertEqual(len(details), 2)
        self.assertEqual(details[1]["debtor_name"], "乙公司")
        self.assertEqual(details[1]["guarantor"], "保证人乙")
        requested_batches = [{field[0] for field in call[0]} for call in scraper.ai_extractor.calls]
        self.assertTrue(any("debt_package_details_json" in batch for batch in requested_batches))

    def test_v2_attachment_ai_details_are_normalized(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "attachment_debt_details": [
                    {
                        "sequence_no": "1",
                        "debtor_name": "附件甲公司",
                        "principal_balance": "100万元",
                        "interest_balance": "10万元",
                        "claim_total": "110万元",
                    }
                ]
            }
        )

        details = scraper.ai_parse_attachment("序号 债务人 本金 利息 合计\n1 附件甲公司 100万元 10万元 110万元", "债权明细.xlsx", "test-id")

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["debtor_name"], "附件甲公司")

    def test_v2_debt_attachment_details_override_page_details(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "debtor_name": "页面甲公司",
                "debt_package_details_json": [
                    {"sequence_no": "1", "debtor_or_asset": "页面甲公司", "principal_balance": "1万元"}
                ],
                "attachment_debt_details": [
                    {"sequence_no": "1", "debtor_name": "附件甲公司", "principal_balance": "100万元"}
                ],
            }
        )
        original_download = scraper.download_attachment
        original_extract = scraper.extract_text_from_attachment
        try:
            scraper.download_attachment = lambda url: b"fake-content"
            scraper.extract_text_from_attachment = lambda content, filename: "序号 债务人 本金\n1 附件甲公司 100万元"
            parsed = extract_key_values_from_html("页面甲公司 本金1万元")

            values, results, details = extract_special_values(
                asset_group="debt",
                parsed=parsed,
                notice_parsed=parsed,
                core={},
                paimai_id="test-id",
                attachments={
                    "files": [
                        {
                            "attachmentName": "债权明细.xlsx",
                            "attachmentAddress": "https://storage.jd.com/fake.xlsx",
                        }
                    ]
                },
            )
        finally:
            scraper.download_attachment = original_download
            scraper.extract_text_from_attachment = original_extract

        self.assertEqual(details[0]["debtor_name"], "附件甲公司")
        self.assertEqual(values["debtor_name"], "附件甲公司")
        self.assertNotIn("principal_balance", values)
        self.assertEqual(values["household_count"], "1")
        self.assertEqual(results["debtor_name"]["source_path"], "attachment_debt_details_debtor_name")
        self.assertEqual(results["household_count"]["source_path"], "attachment_debt_details_count")

    def test_v2_ip_fields_fallback_to_project_title_when_detail_has_images_only(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("<p><img src='a.png'/></p>")

        values, results, details = extract_special_values(
            asset_group="ip",
            parsed=parsed,
            notice_parsed=extract_key_values_from_html(""),
            core={
                "data": {
                    "basicData": {
                        "title": "柔锋机械科技（江苏）有限公司名下7项计算机软件著作权及17项专利权按现状整体拍卖"
                    }
                }
            },
            paimai_id="test-ip",
        )

        self.assertEqual(values["subject_name"], "柔锋机械科技（江苏）有限公司名下7项计算机软件著作权及17项专利权按现状整体拍卖")
        self.assertEqual(values["right_holder"], "柔锋机械科技（江苏）有限公司")
        self.assertNotIn("ip_type", values)
        self.assertNotIn("certificate_no", values)
        self.assertIn("计算机软件著作权", values["specific_category"])
        self.assertIn("专利权", values["specific_category"])
        self.assertEqual(values["subject_intro"], values["subject_name"])
        self.assertEqual(values["ip_count"], "24")
        self.assertEqual(results["ip_count"]["source_path"], "basicData.title_count_regex")
        self.assertEqual(len(details), 2)
        self.assertEqual(details[0]["ip_name"], "计算机软件著作权（7项）")
        self.assertEqual(details[0]["ip_type"], "计算机软件著作权")
        self.assertEqual(details[1]["ip_name"], "专利权（17项）")
        self.assertEqual(details[1]["ip_type"], "专利权")
        self.assertEqual(results["right_holder"]["source_path"], "basicData.title_regex")

    def test_v2_ip_summary_count_deduplicates_repeated_title_text(self):
        title = "柔锋机械科技（江苏）有限公司名下7项计算机软件著作权及17项专利权按现状整体拍卖"

        count, details = scraper.extract_ip_summary_details_from_text(f"{title}\n{title}")

        self.assertEqual(count, "24")
        self.assertEqual(len(details), 2)

    def test_v2_ip_details_use_vision_rows_from_image_tables_before_title_summary(self):
        scraper.ai_extractor = FakeVisionAIExtractor(
            {},
            [
                {
                    "sequence_no": "1",
                    "ip_name": "智能水刀切割特殊路径生成系统[简称：SJCP] V1.0",
                    "certificate_no": "软著登字第13029563号；登记号：2024SR0625690",
                    "ip_type": "软件著作权",
                    "application_date": "2024/5/10",
                    "source_excerpt": "图片表格第1行",
                },
                {
                    "sequence_no": "1",
                    "ip_name": "双心共点万向动摇机构",
                    "certificate_no": "申请号 201210378230.2",
                    "ip_type": "发明专利",
                    "application_date": "2012/9/29",
                    "patent_type": "发明专利",
                    "status": "未缴年费专利权终止，等恢复",
                    "source_excerpt": "图片表格专利第1行",
                },
            ],
        )
        parsed = extract_key_values_from_html("<p><img src='//img30.360buyimg.com/popWareDetail/example.png'/></p>")

        values, results, details = extract_special_values(
            asset_group="ip",
            parsed=parsed,
            notice_parsed=extract_key_values_from_html(""),
            core={
                "data": {
                    "basicData": {
                        "title": "柔锋机械科技（江苏）有限公司名下7项计算机软件著作权及17项专利权按现状整体拍卖"
                    }
                }
            },
            paimai_id="test-ip-image-table",
        )

        self.assertEqual(values["ip_count"], "2")
        self.assertEqual(results["ip_count"]["source_path"], "vision_ip_details_count")
        self.assertEqual(len(details), 2)
        self.assertIn("SJCP", details[0]["ip_name"])
        self.assertEqual(details[1]["certificate_no"], "申请号 201210378230.2")
        image_urls, context = scraper.ai_extractor.image_calls[0]
        self.assertEqual(image_urls, ["https://img30.360buyimg.com/popWareDetail/example.png"])
        self.assertEqual(context.image_urls, image_urls)

    def test_v2_special_images_use_media_for_real_estate_vehicle_and_land(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        core = {"data": {"imageVideoArea": {"imageList": [{"imagePath": "jfs/site-a.jpg"}]}}}
        attachments = {"files": [], "media": scraper.extract_media(core)}

        for asset_group, expected_field in (
            ("real_estate", "site_images"),
            ("vehicle", "vehicle_images"),
            ("land", "site_images"),
        ):
            values, results, _ = extract_special_values(
                asset_group=asset_group,
                parsed=parsed,
                notice_parsed=parsed,
                core=core,
                paimai_id=f"test-{asset_group}",
                attachments=attachments,
            )
            self.assertIn("jfs/site-a.jpg", values[expected_field])
            self.assertEqual(results[expected_field]["source_path"], "imageVideoArea")

    def test_v2_project_status_is_computed_from_api_status_time_and_price(self):
        now = scraper.parse_datetime_value("2026-06-30 12:00:00")

        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=3,
                signup_start_time="2026-06-29 10:00:00",
                signup_end_time="2026-06-30 10:00:00",
                start_price="100万",
                final_price="120万",
                now=now,
            ),
            "已拍出",
        )
        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=0,
                signup_start_time="2026-07-01 10:00:00",
                signup_end_time="2026-07-02 10:00:00",
                start_price="100万",
                final_price="100万",
                now=now,
            ),
            "未开始",
        )
        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=0,
                signup_start_time="2026-06-29 10:00:00",
                signup_end_time="2026-06-30 10:00:00",
                start_price="100万",
                final_price="120万",
                now=now,
            ),
            "已成交",
        )
        self.assertEqual(
            scraper.compute_project_status(
                auction_status_code=0,
                signup_start_time="2026-06-29 10:00:00",
                signup_end_time="2026-06-30 10:00:00",
                start_price="100万",
                final_price="100万",
                now=now,
            ),
            "未成交",
        )

    def test_v2_auction_stage_uses_terminal_status_before_round_code(self):
        self.assertEqual(scraper.compute_auction_stage(1, 5), "撤拍")
        self.assertEqual(scraper.compute_auction_stage(2, 7), "终止")
        self.assertEqual(scraper.compute_auction_stage(1, 0), "一拍")
        self.assertEqual(scraper.compute_auction_stage(4, 0), "变卖")

    def test_v2_schema_has_normalized_type_columns_and_field_catalog_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()

            with db.connect() as conn:
                common_columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(auction_items_common)")}
                self.assertEqual(common_columns["signup_start_time_norm"].upper(), "DATETIME")
                self.assertEqual(common_columns["start_price_amount"].upper(), "DECIMAL(18,2)")
                self.assertEqual(common_columns["assessment_amount"].upper(), "DECIMAL(18,2)")
                self.assertEqual(common_columns["assessment_date"].upper(), "DATE")
                self.assertIn("dedup_hash", common_columns)
                self.assertIn("source_platform", common_columns)

                land_columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(asset_land)")}
                self.assertEqual(land_columns["land_area_sqm"].upper(), "DECIMAL(18,2)")
                self.assertEqual(land_columns["assessment_amount"].upper(), "DECIMAL(18,2)")
                self.assertEqual(land_columns["assessment_date"].upper(), "DATE")

                real_estate_columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(asset_real_estate)")}
                self.assertEqual(real_estate_columns["building_area_sqm"].upper(), "DECIMAL(18,2)")

                debt_detail_columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(asset_debt_details)")}
                self.assertEqual(debt_detail_columns["principal_balance_amount"].upper(), "DECIMAL(18,2)")
                self.assertEqual(debt_detail_columns["interest_balance_amount"].upper(), "DECIMAL(18,2)")
                self.assertEqual(debt_detail_columns["benchmark_date_norm"].upper(), "DATE")

                ip_detail_columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(asset_ip_details)")}
                self.assertEqual(ip_detail_columns["application_date_norm"].upper(), "DATE")

                data_types = {
                    row["field_key"]: row["data_type"]
                    for row in conn.execute(
                        "SELECT field_key, data_type FROM field_catalog WHERE field_namespace='common'"
                    )
                }
                self.assertEqual(data_types["signup_start_time"], "DATETIME")
                self.assertEqual(data_types["start_price_raw"], "VARCHAR(100)")
                self.assertEqual(data_types["attachments_json"], "JSON")

                debt_household = conn.execute(
                    "SELECT data_type FROM field_catalog WHERE field_namespace='special.debt' AND field_key='household_count'"
                ).fetchone()
                self.assertEqual(debt_household["data_type"], "INTEGER")

    def test_v2_dedup_hash_is_stable_for_normalized_same_asset_identity(self):
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

    def test_v2_common_upsert_writes_normalized_values_and_dedup_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()

            common_values = {
                "asset_type": "real_estate",
                "asset_location": "Suzhou Industrial Park Donghu Garden 136-1001",
                "project_status": "running",
                "auction_stage": "first",
                "project_name": "Donghu Garden 136-1001",
                "signup_start_time": "2026-06-29 10:00:00",
                "signup_end_time": "2026-06-30 10:00:00",
                "start_price_raw": "2,200,000 yuan",
                "final_price_raw": "3,142,750 yuan",
                "assessment_price_time": "market price: 3,142,750 yuan",
            }
            special_values = {
                "right_certificate_no": "00448472",
                "building_area": "137.22 sqm",
            }

            db.upsert_common_item(
                paimai_id="310747971",
                batch_id="batch1",
                asset_group="real_estate",
                jd_category_id="101",
                jd_category_name="real estate",
                values=common_values,
                field_results={},
                special_values=special_values,
            )

            with db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT source_platform, source_item_id, signup_start_time_norm,
                           signup_end_time_norm, start_price_amount, final_price_amount,
                           assessment_price_amount, assessment_amount, assessment_date, dedup_hash
                    FROM auction_items_common
                    WHERE paimai_id='310747971'
                    """
                ).fetchone()
                self.assertEqual(row["source_platform"], "jd")
                self.assertEqual(row["source_item_id"], "310747971")
                self.assertEqual(row["signup_start_time_norm"], "2026-06-29 10:00:00")
                self.assertEqual(row["signup_end_time_norm"], "2026-06-30 10:00:00")
                self.assertEqual(Decimal(str(row["start_price_amount"])), Decimal("2200000.00"))
                self.assertEqual(Decimal(str(row["final_price_amount"])), Decimal("3142750.00"))
                self.assertEqual(Decimal(str(row["assessment_price_amount"])), Decimal("3142750.00"))
                self.assertEqual(Decimal(str(row["assessment_amount"])), Decimal("3142750.00"))
                self.assertIsNone(row["assessment_date"])
                self.assertEqual(len(row["dedup_hash"]), 16)

                dedup_row = conn.execute(
                    """
                    SELECT source_platform, source_item_id, paimai_id, dedup_hash,
                           identity_basis_json
                    FROM asset_dedup_index
                    WHERE source_platform='jd' AND source_item_id='310747971'
                    """
                ).fetchone()
                self.assertEqual(dedup_row["paimai_id"], "310747971")
                self.assertEqual(dedup_row["dedup_hash"], row["dedup_hash"])
                self.assertIn("building_area", dedup_row["identity_basis_json"])

    def test_v2_special_upserts_write_normalized_area_and_assessment_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()
            db.seed_field_catalog()

            db.upsert_special_item(
                paimai_id="land-1",
                asset_group="land",
                values={
                    "land_area": "34739.27平方米",
                    "assessment_time_value": "评估价：¥110,824,446；评估基准日：2026年6月30日",
                },
                field_results={},
            )
            db.upsert_special_item(
                paimai_id="house-1",
                asset_group="real_estate",
                values={"building_area": "137.22㎡"},
                field_results={},
            )

            with db.connect() as conn:
                land = conn.execute(
                    "SELECT land_area, land_area_sqm, assessment_amount, assessment_date FROM asset_land WHERE paimai_id='land-1'"
                ).fetchone()
                self.assertEqual(land["land_area"], "34739.27平方米")
                self.assertEqual(Decimal(str(land["land_area_sqm"])), Decimal("34739.27"))
                self.assertEqual(Decimal(str(land["assessment_amount"])), Decimal("110824446.00"))
                self.assertEqual(land["assessment_date"], "2026-06-30")

                house = conn.execute(
                    "SELECT building_area, building_area_sqm FROM asset_real_estate WHERE paimai_id='house-1'"
                ).fetchone()
                self.assertEqual(house["building_area"], "137.22㎡")
                self.assertEqual(Decimal(str(house["building_area_sqm"])), Decimal("137.22"))

    def test_v2_detail_tables_write_normalized_money_and_date_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()

            db.upsert_debt_details(
                paimai_id="debt-1",
                details=[
                    {
                        "sequence_no": "1",
                        "debtor_name": "测试公司",
                        "principal_balance": "650万元",
                        "interest_balance": "2,599,759.59元",
                        "recovery_fee": "0",
                        "claim_total": "13,401,277.95元",
                        "benchmark_date": "基准日：2025年11月11日",
                    }
                ],
            )
            db.upsert_ip_details(
                paimai_id="ip-1",
                details=[
                    {
                        "sequence_no": "1",
                        "ip_name": "测试软件",
                        "certificate_no": "软著登字第001号",
                        "ip_type": "软件著作权",
                        "application_date": "登记批准日期：2024/5/10",
                    }
                ],
            )

            with db.connect() as conn:
                debt = conn.execute(
                    """
                    SELECT principal_balance_amount, interest_balance_amount, recovery_fee_amount,
                           claim_total_amount, benchmark_date_norm
                    FROM asset_debt_details
                    WHERE paimai_id='debt-1'
                    """
                ).fetchone()
                self.assertEqual(Decimal(str(debt["principal_balance_amount"])), Decimal("6500000.00"))
                self.assertEqual(Decimal(str(debt["interest_balance_amount"])), Decimal("2599759.59"))
                self.assertEqual(Decimal(str(debt["recovery_fee_amount"])), Decimal("0.00"))
                self.assertEqual(Decimal(str(debt["claim_total_amount"])), Decimal("13401277.95"))
                self.assertEqual(debt["benchmark_date_norm"], "2025-11-11")

                ip = conn.execute(
                    "SELECT application_date, application_date_norm FROM asset_ip_details WHERE paimai_id='ip-1'"
                ).fetchone()
                self.assertEqual(ip["application_date"], "登记批准日期：2024/5/10")
                self.assertEqual(ip["application_date_norm"], "2024-05-10")

    def test_v2_batch_summary_json_keeps_parameters_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            db = scraper.JDScraperDatabase(db_path)
            db.init_schema()

            batch_id = db.start_batch({"per_category_limit": 8})
            db.finish_batch(batch_id, "success", "done")

            stats = db.get_batch_stats(batch_id)
            self.assertEqual(stats["parameters"]["per_category_limit"], 8)
            self.assertEqual(stats["status"], "success")
            self.assertEqual(stats["message"], "done")

    def test_v2_ip_details_are_normalized_as_one_to_many_rows(self):
        details = scraper.normalize_ai_ip_details(
            [
                {
                    "sequence_no": "1",
                    "ip_name": "测试软件",
                    "certificate_no": "软著登字第001号",
                    "ip_type": "软件著作权",
                    "application_date": "2024年5月1日",
                    "status": "有效",
                    "source_excerpt": "测试软件 软著登字第001号",
                }
            ]
        )

        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["ip_name"], "测试软件")
        self.assertEqual(details[0]["certificate_no"], "软著登字第001号")

    def test_v2_ip_ai_definition_requires_row_level_details_not_summary(self):
        definition = scraper.FIELD_DEFINITIONS["ip_details"]["description"]

        self.assertIn("逐行", definition)
        self.assertIn("禁止", definition)
        self.assertIn("7项软件著作权", definition)
        self.assertIn("17项专利权", definition)

    def test_v2_ip_vision_prompt_handles_multiple_image_tables(self):
        extractor = AIFieldExtractor()
        prompt = extractor._build_ip_vision_prompt(
            ["https://example.com/a.jpg"],
            AIExtractionContext(
                detail_text="软件著作权表和专利表都在图片里",
                notice_text="",
                asset_group="ip",
                paimai_id="ip-prompt",
            ),
        )

        self.assertIn("多个表格", prompt)
        self.assertIn("两个表", prompt)
        self.assertIn("逐行", prompt)
        self.assertIn("禁止", prompt)

    def test_v2_right_holder_ai_value_rejects_asset_name_substitution(self):
        values = {"project_name": "苹果手机一部", "goods_name": "苹果手机一部"}

        self.assertTrue(scraper.right_holder_looks_like_asset_name("苹果手机一部", values))
        self.assertFalse(scraper.right_holder_looks_like_asset_name("张三", values))

    def test_v2_land_area_keeps_unit_and_assessment_reads_notice_text(self):
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "land_area": {
                    "value": "34739.27",
                    "source_text": "土地证载面积：34739.27平方米",
                },
                "assessment_time_value": {
                    "value": "评估价：¥110,824,446",
                    "source_text": "评估价：¥110,824,446",
                },
            }
        )
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(
            "<p>土地证载面积：34739.27平方米，证载建筑面积：13823.1平方米。评估价：¥110,824,446。</p>"
        )

        values, _, _ = extract_special_values(
            asset_group="land",
            parsed=parsed,
            notice_parsed=notice_parsed,
            core={"data": {"basicData": {"title": "土地项目"}}},
            paimai_id="test-land",
            attachments={"files": [], "media": []},
        )

        self.assertEqual(values["land_area"], "34739.27平方米")
        self.assertIn("110,824,446", values["assessment_time_value"])

    def test_v2_land_assessment_special_falls_back_to_common_assessment(self):
        common_values = {"assessment_price_time": "1.10824446亿"}
        common_results = {
            "assessment_price_time": field_result_value(
                "1.10824446亿",
                "list_json",
                "assessmentPriceCN",
                "1.10824446亿",
            )
        }
        special_values = {}
        special_results = {}

        sync_common_special_values("land", common_values, common_results, special_values, special_results)

        self.assertEqual(special_values["assessment_time_value"], "1.10824446亿")
        self.assertIn("common.assessment_price_time", special_results["assessment_time_value"]["source_path"])

    def test_v2_common_times_fall_back_to_notice_auction_range_without_ai(self):
        scraper.ai_extractor = None
        notice_parsed = extract_key_values_from_html(
            """
            <p>登记日期：2012-11-01。使用截止期限：2073-04-06止。</p>
            <p>第一次拍卖竞价时间：2026年6月29日10时至2026年6月30日10时止。
            根据法律规定，法院有权在拍卖开始前、拍卖过程中中止拍卖。</p>
            """
        )

        values, results = extract_common_values(
            category=JDCategory("101", "住宅用房"),
            asset_group="real_estate",
            list_item={"auctionStatus": 1, "paimaiTimes": 1, "title": "测试房产"},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试房产",
                            "startPrice": 2200000,
                            "currentPrice": 3058000,
                        }
                    }
                },
                "realtime": {"data": {"auctionStatus": 1}},
                "attachments": [],
                "vendor": {},
            },
            parsed=extract_key_values_from_html(""),
            notice_parsed=notice_parsed,
            paimai_id="test-auction-range",
        )

        self.assertEqual(values["signup_start_time"], "2026-06-29 10:00:00")
        self.assertEqual(values["signup_end_time"], "2026-06-30 10:00:00")
        self.assertEqual(results["signup_start_time"]["source_path"], "notice_html.auction_time_range")

    def test_v2_common_assessment_accepts_market_price_when_assessment_missing(self):
        values, results = extract_common_values(
            category=JDCategory("101", "住宅用房"),
            asset_group="real_estate",
            list_item={
                "auctionStatus": 1,
                "paimaiTimes": 1,
                "title": "测试房产",
                "marketPriceCN": "314.275万",
                "assessmentPriceCN": "0",
            },
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试房产",
                            "assessmentPrice": 0,
                            "judicatureBasicInfoResult": {"marketPrice": 3142750.0},
                        }
                    }
                },
                "realtime": {"data": {"auctionStatus": 1}},
                "attachments": [],
                "vendor": {},
            },
            parsed=extract_key_values_from_html(""),
            notice_parsed=extract_key_values_from_html(""),
            paimai_id="test-market-price",
        )

        self.assertEqual(values["assessment_price_time"], "市场价：314.275万")
        self.assertIn("marketPrice", results["assessment_price_time"]["source_path"])

    def test_v2_common_disposal_party_prefers_explicit_disposal_unit(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")
        notice_parsed = extract_key_values_from_html(
            "<p>沈阳国大基业房地产开发有限公司管理人将于2026年6月30日10时至2026年7月1日10时止"
            "（延时除外）在京东拍卖破产强清平台（处置单位：沈阳国大基业房地产开发有限公司管理人，"
            "监督单位：沈阳市沈北新区人民法院）进行公开拍卖活动。</p>"
        )

        values, results = extract_common_values(
            category=JDCategory("105", "车辆"),
            asset_group="vehicle",
            list_item={"shopName": "北京隆安（沈阳）律师事务所"},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试车辆",
                            "shopName": "北京隆安（沈阳）律师事务所",
                        }
                    }
                },
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {"data": {"orgName": "北京隆安（沈阳）律师事务所"}},
            },
            parsed=parsed,
            notice_parsed=notice_parsed,
            paimai_id="test-disposal-party",
        )

        self.assertEqual(values["disposal_party"], "沈阳国大基业房地产开发有限公司管理人")
        self.assertEqual(results["disposal_party"]["source_path"], "explicit_disposal_party")

    def test_v2_common_disposal_party_uses_upload_organization_before_shop_name(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        parsed = extract_key_values_from_html("")

        values, results = extract_common_values(
            category=JDCategory("105", "车辆"),
            asset_group="vehicle",
            list_item={"shopName": "北京隆安（沈阳）律师事务所"},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试车辆",
                            "shopName": "北京隆安（沈阳）律师事务所",
                        }
                    }
                },
                "product_basic": {
                    "data": {
                        "uploadOrganization": "沈阳国大基业房地产开发有限公司管理人",
                    }
                },
                "realtime": {"data": {}},
                "attachments": [],
                "vendor": {"data": {"orgName": "北京隆安（沈阳）律师事务所"}},
            },
            parsed=parsed,
            notice_parsed=parsed,
            paimai_id="test-upload-organization",
        )

        self.assertEqual(values["disposal_party"], "沈阳国大基业房地产开发有限公司管理人")
        self.assertEqual(results["disposal_party"]["source_path"], "product_basic.uploadOrganization")

    def test_v2_real_estate_use_term_falls_back_from_notice_text(self):
        scraper.ai_extractor = FakeBatchAIExtractor({})
        notice_parsed = extract_key_values_from_html(
            "<p>房屋产权证号：00448472。使用截止期限：2073-04-06止。土地用途：住宅用地。</p>"
        )

        values, results, _ = extract_special_values(
            asset_group="real_estate",
            parsed=extract_key_values_from_html(""),
            notice_parsed=notice_parsed,
            core={"data": {"basicData": {"title": "测试房产"}}},
            paimai_id="test-use-term",
            attachments={"files": [], "media": []},
        )

        self.assertEqual(values["use_term"], "2073-04-06止")
        self.assertEqual(results["use_term"]["source_path"], "text.use_term")

    def test_v2_project_status_stays_bidding_when_realtime_remain_time_positive_after_original_end(self):
        now = scraper.dt.datetime.now()
        original_start = now - scraper.dt.timedelta(days=1)
        original_end = now - scraper.dt.timedelta(minutes=1)
        extended_end = now + scraper.dt.timedelta(hours=1)
        source_text = (
            f"第一次拍卖竞价时间：{original_start.year}年{original_start.month}月{original_start.day}日10时"
            f"至{original_end.year}年{original_end.month}月{original_end.day}日10时止（延时除外）。"
        )
        scraper.ai_extractor = FakeBatchAIExtractor(
            {
                "signup_start_time": {
                    "value": f"{original_start.year}年{original_start.month}月{original_start.day}日",
                    "source_text": source_text,
                },
                "signup_end_time": {
                    "value": f"{original_end.year}年{original_end.month}月{original_end.day}日",
                    "source_text": source_text,
                },
            }
        )

        values, _ = extract_common_values(
            category=JDCategory("101", "住宅用房"),
            asset_group="real_estate",
            list_item={"auctionStatus": 1, "paimaiTimes": 1, "title": "测试房产"},
            bundle={
                "core": {
                    "data": {
                        "basicData": {
                            "title": "测试房产",
                            "startPrice": 2200000,
                            "startTime": int(original_start.timestamp() * 1000),
                            "endTime": int(extended_end.timestamp() * 1000),
                            "delayedTime": 5,
                        }
                    }
                },
                "realtime": {
                    "data": {
                        "auctionStatus": 1,
                        "currentPrice": 3058000,
                        "startPrice": 2200000,
                        "endTime": int(extended_end.timestamp() * 1000),
                        "remainTime": 3600,
                        "delayedCount": 3,
                    }
                },
                "attachments": [],
                "vendor": {},
            },
            parsed=extract_key_values_from_html(""),
            notice_parsed=extract_key_values_from_html(source_text),
            paimai_id="test-delayed-status",
        )

        self.assertEqual(values["project_status"], "竞价中")

    def test_v2_auction_stage_infers_first_round_from_title_when_code_is_zero(self):
        values, _ = extract_common_values(
            category=JDCategory("118", "奢侈品"),
            asset_group="goods",
            list_item={"auctionStatus": 1, "paimaiTimes": 0, "title": "GC-NM-64 足金金条100g（第一次）"},
            bundle={
                "core": {"data": {"basicData": {"title": "GC-NM-64 足金金条100g（第一次）", "paimaiTimes": 0}}},
                "realtime": {"data": {"auctionStatus": 1}},
                "attachments": [],
                "vendor": {},
            },
            parsed=extract_key_values_from_html(""),
            notice_parsed=extract_key_values_from_html(""),
            paimai_id="test-stage-title",
        )

        self.assertEqual(values["auction_stage"], "一拍")


if __name__ == "__main__":
    unittest.main()
