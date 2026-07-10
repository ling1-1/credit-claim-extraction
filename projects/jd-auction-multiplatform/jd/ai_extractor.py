"""
AI 辅助字段提取引擎
支持 DeepSeek / Qwen / OpenAI 三种后端，提供规则失败时的兜底提取
"""

import json
import time
from dataclasses import dataclass, field
import re
from typing import Any, Optional

from .ai_config import resolve_ai_config
from .config import get_config
from .logger import get_logger

logger = get_logger()

AI_PROMPT_KEY_VALUES_LIMIT = 60000
AI_PROMPT_DETAIL_TEXT_LIMIT = 60000
AI_PROMPT_NOTICE_TEXT_LIMIT = 30000
AI_PROMPT_VISION_CONTEXT_LIMIT = 3000

# 详情文本统一上限：bundle 原始文本、AI 上下文、入库 raw_detail_text 共用此值。
# 模型（qwen-plus / gpt-4o-mini 等）上下文窗口约 128K，60000 中文字符≈2万 token，属适当范围。
AI_DETAIL_TEXT_LIMIT = 60000


@dataclass
class AIExtractionResult:
    """AI 提取结果"""
    field_key: str
    field_label: str
    value: Any
    confidence: float  # 0.0 - 1.0
    source: str = "ai"
    reasoning: str = ""
    original_text: str = ""
    extraction_method: str = "ai"
    error: Optional[str] = None


@dataclass
class AIExtractionContext:
    """AI 提取上下文"""
    html_key_values: dict[str, str] = field(default_factory=dict)
    detail_text: str = ""  # 去除 HTML 标签的纯文本
    notice_text: str = ""  # 竞买须知文本
    image_urls: list[str] = field(default_factory=list)  # 详情页图片，可能包含图片表格
    asset_group: str = ""  # 资产类型: land, real_estate, equipment, vehicle, debt, equity, ip, goods, usufruct, other
    paimai_id: str = ""


def normalize_request_timeout(timeout: Any) -> Any:
    """Return a requests-compatible timeout; 0/negative means no local timeout."""
    if timeout is None:
        return None
    try:
        numeric = float(timeout)
    except (TypeError, ValueError):
        return timeout
    if numeric <= 0:
        return None
    return timeout


class BaseAIClient:
    """AI 客户端基类"""

    def __init__(self, api_key: str, base_url: str, timeout: int = 30, max_retries: int = 1):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = normalize_request_timeout(timeout)
        self.max_retries = max_retries

    def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """聊天补全接口 - 子类实现"""
        raise NotImplementedError

    def extract_json_from_response(self, response: dict[str, Any]) -> str:
        """从响应中提取内容"""
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return ""


class OpenAIClient(BaseAIClient):
    """OpenAI 客户端"""

    def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        try:
            import requests
        except ImportError:
            raise ImportError("需要安装 requests 库")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": kwargs.get("model", "gpt-3.5-turbo"),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.1),
            "response_format": {"type": "json_object"},
        }
        # OpenAI 兼容端点: 如果 base_url 已包含 /chat/completions 则不拼接路径
        if "/chat/completions" in self.base_url:
            api_url = self.base_url
        else:
            api_url = f"{self.base_url}/v1/chat/completions"
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        # 部分模型不支持 response_format(json_object)，回退到普通请求
        if response.status_code == 400 and "json_object" in response.text:
            del payload["response_format"]
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()


class DeepSeekClient(BaseAIClient):
    """DeepSeek 客户端"""

    def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        try:
            import requests
        except ImportError:
            raise ImportError("需要安装 requests 库")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": kwargs.get("model", "deepseek-chat"),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.1),
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 400 and "json_object" in response.text:
            del payload["response_format"]
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()


