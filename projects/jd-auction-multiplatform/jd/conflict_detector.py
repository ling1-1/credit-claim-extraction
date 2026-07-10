"""
多来源冲突检测器
比较 API / HTML规则 / AI 三个来源的结果，检测并记录冲突
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .field_standardizer import FieldStandardizer
from .logger import get_logger

logger = get_logger()


class ConflictSeverity(Enum):
    """冲突严重程度"""
    NONE = "none"  # 无冲突
    LOW = "low"  # 轻微差异（格式不同，但值相同）
    MEDIUM = "medium"  # 中等差异（值在合理范围内波动
    HIGH = "high"  # 严重差异（值完全不同）


SEVERITY_RANK = {
    ConflictSeverity.NONE: 0,
    ConflictSeverity.LOW: 1,
    ConflictSeverity.MEDIUM: 2,
    ConflictSeverity.HIGH: 3,
}


class SourceType(Enum):
    """数据来源类型"""
    API = "api"  # 结构化 API 数据
    HTML_RULE = "html_rule"  # HTML 规则提取
    AI = "ai"  # AI 提取结果


@dataclass
class FieldValue:
    """单来源字段值"""
    source: SourceType
    value: Any
    confidence: float = 0.0
    raw_context: Optional[str] = None


@dataclass
class ConflictDetail:
    """冲突详情"""
    field_key: str
    field_label: str
    severity: ConflictSeverity
    values: list[FieldValue] = field(default_factory=list)
    recommended_value: Any = None
    recommendation_reason: str = ""
    needs_review: bool = False


@dataclass
class ConflictReport:
    """冲突报告"""
    paimai_id: str
    total_fields: int = 0
    conflict_count: int = 0
    high_severity_count: int = 0
    medium_severity_count: int = 0
    low_severity_count: int = 0
    conflicts: list[ConflictDetail] = field(default_factory=list)
    needs_human_review_count: int = 0


class ValueComparator:
    """值比较器"""

    @staticmethod
    def compare_numbers(v1: Any, v2: Any) -> ConflictSeverity:
        """比较数值差异"""
        try:
            n1 = float(str(v1)) if v1 else None
            n2 = float(str(v2)) if v2 else None
            if n1 is None and n2 is None:
                return ConflictSeverity.NONE
            if n1 is None or n2 is None:
                return ConflictSeverity.HIGH

            if abs(n1 - n2) < 0.01:
                return ConflictSeverity.NONE

            # 计算相对差异
            max_val = max(abs(n1), abs(n2))
            if max_val == 0:
                return ConflictSeverity.NONE

            relative_diff = abs(n1 - n2) / max_val
            if relative_diff < 0.01:  # 1% 以内差异
                return ConflictSeverity.LOW
            elif relative_diff < 0.1:  # 10% 以内差异
                return ConflictSeverity.MEDIUM
            else:
                return ConflictSeverity.HIGH
        except (ValueError, TypeError):
            return ConflictSeverity.HIGH

    @staticmethod
    def compare_strings(v1: Any, v2: Any) -> ConflictSeverity:
        """比较字符串差异"""
        s1 = str(v1).strip() if v1 else ""
        s2 = str(v2).strip() if v2 else ""

        if s1 == s2:
            return ConflictSeverity.NONE

        # 标准化后比较
        def normalize(s: str) -> str:
            import re
            s = re.sub(r"\s+", "", s)
            s = re.sub(r"[，,。；;、]", "", s)
            return s.lower()

        s1_norm = normalize(s1)
        s2_norm = normalize(s2)

        if s1_norm == s2_norm:
            return ConflictSeverity.LOW

        # 计算编辑距离
        if len(s1_norm) == 0 or len(s2_norm) == 0:
            return ConflictSeverity.HIGH

        distance = ValueComparator._levenshtein_distance(s1_norm, s2_norm)
        max_len = max(len(s1_norm), len(s2_norm))
        similarity = 1 - (distance / max_len)

        if similarity > 0.9:
            return ConflictSeverity.LOW
        elif similarity > 0.7:
            return ConflictSeverity.MEDIUM
        else:
            return ConflictSeverity.HIGH

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """计算编辑距离"""
        if len(s1) < len(s2):
            return ValueComparator._levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        previous_row = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        return previous_row[-1]

    @staticmethod
    def compare_dates(v1: Any, v2: Any) -> ConflictSeverity:
        """比较日期差异"""
        d1 = FieldStandardizer.date(v1)
        d2 = FieldStandardizer.date(v2)

        if d1.iso_date == d2.iso_date:
            return ConflictSeverity.NONE
        if d1.iso_date is None or d2.iso_date is None:
            return ConflictSeverity.HIGH
        return ConflictSeverity.MEDIUM

    @staticmethod
    def compare_money(v1: Any, v2: Any) -> ConflictSeverity:
        """比较金额差异"""
        m1 = FieldStandardizer.money(v1)
        m2 = FieldStandardizer.money(v2)

        if m1.numeric == m2.numeric:
            return ConflictSeverity.NONE
        if m1.numeric is None or m2.numeric is None:
            return ConflictSeverity.HIGH

        # 金额差异判断
        return ValueComparator.compare_numbers(float(m1.numeric), float(m2.numeric))


class ConflictDetector:
    """冲突检测器"""

    def __init__(self) -> None:
        self.comparator = ValueComparator()

    def detect_field_conflict(
        self,
        field_key: str,
        field_label: str,
        api_value: Any = None,
        html_value: Any = None,
        ai_value: Any = None,
        field_type: Optional[str] = None,
    ) -> ConflictDetail:
        """检测单个字段的多来源冲突"""
        values: list[FieldValue] = []

        if api_value is not None:
            values.append(FieldValue(source=SourceType.API, value=api_value, confidence=0.9))
        if html_value is not None:
            values.append(FieldValue(source=SourceType.HTML_RULE, value=html_value, confidence=0.7))
        if ai_value is not None:
            values.append(FieldValue(source=SourceType.AI, value=ai_value, confidence=0.85))

        # 只有一个来源或无冲突
        if len(values) <= 1:
            return ConflictDetail(
                field_key=field_key,
                field_label=field_label,
                severity=ConflictSeverity.NONE,
                values=values,
                recommended_value=values[0].value if values else None,
                needs_review=False,
            )

        # 比较所有值对
        max_severity = ConflictSeverity.NONE
        all_values = [v.value for v in values]

        # 根据字段类型选择比较器
        comparator = self._get_comparator(field_type, field_key)

        for i in range(len(all_values)):
            for j in range(i + 1, len(all_values)):
                severity = comparator(all_values[i], all_values[j])
                if SEVERITY_RANK[severity] > SEVERITY_RANK[max_severity]:
                    max_severity = severity

        # 选择推荐值（按置信度加权）
        sorted_values = sorted(values, key=lambda x: x.confidence, reverse=True)
        recommended = sorted_values[0].value
        reason = f"选择 {sorted_values[0].source.value} (置信度 {sorted_values[0].confidence})"

        # 需要人工审核的情况
        needs_review = max_severity in (ConflictSeverity.MEDIUM, ConflictSeverity.HIGH)

        return ConflictDetail(
            field_key=field_key,
            field_label=field_label,
            severity=max_severity,
            values=values,
            recommended_value=recommended,
            recommendation_reason=reason,
            needs_review=needs_review,
        )

    def _get_comparator(self, field_type: Optional[str], field_key: str) -> Callable:
        """根据字段类型获取比较器"""
        field_lower = field_key.lower() if field_key else ""

        # 金额类字段
        if field_type == "money" or any(keyword in field_lower for keyword in ["price", "amount", "money", "金额", "价格"]):
            return self.comparator.compare_money

        # 日期类字段
        elif field_type == "date" or any(keyword in field_lower for keyword in ["date", "time", "日期", "时间"]):
            return self.comparator.compare_dates

        # 数值类字段
        elif field_type == "number" or any(keyword in field_lower for keyword in ["area", "size", "count", "面积", "数量"]):
            return self.comparator.compare_numbers

        # 默认字符串比较
        return self.comparator.compare_strings

    def detect_all_conflicts(
        self,
        paimai_id: str,
        api_values: dict[str, Any],
        html_values: dict[str, Any],
        ai_values: dict[str, Any] | None = None,
        field_types: dict[str, str] | None = None,
    ) -> ConflictReport:
        """检测所有字段的冲突"""
        report = ConflictReport(paimai_id=paimai_id)
        field_types = field_types or {}

        # 收集所有字段
        all_fields = set(api_values.keys()) | set(html_values.keys())
        if ai_values:
            all_fields |= set(ai_values.keys())

        report.total_fields = len(all_fields)

        for field_key in all_fields:
            field_type = field_types.get(field_key)
            conflict = self.detect_field_conflict(
                field_key=field_key,
                field_label=field_key,  # TODO: 传入字段中文名
                api_value=api_values.get(field_key),
                html_value=html_values.get(field_key),
                ai_value=ai_values.get(field_key) if ai_values else None,
                field_type=field_type,
            )

            if conflict.severity != ConflictSeverity.NONE:
                report.conflicts.append(conflict)
                report.conflict_count += 1

                if conflict.severity == ConflictSeverity.HIGH:
                    report.high_severity_count += 1
                elif conflict.severity == ConflictSeverity.MEDIUM:
                    report.medium_severity_count += 1
                elif conflict.severity == ConflictSeverity.LOW:
                    report.low_severity_count += 1

                if conflict.needs_review:
                    report.needs_human_review_count += 1

        logger.info(
            "conflict_detection_complete",
            f"冲突检测完成: {paimai_id}",
            paimai_id=paimai_id,
            total_fields=report.total_fields,
            conflict_count=report.conflict_count,
            high_severity=report.high_severity_count,
            medium_severity=report.medium_severity_count,
            low_severity=report.low_severity_count,
            needs_review=report.needs_human_review_count,
        )

        return report

    def get_conflict_summary(self, report: ConflictReport) -> dict[str, Any]:
        """获取冲突摘要"""
        return {
            "paimai_id": report.paimai_id,
            "total_fields": report.total_fields,
            "conflict_count": report.conflict_count,
            "high_severity": report.high_severity_count,
            "medium_severity": report.medium_severity_count,
            "low_severity": report.low_severity_count,
            "needs_review": report.needs_human_review_count,
            "conflict_rate": report.conflict_count / report.total_fields if report.total_fields > 0 else 0,
            "conflicts": [
                {
                    "field_key": c.field_key,
                    "field_label": c.field_label,
                    "severity": c.severity.value,
                    "values": [
                        {"source": v.source.value, "value": str(v.value)[:200]}
                        for v in c.values
                    ],
                    "recommended_value": str(c.recommended_value)[:200],
                    "needs_review": c.needs_review,
                }
                for c in report.conflicts
            ],
        }
