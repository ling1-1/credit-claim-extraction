"""
字段标准化引擎
统一金额、日期、面积、电话等字段的输出格式
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .logger import get_logger

logger = get_logger()


@dataclass
class StandardizedMoney:
    """标准化后的金额结果"""
    raw_value: str
    numeric: Decimal | None
    unit: str  # 元, 万元, 亿元
    currency: str  # CNY, USD 等
    display: str  # 格式化显示字符串
    confidence: float  # 标准化置信度 0.0-1.0


@dataclass
class StandardizedDate:
    """标准化后的日期结果"""
    raw_value: str
    iso_date: str | None  # YYYY-MM-DD
    iso_datetime: str | None  # YYYY-MM-DD HH:MM:SS
    year: int | None
    month: int | None
    day: int | None
    display: str
    confidence: float


@dataclass
class StandardizedArea:
    """标准化后的面积结果"""
    raw_value: str
    numeric: Decimal | None
    unit: str  # ㎡, 平方米, 亩, 公顷
    sqm_equivalent: Decimal | None  # 换算为平方米
    display: str
    confidence: float


@dataclass
class StandardizedPhone:
    """标准化后的电话结果"""
    raw_value: str
    pure_number: str  # 纯数字，不含分隔符
    country_code: str  # +86
    phone_type: str  # mobile, landline, unknown
    display: str
    confidence: float


class MoneyStandardizer:
    """金额标准化器"""

    # 金额单位映射（按长度降序排列，避免"元"提前匹配到"万元"）
    UNIT_MAPPING = {
        "人民币元": "元",
        "万亿元": "万亿元",
        "亿元": "亿元",
        "万元": "万元",
        "人民币": "元",
        "CNY": "元",
        "圆": "元",
        "元": "元",
        "亿": "亿元",
        "万": "万元",
        "w": "万元",
        "W": "万元",
    }

    # 货币符号映射
    CURRENCY_MAPPING = {
        "¥": "CNY",
        "￥": "CNY",
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "CNY": "CNY",
        "USD": "USD",
        "人民币": "CNY",
    }

    # 金额提取正则
    MONEY_PATTERNS = [
        r"([¥￥$€£]?)\s*([\d,]+\.?\d*)\s*([万亿万元圆wW]?)",  # 标准格式
        r"([\d,]+\.?\d*)\s*([万亿万元圆wW]?)",  # 只有数字和单位
    ]

    @classmethod
    def standardize(cls, value: Any) -> StandardizedMoney:
        """标准化金额"""
        raw = str(value) if value is not None else ""
        if not raw.strip():
            return StandardizedMoney(
                raw_value=raw,
                numeric=None,
                unit="元",
                currency="CNY",
                display="",
                confidence=0.0,
            )

        numeric: Decimal | None = None
        unit = "元"
        currency = "CNY"
        confidence = 0.0

        # 检测货币符号
        for symbol, curr in cls.CURRENCY_MAPPING.items():
            if symbol in raw:
                currency = curr
                break

        # 尝试提取纯数字
        clean_value = re.sub(r"[^\d.]", "", raw)
        if clean_value:
            try:
                numeric = Decimal(clean_value.replace(",", ""))
                confidence = 0.9
            except InvalidOperation:
                pass

        # 检测单位
        for raw_unit, std_unit in cls.UNIT_MAPPING.items():
            if raw_unit in raw:
                unit = std_unit
                break

        # 单位换算
        if numeric is not None:
            if unit == "万亿元":
                numeric = numeric * Decimal("1000000000000")
                unit = "元"
            elif unit == "万元":
                numeric = numeric * Decimal("10000")
                unit = "元"
            elif unit == "亿元":
                numeric = numeric * Decimal("100000000")
                unit = "元"

        # 格式化显示（统一以"元"为单位）   
        display = ""
        if numeric is not None:
            display = f"{numeric:,.2f} 元"


        return StandardizedMoney(
            raw_value=raw,
            numeric=numeric,
            unit=unit,
            currency=currency,
            display=display,
            confidence=confidence,
        )


class DateStandardizer:
    """日期标准化器"""

    # 常见日期格式
    DATE_FORMATS = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M:%S",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
        "%Y%m%d",
    ]

    # 中文数字映射
    CHINESE_NUMBERS = {
        "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
        "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
        "十": "10", "十一": "11", "十二": "12",
    }

    @classmethod
    def standardize(cls, value: Any) -> StandardizedDate:
        """标准化日期"""
        raw = str(value) if value is not None else ""
        if not raw.strip():
            return StandardizedDate(
                raw_value=raw,
                iso_date=None,
                iso_datetime=None,
                year=None,
                month=None,
                day=None,
                display="",
                confidence=0.0,
            )

        # 清理中文日期格式
        cleaned = cls._clean_chinese_date(raw)

        parsed_dt: datetime | None = None
        confidence = 0.0

        # 尝试各种格式解析
        for fmt in cls.DATE_FORMATS:
            try:
                parsed_dt = datetime.strptime(cleaned, fmt)
                confidence = 0.95 if "%H" in fmt else 0.9
                break
            except ValueError:
                continue

        # 正则提取年月日
        if parsed_dt is None:
            match = re.search(r"(\d{4})[^\d]?(\d{1,2})?[^\d]?(\d{1,2})?", cleaned)
            if match:
                try:
                    year = int(match.group(1))
                    month = int(match.group(2)) if match.group(2) else 1
                    day = int(match.group(3)) if match.group(3) else 1
                    parsed_dt = datetime(year, month, day)
                    confidence = 0.7
                except (ValueError, TypeError):
                    pass

        if parsed_dt:
            return StandardizedDate(
                raw_value=raw,
                iso_date=parsed_dt.strftime("%Y-%m-%d"),
                iso_datetime=parsed_dt.strftime("%Y-%m-%d %H:%M:%S"),
                year=parsed_dt.year,
                month=parsed_dt.month,
                day=parsed_dt.day,
                display=parsed_dt.strftime("%Y-%m-%d"),
                confidence=confidence,
            )

        return StandardizedDate(
            raw_value=raw,
            iso_date=None,
            iso_datetime=None,
            year=None,
            month=None,
            day=None,
            display=raw,
            confidence=0.0,
        )

    @classmethod
    def _clean_chinese_date(cls, text: str) -> str:
        """清理中文日期格式"""
        for cn, num in cls.CHINESE_NUMBERS.items():
            text = text.replace(cn, num)
        return text


class AreaStandardizer:
    """面积标准化器"""

    # 面积单位换算系数（换算为平方米）
    UNIT_CONVERSION = {
        "㎡": Decimal("1"),
        "m²": Decimal("1"),
        "平方米": Decimal("1"),
        "平米": Decimal("1"),
        "平": Decimal("1"),
        "亩": Decimal("666.67"),
        "公顷": Decimal("10000"),
        "km²": Decimal("1000000"),
        "平方公里": Decimal("1000000"),
    }

    @classmethod
    def standardize(cls, value: Any) -> StandardizedArea:
        """标准化面积"""
        raw = str(value) if value is not None else ""
        if not raw.strip():
            return StandardizedArea(
                raw_value=raw,
                numeric=None,
                unit="㎡",
                sqm_equivalent=None,
                display="",
                confidence=0.0,
            )

        numeric: Decimal | None = None
        unit = "㎡"
        confidence = 0.0

        # 提取数字
        match = re.search(r"([\d,]+\.?\d*)", raw)
        if match:
            try:
                numeric = Decimal(match.group(1).replace(",", ""))
                confidence = 0.85
            except InvalidOperation:
                pass

        # 检测单位
        for unit_name in cls.UNIT_CONVERSION.keys():
            if unit_name in raw:
                unit = unit_name
                break

        # 换算为平方米
        sqm_equivalent = None
        if numeric is not None:
            conversion = cls.UNIT_CONVERSION.get(unit, Decimal("1"))
            sqm_equivalent = numeric * conversion

        # 格式化显示
        display = ""
        if numeric is not None:
            display = f"{numeric:,.2f} {unit}"
            if sqm_equivalent is not None and unit != "㎡":
                display += f"（约 {sqm_equivalent:,.2f} 平方米）"

        return StandardizedArea(
            raw_value=raw,
            numeric=numeric,
            unit=unit,
            sqm_equivalent=sqm_equivalent,
            display=display,
            confidence=confidence,
        )


class PhoneStandardizer:
    """电话号码标准化器"""

    # 中国大陆手机号正则
    MOBILE_PATTERN = r"1[3-9]\d{9}"
    # 固定电话正则（带区号）
    LANDLINE_PATTERN = r"0\d{2,3}-?\d{7,8}"
    PHONE_PATTERN = r"0\d{2,3}-?\d{7,8}|1[3-9]\d{9}"

    @classmethod
    def _format_number(cls, number: str) -> tuple[str, str]:
        digits = re.sub(r"[^\d]", "", number)
        if re.fullmatch(cls.MOBILE_PATTERN, digits):
            return f"{digits[:3]} {digits[3:7]} {digits[7:]}", "mobile"
        if re.fullmatch(cls.LANDLINE_PATTERN, digits):
            if len(digits) == 11:
                return f"{digits[:3]}-{digits[3:]}", "landline"
            if len(digits) == 12:
                return f"{digits[:4]}-{digits[4:]}", "landline"
            return digits, "landline"
        return digits, "unknown"

    @classmethod
    def standardize(cls, value: Any) -> StandardizedPhone:
        """标准化电话"""
        raw = str(value) if value is not None else ""
        if not raw.strip():
            return StandardizedPhone(
                raw_value=raw,
                pure_number="",
                country_code="+86",
                phone_type="unknown",
                display="",
                confidence=0.0,
            )

        matches = re.findall(cls.PHONE_PATTERN, raw)
        if matches:
            formatted = []
            pure_numbers = []
            phone_types = []
            for match in matches:
                display_value, phone_type = cls._format_number(match)
                digits = re.sub(r"[^\d]", "", match)
                if digits not in pure_numbers:
                    pure_numbers.append(digits)
                    formatted.append(display_value)
                    phone_types.append(phone_type)

            if len(pure_numbers) > 1:
                return StandardizedPhone(
                    raw_value=raw,
                    pure_number=";".join(pure_numbers),
                    country_code="+86",
                    phone_type="multiple",
                    display="; ".join(formatted),
                    confidence=0.95,
                )

            if len(pure_numbers) == 1:
                return StandardizedPhone(
                    raw_value=raw,
                    pure_number=pure_numbers[0],
                    country_code="+86",
                    phone_type=phone_types[0],
                    display=formatted[0],
                    confidence=0.95 if phone_types[0] == "mobile" else 0.9,
                )

        # 提取纯数字
        pure_number = re.sub(r"[^\d]", "", raw)

        # 判断类型
        phone_type = "unknown"
        confidence = 0.0

        if re.fullmatch(cls.MOBILE_PATTERN, pure_number):
            phone_type = "mobile"
            confidence = 0.95
        elif re.fullmatch(cls.LANDLINE_PATTERN, pure_number):
            phone_type = "landline"
            confidence = 0.9

        # 格式化显示
        display = pure_number
        if phone_type == "mobile" and len(pure_number) == 11:
            display = f"{pure_number[:3]} {pure_number[3:7]} {pure_number[7:]}"
        elif phone_type == "landline" and len(pure_number) >= 10:
            display = f"{pure_number[:3]}-{pure_number[3:]}"

        return StandardizedPhone(
            raw_value=raw,
            pure_number=pure_number,
            country_code="+86",
            phone_type=phone_type,
            display=display,
            confidence=confidence,
        )


class FieldStandardizer:
    """字段标准化器 - 统一入口"""

    @staticmethod
    def money(value: Any) -> StandardizedMoney:
        """标准化金额"""
        return MoneyStandardizer.standardize(value)

    @staticmethod
    def date(value: Any) -> StandardizedDate:
        """标准化日期"""
        return DateStandardizer.standardize(value)

    @staticmethod
    def area(value: Any) -> StandardizedArea:
        """标准化面积"""
        return AreaStandardizer.standardize(value)

    @staticmethod
    def phone(value: Any) -> StandardizedPhone:
        """标准化电话"""
        return PhoneStandardizer.standardize(value)

    @staticmethod
    def auto_detect(value: str, field_name: str | None = None) -> Any:
        """根据字段名自动检测并标准化"""
        if not value:
            return value

        field_lower = (field_name or "").lower()

        # 根据字段名选择标准化器
        if any(keyword in field_lower for keyword in ["price", "amount", "money", "金额", "价格"]):
            return FieldStandardizer.money(value)
        elif any(keyword in field_lower for keyword in ["date", "time", "日期", "时间"]):
            return FieldStandardizer.date(value)
        elif any(keyword in field_lower for keyword in ["area", "size", "面积", "大小"]):
            return FieldStandardizer.area(value)
        elif any(keyword in field_lower for keyword in ["phone", "tel", "contact", "电话", "手机"]):
            return FieldStandardizer.phone(value)

        return value