class QwenClient(BaseAIClient):
    """Qwen (通义千问) 客户端"""

    def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        try:
            import requests
        except ImportError:
            raise ImportError("需要安装 requests 库")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": kwargs.get("model", "qwen-plus"),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.1),
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{self.base_url}/compatible-mode/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 400 and "json_object" in response.text:
            del payload["response_format"]
            response = requests.post(
                f"{self.base_url}/compatible-mode/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()


class AIFieldExtractor:
    """AI 字段提取器"""

    def __init__(
        self,
        provider: str = "deepseek",
        api_key: str = "",
        base_url: str = "",
        model_name: str = "",
        vision_model: str = "",
        timeout: int = 30,
        max_retries: int = 1,
        qps: int = 10,
        enable_single_field_fallback: bool = False,
        circuit_breaker_failures: int = 2,
        circuit_breaker_cooldown_seconds: int = 300,
    ) -> None:
        self.provider = provider.lower()
        self.model_name = model_name
        self.vision_model = vision_model
        self.client: Optional[BaseAIClient] = None
        self.timeout = normalize_request_timeout(timeout)
        self.max_retries = max_retries
        self.enable_single_field_fallback = enable_single_field_fallback
        self.circuit_breaker_failures = max(0, circuit_breaker_failures)
        self.circuit_breaker_cooldown_seconds = max(0, circuit_breaker_cooldown_seconds)
        self.consecutive_failures = 0
        self.disabled_until = 0.0
        self.last_request_time = 0.0
        self.min_interval = 1 / max(int(qps or 10), 1)

        # 初始化客户端
        if api_key:
            self._init_client(self.provider, api_key, base_url, timeout, max_retries)

    def _init_client(
        self, provider: str, api_key: str, base_url: str, timeout: int, max_retries: int
    ) -> None:
        """初始化 AI 客户端"""
        provider = provider.lower()
        # bailian (百炼) 是千问的 OpenAI 兼容模式，使用 QwenClient
        if provider == "bailian":
            provider = "qwen"
        if provider == "openai":
            default_url = "https://api.openai.com"
            self.client = OpenAIClient(
                api_key=api_key,
                base_url=base_url or default_url,
                timeout=timeout,
                max_retries=max_retries,
            )
        elif provider == "deepseek":
            default_url = "https://api.deepseek.com"
            self.client = DeepSeekClient(
                api_key=api_key,
                base_url=base_url or default_url,
                timeout=timeout,
                max_retries=max_retries,
            )
        elif provider == "qwen":
            default_url = "https://dashscope.aliyuncs.com"
            self.client = QwenClient(
                api_key=api_key,
                base_url=base_url or default_url,
                timeout=timeout,
                max_retries=max_retries,
            )
        else:
            raise ValueError(f"不支持的 AI 提供商: {provider}")

    def _chat_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        if self.model_name and "model" not in kwargs:
            kwargs["model"] = self.model_name
        return kwargs

    def is_available(self) -> bool:
        """检查 AI 提取是否可用"""
        return self.client is not None and time.time() >= self.disabled_until

    def _record_success(self) -> None:
        self.consecutive_failures = 0
        self.disabled_until = 0.0

    def _record_failure(self, context: Optional[AIExtractionContext] = None) -> None:
        if self.circuit_breaker_failures <= 0:
            return
        self.consecutive_failures += 1
        if self.consecutive_failures < self.circuit_breaker_failures:
            return
        self.disabled_until = time.time() + self.circuit_breaker_cooldown_seconds
        logger.warning(
            "ai_circuit_breaker_open",
            "AI 连续失败，临时跳过后续 AI 提取",
            paimai_id=getattr(context, "paimai_id", None),
            consecutive_failures=self.consecutive_failures,
            cooldown_seconds=self.circuit_breaker_cooldown_seconds,
        )

    def _rate_limit(self) -> None:
        """限流"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def _build_prompt(
        self,
        field_key: str,
        field_label: str,
        field_description: str,
        context: AIExtractionContext,
    ) -> str:
        """构建提取提示词"""
        asset_type_desc = {
            "land": "土地",
            "real_estate": "房产",
            "equipment": "设备",
            "vehicle": "车辆",
            "debt": "债权",
            "equity": "股权",
            "ip": "知识产权",
            "goods": "物资",
            "usufruct": "用益物权",
            "other": "其他资产",
        }.get(context.asset_group, "资产")
        current_project_name = context.html_key_values.get("_current_project_name") or ""
        target_row_rule = (
            "如果 HTML 表格键值对中存在 _target_item_table_rows_json，说明公告/表格包含多个标的。"
            "你必须先用“当前采集标的名称/页面标题”定位目标行。"
            "目标相关字段只能从这些目标行及其表头行提取，严禁从相邻行、合计行或其他标的行复制。"
            "如果目标行没有提供该字段，则返回 null。"
        )

        prompt = f"""
你是一个专业的司法拍卖数据提取专家。请从以下材料中提取「{field_label}」字段。

【资产类型】{asset_type_desc}
【当前采集标的名称/页面标题】{current_project_name}
【字段名】{field_label} ({field_key})
【字段说明】{field_description}
【多标的表格约束】{target_row_rule}

【数据源】
1. HTML 表格键值对（已提取的结构化数据）:
{json.dumps(context.html_key_values, ensure_ascii=False, indent=2)[:AI_PROMPT_KEY_VALUES_LIMIT]}

2. 标的详情文本:
{context.detail_text[:AI_PROMPT_DETAIL_TEXT_LIMIT]}

3. 竞买须知文本:
{context.notice_text[:AI_PROMPT_NOTICE_TEXT_LIMIT]}

【提取要求】
1. 仔细阅读所有材料，找到最准确的值
2. 如果找不到相关信息，value 设置为 null
3. 置信度评分标准:
   - 1.0: 明确标注，直接匹配
   - 0.8-0.9: 信息明确，稍有推断
   - 0.5-0.7: 有相关信息，但不够明确
   - 0.0-0.4: 找不到或非常不确定
4. reasoning 字段简要说明提取依据或未找到的原因
5. 对 signup_start_time / signup_end_time：竞买公告/须知中的'竞价时间''拍卖时间'就是正确的起止时间来源。优先识别“将于X至Y止”“竞价时间为X起Y止”等表达；不要提取公告发布日期、展示看样期、资质审核截止日等非竞价时段的时间。
6. 对表格字段：优先按表头语义理解整张表，不要把表头、合计行或说明文字当成字段值。
7. 如果公告表格包含多个标的，必须只提取当前采集标的对应行的内容；不得把其他序号、其他房号、其他车位、其他债务人或其他资产的字段合并进来。
8. 对 special_notice：只有页面明确出现“特别告知/特别提示/特别提醒/特别说明/重要提示/注意事项/重大事项/瑕疵说明/风险提示”等标题时才提取该标题下内容；普通风险描述或“其他说明”不要作为特别告知。

【输出格式】JSON，严格按照以下结构:
{{
    "value": "提取到的值（找不到则为null）",
    "confidence": 0.0,
    "reasoning": "简要说明提取依据",
    "source_text": "原文中对应的片段"
}}
"""
        return prompt

    def extract_field(
        self,
        field_key: str,
        field_label: str,
        field_description: str,
        context: AIExtractionContext,
    ) -> AIExtractionResult:
        """提取单个字段"""
        if not self.is_available():
            return AIExtractionResult(
                field_key=field_key,
                field_label=field_label,
                value=None,
                confidence=0.0,
                error="AI 客户端未初始化",
            )

        # 限流
        self._rate_limit()

        prompt = self._build_prompt(field_key, field_label, field_description, context)
        messages = [
            {
                "role": "system",
                "content": "你是一个专业的司法拍卖数据提取专家，只输出 JSON 格式数据。",
            },
            {"role": "user", "content": prompt},
        ]

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                logger.debug(
                    "ai_extraction_attempt",
                    f"AI 提取尝试 {attempt + 1}: {field_label}",
                    field_key=field_key,
                    paimai_id=context.paimai_id,
                    attempt=attempt + 1,
                )

                response = self.client.chat_completion(messages, **self._chat_kwargs(temperature=0.1))
                content = self.client.extract_json_from_response(response)

                # 解析 JSON
                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    # 尝试从文本中提取 JSON
                    import re

                    match = re.search(r"\{[\s\S]*\}", content)
                    if match:
                        result = json.loads(match.group(0))
                    else:
                        raise

                ai_value = result.get("value")
                confidence = float(result.get("confidence", 0.0))
                reasoning = result.get("reasoning", "")
                source_text = result.get("source_text", "")

                # 置信度二次校验
                final_confidence = self._verify_confidence(
                    field_key, ai_value, confidence, source_text, context
                )

                logger.info(
                    "ai_extraction_success",
                    f"AI 提取成功: {field_label} = {str(ai_value)[:50]}",
                    field_key=field_key,
                    field_label=field_label,
                    paimai_id=context.paimai_id,
                    confidence=final_confidence,
                    has_value=ai_value is not None,
                )
                self._record_success()

                return AIExtractionResult(
                    field_key=field_key,
                    field_label=field_label,
                    value=ai_value,
                    confidence=final_confidence,
                    reasoning=reasoning,
                    original_text=source_text,
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    "ai_extraction_retry",
                    f"AI 提取重试: {field_label}, 错误: {str(e)}",
                    field_key=field_key,
                    paimai_id=context.paimai_id,
                    attempt=attempt + 1,
                    error=str(e),
                )
                time.sleep(1 * (attempt + 1))  # 退避重试

        # 所有重试失败
        logger.error(
            "ai_extraction_failed",
            f"AI 提取失败: {field_label}",
            field_key=field_key,
            paimai_id=context.paimai_id,
            error=str(last_error) if last_error else "unknown",
        )
        self._record_failure(context)

        return AIExtractionResult(
            field_key=field_key,
            field_label=field_label,
            value=None,
            confidence=0.0,
            error=str(last_error) if last_error else "unknown",
        )

    def _verify_confidence(
        self,
        field_key: str,
        ai_value: Any,
        ai_confidence: float,
        source_text: str,
        context: AIExtractionContext,
    ) -> float:
        """二次校验置信度"""
        if ai_value is None:
            return 0.0

        # 校验 1: 原文回查 - 提取的值是否在原文中出现
        value_str = str(ai_value).strip()
        all_text = context.detail_text + context.notice_text

        if value_str and len(value_str) >= 2:
            if value_str not in all_text and value_str not in source_text:
                ai_confidence *= 0.7  # 惩罚

        # 校验 2: 格式校验
        # 金额类字段
        if any(k in field_key for k in ["price", "amount", "money"]):
            if not re.search(r"\d", value_str):
                ai_confidence *= 0.5

        # 日期类字段
        elif any(k in field_key for k in ["date", "time"]):
            if not re.search(r"\d{4}|\d{2}[年/.-]", value_str):
                ai_confidence *= 0.5

        # 电话类字段
        elif any(k in field_key for k in ["phone", "tel", "contact"]):
            if not re.search(r"\d{7,}", value_str):
                ai_confidence *= 0.5

        return min(max(ai_confidence, 0.0), 1.0)

    def batch_extract(
        self,
        fields: list[tuple[str, str, str]],  # [(key, label, description), ...]
        context: AIExtractionContext,
    ) -> dict[str, AIExtractionResult]:
        """批量提取多个字段（一次 API 调用提取所有缺失字段，大幅降低 token 消耗和 API 次数）"""
        results: dict[str, AIExtractionResult] = {}

        if not fields or not self.is_available():
            for field_key, field_label, _ in fields:
                results[field_key] = AIExtractionResult(
                    field_key=field_key, field_label=field_label,
                    value=None, confidence=0.0,
                    error="AI 客户端未初始化" if not self.is_available() else None,
                )
            return results

        self._rate_limit()

        # 构建一次性批量提取 prompt
        asset_type_desc = {
            "land": "土地", "real_estate": "房产", "equipment": "设备",
            "vehicle": "车辆", "debt": "债权", "equity": "股权",
            "ip": "知识产权", "goods": "物资", "usufruct": "用益物权",
            "other": "其他资产",
        }.get(context.asset_group, "资产")
        current_project_name = context.html_key_values.get("_current_project_name") or ""
        target_row_rule = (
            "如果 HTML 表格键值对中存在 _target_item_table_rows_json，说明公告/表格包含多个标的。"
            "你必须先用“当前采集标的名称/页面标题”定位目标行。"
            "目标相关字段只能从这些目标行及其表头行提取，严禁从相邻行、合计行或其他标的行复制。"
            "如果目标行没有提供该字段，则返回 null。"
        )

        fields_json = json.dumps(
            [{"key": k, "label": l, "desc": d} for k, l, d in fields],
            ensure_ascii=False, indent=2,
        )

        prompt = f"""你是一个专业的司法拍卖数据提取专家。请从以下材料中提取指定字段的值。

【资产类型】{asset_type_desc}
【标的ID】{context.paimai_id}
【当前采集标的名称/页面标题】{current_project_name}
【多标的表格约束】{target_row_rule}

【需要提取的字段】
{fields_json}

【数据源】
1. HTML 表格键值对（已提取的结构化数据）:
{json.dumps(context.html_key_values, ensure_ascii=False, indent=2)[:AI_PROMPT_KEY_VALUES_LIMIT]}

2. 标的详情文本:
{context.detail_text[:AI_PROMPT_DETAIL_TEXT_LIMIT]}

3. 竞买须知文本:
{context.notice_text[:AI_PROMPT_NOTICE_TEXT_LIMIT]}

【提取要求】
1. 仔细阅读所有材料，为每个字段找到最准确的值
2. 如果找不到某个字段，该字段的 value 设置为 null
3. 置信度评分标准: 1.0=明确标注, 0.8-0.9=信息明确, 0.5-0.7=有相关信息但不够明确, 0.0-0.4=找不到或非常不确定
4. reasoning 字段简要说明提取依据
5. 对 signup_start_time / signup_end_time：竞买公告/须知中的'竞价时间''拍卖时间'就是正确的起止时间来源。优先识别“将于X至Y止”“竞价时间为X起Y止”等表达；不要提取公告发布日期、展示看样期、资质审核截止日等非竞价时段的时间。
6. 对债权表格：按表头语义读取整张表，区分债权合计、本金余额、利息/欠息、担保方式等列；不要把表头、合计行或说明文字当成明细行。
7. 如果公告表格包含多个标的，必须只提取当前采集标的对应行的内容；不得把其他序号、其他房号、其他车位、其他债务人或其他资产的字段合并进来。
8. 对 special_notice：只有页面明确出现“特别告知/特别提示/特别提醒/特别说明/重要提示/注意事项/重大事项/瑕疵说明/风险提示”等标题时才提取该标题下内容；普通风险描述或“其他说明”不要作为特别告知。

【输出格式】严格返回 JSON，key 为字段 key，value 为对象:
{{
    "field_key_1": {{"value": "提取值或null", "confidence": 0.0, "reasoning": "依据说明", "source_text": "原文片段"}},
    "field_key_2": {{"value": "...", "confidence": 0.0, "reasoning": "...", "source_text": "..."}}
}}"""

        messages = [
            {"role": "system", "content": "你是一个专业的司法拍卖数据提取专家，只输出 JSON 格式数据。"},
            {"role": "user", "content": prompt},
        ]

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat_completion(messages, **self._chat_kwargs(temperature=0.1))
                content = self.client.extract_json_from_response(response)

                try:
                    batch_result = json.loads(content)
                except json.JSONDecodeError:
                    match = re.search(r"\{[\s\S]*\}", content)
                    if match:
                        batch_result = json.loads(match.group(0))
                    else:
                        raise

                for field_key, field_label, _ in fields:
                    field_data = batch_result.get(field_key, {})
                    ai_value = field_data.get("value")
                    confidence = float(field_data.get("confidence", 0.0))
                    reasoning = field_data.get("reasoning", "")
                    source_text = field_data.get("source_text", "")
                    final_confidence = self._verify_confidence(field_key, ai_value, confidence, source_text, context)
                    results[field_key] = AIExtractionResult(
                        field_key=field_key, field_label=field_label,
                        value=ai_value, confidence=final_confidence,
                        reasoning=reasoning, original_text=source_text,
                    )

                logger.info(
                    "batch_ai_extraction_success",
                    f"批量 AI 提取成功: {len(fields)} 个字段",
                    paimai_id=context.paimai_id,
                    field_count=len(fields),
                )
                self._record_success()
                return results

            except Exception as e:
                last_error = e
                event_name = "batch_ai_extraction_retry" if attempt < self.max_retries else "batch_ai_extraction_failed_attempt"
                message = f"批量 AI 提取失败: 错误={e}" if attempt >= self.max_retries else f"批量 AI 提取重试: 错误={e}"
                logger.warning(
                    event_name,
                    message,
                    paimai_id=context.paimai_id,
                    attempt=attempt + 1,
                    error=str(e),
                )
                time.sleep(1 * (attempt + 1))

        if not self.enable_single_field_fallback:
            self._record_failure(context)
            logger.warning(
                "batch_ai_extraction_no_fallback",
                "批量提取失败，快速模式下不再逐字段重试",
                paimai_id=context.paimai_id,
                error=str(last_error) if last_error else None,
            )
            for field_key, field_label, _ in fields:
                results[field_key] = AIExtractionResult(
                    field_key=field_key,
                    field_label=field_label,
                    value=None,
                    confidence=0.0,
                    error=str(last_error) if last_error else "batch extraction failed",
                )
            return results

        # 全部失败，回退到单字段提取
        logger.warning(
            "batch_ai_extraction_fallback",
            "批量提取失败，回退到单字段逐次提取",
            paimai_id=context.paimai_id,
        )
        for field_key, field_label, field_description in fields:
            result = self.extract_field(field_key, field_label, field_description, context)
            results[field_key] = result

        return results

    def extract_ip_details_from_images(
        self,
        image_urls: list[str],
        context: AIExtractionContext,
    ) -> AIExtractionResult:
        """Use a vision-capable model to OCR IP detail tables rendered as images."""
        if not image_urls or not self.is_available():
            return AIExtractionResult(
                field_key="ip_details",
                field_label="知识产权逐项明细",
                value=None,
                confidence=0.0,
                error="AI 客户端未初始化或无图片",
            )

        self._rate_limit()
        limited_urls = image_urls[:8]
        prompt = self._build_ip_vision_prompt(limited_urls, context)

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in limited_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [
            {"role": "system", "content": "你只输出 JSON，不要输出 Markdown。"},
            {"role": "user", "content": content},
        ]

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                cfg = get_config()
                vision_model = self.vision_model or getattr(cfg.ai, "vision_model", "") or (
                    "qwen-vl-plus" if self.provider in ("qwen", "bailian") else "gpt-4o-mini"
                )
                response = self.client.chat_completion(messages, temperature=0.1, model=vision_model)
                content_text = self.client.extract_json_from_response(response)
                try:
                    parsed = json.loads(content_text)
                except json.JSONDecodeError:
                    match = re.search(r"\{[\s\S]*\}", content_text)
                    if not match:
                        raise
                    parsed = json.loads(match.group(0))
                value = parsed.get("value") or parsed.get("ip_details") or parsed.get("details")
                confidence = float(parsed.get("confidence", 0.0) or 0.0)
                return AIExtractionResult(
                    field_key="ip_details",
                    field_label="知识产权逐项明细",
                    value=value,
                    confidence=min(max(confidence, 0.0), 1.0),
                    reasoning=parsed.get("reasoning", ""),
                    original_text=parsed.get("source_text", "") or f"image_urls={limited_urls}",
                    extraction_method="vision_ai",
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "ip_image_ai_retry",
                    f"图片表格 AI 提取重试: {exc}",
                    paimai_id=context.paimai_id,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                time.sleep(1 * (attempt + 1))

        return AIExtractionResult(
            field_key="ip_details",
            field_label="知识产权逐项明细",
            value=None,
            confidence=0.0,
            error=str(last_error) if last_error else "unknown",
            extraction_method="vision_ai",
        )

    def _build_ip_vision_prompt(self, image_urls: list[str], context: AIExtractionContext) -> str:
        return f"""你是司法拍卖知识产权表格 OCR 专家。请读取图片中的表格，逐条提取知识产权明细。

【标的ID】{context.paimai_id}
【图片数量】{len(image_urls)}
【文字上下文】
{context.detail_text[:AI_PROMPT_VISION_CONTEXT_LIMIT]}
{context.notice_text[:AI_PROMPT_VISION_CONTEXT_LIMIT]}

【提取规则】
1. 图片中每一行软件著作权、专利、商标或其他知识产权，都必须单独输出一条 JSON 记录。
2. 禁止把“7项软件著作权”“17项专利权”合并成摘要行。
3. 如果图片里有多个表格，例如软件著作权表和专利表两个表，两个表都要逐行输出，不要只输出第一个表。
4. 软件著作权表常见列：软件名称、软件著作权证书号、登记号、取得方式、登记批准日期。
5. 专利表常见列：专利名称、专利号/申请号、申请日、专利类型、状态。
6. certificate_no 可以合并同一行里的证书号、登记号、申请号、专利号，但必须来自同一行。
7. 看不清的单元格填 null，不要编造。

【输出格式】严格返回 JSON：
{{
  "value": [
    {{
      "sequence_no": "表格序号",
      "ip_name": "单项名称",
      "certificate_no": "证书号/登记号/申请号/专利号，可合并多个证号",
      "ip_type": "软件著作权/发明专利/实用新型/外观设计/商标等",
      "application_date": "申请日或登记批准日期",
      "patent_type": "专利类型，非专利填 null",
      "status": "法律状态或备注",
      "source_excerpt": "图片中对应行的关键文字"
    }}
  ],
  "confidence": 0.0,
  "reasoning": "简要说明"
}}"""


# 字段定义映射 - 用于 AI 提取的字段说明（包含所有共有字段和特有字段）
FIELD_DEFINITIONS: dict[str, dict[str, str]] = {
    # ===== 共有字段 (COMMON_FIELDS) =====
    "asset_type": {"label": "标的类型", "description": "资产的具体类型，如住宅、商业、工业、土地、车辆等"},
    "asset_location": {"label": "标的所在地", "description": "资产所在的具体地理位置，包括省市区街道"},
    "project_status": {"label": "项目状态", "description": "拍卖项目当前状态，如预告中、进行中、已结束、已撤回等"},
    "auction_stage": {"label": "拍卖阶段", "description": "第几次拍卖，如一拍、二拍、变卖、破产等"},
    "start_price_raw": {"label": "起拍价", "description": "拍卖起始价格，包含单位"},
    "final_price_raw": {"label": "最终价", "description": "最终成交价格或当前价格"},
    "disposal_party": {"label": "处置方", "description": "发起拍卖的机构，如法院、资产管理公司等"},
    "contact_info": {"label": "联系方式", "description": "项目咨询联系方式。必须包含手机号、座机号或邮箱，并尽量保留联系人姓名；如果只有审判员、书记员、当事人姓名但没有电话/邮箱，则返回 null。"},
    "special_notice": {"label": "特别告知", "description": "页面中明确以特别告知、特别提示、特别提醒、特别说明、重要提示、注意事项、重大事项、瑕疵说明、风险提示等标题标出的提示内容；如果只是普通风险描述或其他说明且没有这些标题，返回 null。"},
    "assessment_price_time": {
        "label": "评估价格及时间",
        "description": "提取页面明确写有“评估价、评估价格、评估价值、评估基准日、评估报告”的评估价格及时间；如果页面或接口只有“市场价/市场价格”，也可以作为评估价格候选并标注为市场价。不要把起拍价、挂牌价、转让底价、保证金、债权基准日或普通基准日当作评估价。如果没有明确评估或市场价信息，返回 null。",
    },
    "signup_start_time": {"label": "报名开始时间", "description": "挂牌、竞价或拍卖活动开始时间。优先从“将于X至Y止”“竞价时间为X起Y止”等处置方式/公告段落提取开始时间，不要使用公告期或资质审核时间。"},
    "signup_end_time": {"label": "报名截止时间", "description": "挂牌、竞价或拍卖活动截止/结束时间。优先从“将于X至Y止”“竞价时间为X起Y止”等处置方式/公告段落提取结束时间，不要使用公告期或资质审核时间。"},
    "data_source": {"label": "数据来源", "description": "数据来源平台"},
    "project_name": {"label": "项目名称", "description": "拍卖标的的名称标题"},
    "attachments_json": {"label": "附件材料", "description": "相关附件列表"},
    # ===== 土地 (land) 特有字段 =====
    "right_certificate_no": {"label": "权证编号", "description": "不动产权证号、土地证号"},
    "land_area": {"label": "土地面积", "description": "宗地面积，通常以平方米为单位"},
    "land_use": {"label": "土地用途", "description": "土地规划用途，如住宅、商业、工业等"},
    "use_term": {"label": "使用期限", "description": "土地使用期限或终止日期"},
    "land_location": {"label": "土地位置", "description": "土地所在的具体位置或坐落"},
    "right_holder": {
        "label": "权利人",
        "description": "页面明确标注的所有权人/产权人/著作权人/专利权人全称。如果页面没有明确标注这些词之一，则必须返回 null。绝对不要用商品名称、标的名称、project_name、goods_name 或其他无关信息代替。",
    },
    "land_status": {"label": "土地状态", "description": "土地当前使用状态或现状"},
    "disclosed_defects": {"label": "公示瑕疵", "description": "瑕疵说明、风险提示等"},
    "site_images": {"label": "现场图片", "description": "现场照片或图片列表"},
    "land_type": {"label": "土地类型", "description": "土地的具体类型或权利类型"},
    "assessment_time_value": {"label": "评估时间及价值", "description": "评估价及评估时间"},
    # ===== 房地产 (real_estate) 特有字段 =====
    "building_area": {"label": "建筑面积", "description": "房屋建筑面积或套内建筑面积"},
    "property_use": {"label": "房产用途", "description": "规划用途，如住宅、商业、办公等"},
    "property_location": {"label": "房产位置", "description": "房产所在的具体位置或坐落"},
    "property_structure": {"label": "房产结构", "description": "建筑用料或结构类型"},
    "property_status": {"label": "房产状态", "description": "房产当前使用状态或现状"},
    "property_type": {"label": "房产类型", "description": "房屋或物业类型"},
    "asset_highlights": {"label": "资产亮点", "description": "资产的核心优势或亮点描述"},
    # ===== 设备 (equipment) 特有字段 =====
    "storage_location": {"label": "存放位置", "description": "设备存放地点或所在地"},
    "equipment_status": {"label": "设备状态", "description": "设备当前使用状态或现状"},
    "equipment_type": {"label": "设备类型", "description": "设备的具体类型或种类"},
    # ===== 车辆 (vehicle) 特有字段 =====
    "vehicle_brand_model": {"label": "车型品牌", "description": "品牌型号、车辆品牌"},
    "vehicle_usage": {"label": "车辆使用情况", "description": "出厂日期、里程数、使用情况"},
    "plate_number": {"label": "车牌号", "description": "号牌号码或牌照号"},
    "vehicle_configuration": {"label": "车辆配置", "description": "配置、排量、功率等"},
    "vehicle_status": {"label": "车辆状态", "description": "车辆当前状态或现状"},
    "vehicle_images": {"label": "车辆图片", "description": "车辆照片或图片列表"},
    "vehicle_type": {"label": "车辆类型", "description": "车辆的类型或种类"},
    # ===== 债权 (debt) 特有字段 =====
    "debtor_name": {"label": "主债务人名称", "description": "主债务人、借款人或债务人名称"},
    "principal_balance": {"label": "本金余额", "description": "本金余额、剩余本金或贷款本金金额"},
    "interest_balance": {"label": "利息余额", "description": "利息余额、剩余利息或欠息金额"},
    "benchmark_date": {"label": "基准日", "description": "债权基准日或截止日期"},
    "guarantee_method": {"label": "担保方式", "description": "担保类型、保证方式或抵押顺位"},
    "guarantors": {"label": "保证人", "description": "担保人或保证方"},
    "collateral": {"label": "抵质押物", "description": "抵押物、质押物或抵押资产"},
    "litigation_status": {"label": "诉讼状态", "description": "诉讼进展或执行情况"},
    "creditor": {"label": "债权人", "description": "权利人、转让方或债权人名称"},
    "household_count": {"label": "户数", "description": "债权笔数或户数"},
    # ===== 股权 (equity) 特有字段 =====
    "transferor": {"label": "转让方", "description": "出让方或处置方"},
    "target_company": {"label": "标的企业", "description": "企业名称或公司名称"},
    "equity_ratio": {"label": "股权占比", "description": "持股比例或股权比例"},
    "company_nature": {"label": "企业性质", "description": "公司性质或企业类型"},
    "company_industry": {"label": "企业行业", "description": "所属行业"},
    "business_scope": {"label": "经营范围", "description": "主营业务"},
    "ownership_structure": {"label": "股权结构", "description": "股东结构"},
    "financial_metrics": {"label": "财务指标", "description": "财务数据、营业收入、利润总额等"},
    "asset_valuation": {"label": "资产评估", "description": "资产总额、负债总额、净资产"},
    "disclosure_items": {"label": "公示事项", "description": "重大事项或风险提示"},
    "attached_assets": {"label": "附带标的", "description": "附带资产或同步转让"},
    # ===== 知识产权 (ip) 特有字段 =====
    "subject_name": {"label": "标的名称", "description": "知识产权名称"},
    "certificate_no": {"label": "标的证号", "description": "专利号、作品号或证书号"},
    "ip_type": {"label": "知产类型", "description": "知识产权类型"},
    "specific_category": {"label": "具体类别", "description": "类别或小类"},
    "subject_intro": {"label": "标的简介", "description": "简介或基本情况"},
    "right_term": {"label": "权利期限", "description": "有效期或保护期限"},
    "ip_details": {
        "label": "知识产权逐项明细",
        "description": "从页面文字、HTML 表格、图片表格或附件文本中逐行提取每一项知识产权明细，value 必须是 JSON 数组。每行字段包括 sequence_no、ip_name、certificate_no、ip_type、application_date、patent_type、status、source_excerpt。禁止把“7项软件著作权”“17项专利权”等汇总描述当成明细；如果只能看到汇总而没有逐行明细，返回 null，交给图片 OCR 或附件解析兜底。",
    },
    # ===== 物资 (goods) 特有字段 =====
    "goods_category": {"label": "物资种类", "description": "种类或类别"},
    "goods_name": {"label": "物资名称", "description": "物资名称"},
    "goods_location": {"label": "物资所在位置", "description": "所在地或存放位置"},
    "goods_details": {"label": "物资详情", "description": "详情、规格或数量"},
    "right_burden": {"label": "权利负担", "description": "查封、抵押或负担情况"},
    # ===== 用益物权 (usufruct) 特有字段 =====
    "right_category": {"label": "权益种类", "description": "权益类型或权利类型"},
    "subject_location": {"label": "标的所在位置", "description": "所在地或位置"},
    "subject_details": {"label": "标的物详情", "description": "详情"},
    "valid_period": {"label": "有效期", "description": "期限或权利期限"},
    "original_right_holder": {"label": "原权利人", "description": "权利人或原产权人"},
}


def create_ai_extractor(config: dict[str, Any] | None = None) -> Optional[AIFieldExtractor]:
    """创建 AI 提取器实例"""
    cfg = get_config()

    if config is None:
        cli_values = {
            key: value
            for key, value in {
                "profile_name": cfg.ai.active_profile,
                "provider": cfg.ai.model,
                "model_name": cfg.ai.model_name,
                "api_key": cfg.ai.api_key,
                "base_url": cfg.ai.base_url,
                "vision_model": cfg.ai.vision_model,
                "timeout": cfg.ai.timeout if cfg.ai.timeout else "",
                "max_retries": cfg.ai.max_retries if cfg.ai.max_retries else "",
                "qps": cfg.ai.qps if cfg.ai.qps and cfg.ai.qps != 10 else "",
            }.items()
            if value not in (None, "")
        }
        resolved = resolve_ai_config(
            cli=cli_values
        )
        config = resolved.to_extractor_config(
            enable_single_field_fallback=cfg.ai.enable_single_field_fallback,
            circuit_breaker_failures=cfg.ai.circuit_breaker_failures,
            circuit_breaker_cooldown_seconds=cfg.ai.circuit_breaker_cooldown_seconds,
        )

    if not config.get("api_key"):
        logger.warning("ai_extractor_disabled", "AI 提取器未启用（未配置 API Key）")
        return None

    try:
        return AIFieldExtractor(**config)
    except Exception as e:
        logger.error("ai_extractor_init_failed", f"AI 提取器初始化失败: {e}", error=str(e))
        return None
