from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps


FIELDS = [
    "债权人",
    "主债务人",
    "借款金额",
    "本金余额",
    "保证人",
    "抵押物",
    "质押物",
    "贷款日",
    "到期日",
    "诉讼状态",
    "受让方",
    "转让方",
]

NULL_MARKERS = {
    "无",
    "无信息",
    "未披露",
    "未显示",
    "未列明",
    "未载明",
    "未提供",
    "未提及",
    "不详",
    "不明确",
    "无法判断",
    "无法识别",
}

KEY_FIELDS = ["债权人", "主债务人", "借款金额", "本金余额"]

EXTRA_RECORD_FIELDS = ["row_index", "amount_basis"]

REVIEW_COLUMNS = [
    "来源图片",
    "公告块编号",
    "问题类型",
    "问题详情",
    "置信度",
    "原文摘录",
    "切块图片",
    "bbox",
]

TRACE_COLUMNS = [
    "来源图片",
    "公告块编号",
    "切块图片",
    "bbox",
    "provider",
    "model",
    "明细序号",
    *FIELDS,
    "金额口径",
    "置信度",
    "原文摘录",
    "行内警告",
]

ISSUE_COLUMNS = [
    "行号",
    "问题类型",
    "问题详情",
    "来源图片",
    "公告块编号",
    "债权人",
    "主债务人",
    "借款金额",
    "本金余额",
    "受让方",
    "转让方",
    "置信度",
    "原文摘录",
    "切块图片",
]

REMOVED_COLUMNS = [
    "剔除原因",
    "来源图片",
    "公告块编号",
    *FIELDS,
    "置信度",
    "原文摘录",
    "切块图片",
]

STAT_COLUMNS = [
    "来源图片",
    "切块数量",
    "成功抽取记录数",
    "复核问题数",
    "是否调用模型",
]


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return self.w * self.h

    def expand(self, pad: int, width: int, height: int) -> "Box":
        x1 = max(0, self.x - pad)
        y1 = max(0, self.y - pad)
        x2 = min(width, self.x + self.w + pad)
        y2 = min(height, self.y + self.h + pad)
        return Box(x1, y1, x2 - x1, y2 - y1)

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


def load_env_file(path: Path, override: bool = True) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_env_file(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(
        description="Batch extract credit claim data from newspaper announcement images."
    )
    parser.add_argument("--input-dir", default=".", help="Folder containing jpg/jpeg/png images.")
    parser.add_argument("--output-dir", default="outputs", help="Folder for CSVs, crops, and debug images.")
    parser.add_argument(
        "--provider",
        choices=["ark", "ark_coding", "agnes", "compat", "xiaomi", "bailian", "openai"],
        default=os.getenv("LLM_PROVIDER", "ark"),
        help="Vision LLM provider. Ark/Ark CodingPlan/Agnes/Xiaomi/Bailian/compat use OpenAI-compatible chat completions.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Vision model, Ark endpoint ID, or OpenAI model.",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="OpenAI-compatible base URL.",
    )
    parser.add_argument("--api-key-env", default="", help="Override API key environment variable name.")
    parser.add_argument("--whole-image", action="store_true", help="Treat each input image as one full-image block.")
    parser.add_argument("--prepare-only", action="store_true", help="Only crop/debug locally; do not call the model.")
    parser.add_argument("--smoke-test", action="store_true", help="Call the model on only one image/block and stop.")
    parser.add_argument("--min-area-ratio", type=float, default=0.004, help="Smallest candidate block area ratio.")
    parser.add_argument("--max-crop-side", type=int, default=2600, help="Resize saved API crops to this max side.")
    parser.add_argument("--jpeg-quality", type=int, default=92, help="JPEG quality for cropped blocks.")
    parser.add_argument("--confidence-threshold", type=float, default=0.75, help="Rows below this enter review CSV.")
    parser.add_argument("--request-timeout", type=float, default=120.0, help="OpenAI request timeout in seconds.")
    parser.add_argument("--max-tokens", type=int, default=16000, help="Maximum model output tokens per block.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per model call.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Pause between model calls in seconds.")
    parser.add_argument("--limit-images", type=int, default=0, help="Debug only: process at most N images.")
    parser.add_argument("--limit-blocks-per-image", type=int, default=0, help="Debug only: process at most N blocks per image.")
    parser.add_argument("--no-cache", action="store_true", help="Do not reuse saved per-block model results.")
    parser.add_argument(
        "--reuse-any-cache",
        action="store_true",
        help="Reuse existing caches even if provider/model differ. Default avoids accidental old-provider reuse.",
    )
    parser.add_argument(
        "--reuse-success-cache",
        action="store_true",
        help="Reuse successful caches from any provider/model, but rerun failed or missing blocks with the current provider.",
    )
    parser.add_argument(
        "--keep-sparse-duplicates",
        action="store_true",
        help="Keep low-information rows that look duplicated by a fuller row from the same image.",
    )
    parser.add_argument("--cache-only", action="store_true", help="Rebuild CSVs from cached block results without API calls.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent model calls for uncached blocks. Try 2-3 for Ark.")
    args = parser.parse_args()

    model_defaults = {
        "ark": os.getenv("ARK_MODEL") or "ep-20260309122322-xwfhv",
        "ark_coding": os.getenv("ARK_CODING_MODEL") or "",
        "agnes": os.getenv("AGNES_MODEL") or "agnes-2.0-flash",
        "compat": os.getenv("COMPAT_MODEL") or "",
        "xiaomi": os.getenv("XIAOMI_MODEL") or "mimo-v2.5-pro",
        "bailian": os.getenv("BAILIAN_MODEL") or "",
        "openai": os.getenv("OPENAI_MODEL") or "gpt-4.1",
    }
    base_url_defaults = {
        "ark": os.getenv("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3",
        "ark_coding": os.getenv("ARK_CODING_BASE_URL") or "https://ark.cn-beijing.volces.com/api/coding/v3",
        "agnes": os.getenv("AGNES_BASE_URL") or "https://apihub.agnes-ai.com/v1",
        "compat": os.getenv("COMPAT_BASE_URL") or "",
        "xiaomi": os.getenv("XIAOMI_BASE_URL") or "https://token-plan-cn.xiaomimimo.com/v1",
        "bailian": os.getenv("BAILIAN_BASE_URL") or "",
        "openai": "",
    }
    if not args.model:
        args.model = model_defaults[args.provider]
    if not args.base_url:
        args.base_url = base_url_defaults[args.provider]
    return args


def read_image(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        array = np.asarray(image)
    return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)


def write_image(path: Path, image: np.ndarray, quality: int = 95) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower() or ".jpg"
    params: list[int] = []
    if ext in {".jpg", ".jpeg"}:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    ok, data = cv2.imencode(ext, image, params)
    if not ok:
        raise RuntimeError(f"Could not encode image: {path}")
    data.tofile(str(path))


def resize_for_api(image: np.ndarray, max_side: int) -> np.ndarray:
    height, width = image.shape[:2]
    current = max(width, height)
    if current <= max_side:
        return image
    scale = max_side / current
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)


def make_line_mask(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    threshold = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        15,
    )

    height, width = threshold.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, width // 55), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, height // 55)))

    horizontal = cv2.dilate(cv2.erode(threshold, horizontal_kernel, iterations=1), horizontal_kernel, iterations=1)
    vertical = cv2.dilate(cv2.erode(threshold, vertical_kernel, iterations=1), vertical_kernel, iterations=1)
    lines = cv2.bitwise_or(horizontal, vertical)

    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    return cv2.morphologyEx(lines, cv2.MORPH_CLOSE, close_kernel, iterations=2)


def overlap_area(a: Box, b: Box) -> int:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    return max(0, x2 - x1) * max(0, y2 - y1)


def contained_ratio(inner: Box, outer: Box) -> float:
    if inner.area == 0:
        return 0.0
    return overlap_area(inner, outer) / inner.area


def iou(a: Box, b: Box) -> float:
    inter = overlap_area(a, b)
    union = a.area + b.area - inter
    return inter / union if union else 0.0


def dedupe_boxes(boxes: list[Box]) -> list[Box]:
    boxes = sorted(boxes, key=lambda box: box.area, reverse=True)
    unique: list[Box] = []
    for box in boxes:
        if any(iou(box, old) > 0.88 for old in unique):
            continue
        unique.append(box)
    return unique


def remove_nested_boxes(boxes: list[Box], image_area: int) -> list[Box]:
    accepted: list[Box] = []
    for box in sorted(boxes, key=lambda item: item.area, reverse=True):
        if box.area / image_area > 0.78:
            continue
        if any(contained_ratio(box, outer) > 0.92 for outer in accepted):
            continue
        accepted.append(box)
    return accepted


def sort_boxes_reading_order(boxes: list[Box]) -> list[Box]:
    if not boxes:
        return []
    median_height = int(np.median([box.h for box in boxes])) or 1
    row_band = max(1, median_height // 2)
    return sorted(boxes, key=lambda box: (box.y // row_band, box.x))


def detect_blocks(image: np.ndarray, min_area_ratio: float) -> list[Box]:
    height, width = image.shape[:2]
    image_area = width * height
    mask = make_line_mask(image)
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[Box] = []
    min_area = image_area * min_area_ratio
    min_w = max(80, width // 18)
    min_h = max(80, height // 22)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h < min_area:
            continue
        if w < min_w or h < min_h:
            continue
        aspect = w / max(1, h)
        if aspect < 0.12 or aspect > 8.5:
            continue
        candidates.append(Box(x, y, w, h))

    candidates = dedupe_boxes(candidates)
    candidates = remove_nested_boxes(candidates, image_area)

    if not candidates:
        # Keep the pipeline lossless: if no frames are detected, review the whole image.
        candidates = [Box(0, 0, width, height)]

    pad = max(8, int(min(width, height) * 0.006))
    expanded = [box.expand(pad, width, height) for box in candidates]
    return sort_boxes_reading_order(expanded)


def draw_debug_boxes(image: np.ndarray, boxes: list[Box]) -> np.ndarray:
    debug = image.copy()
    for index, box in enumerate(boxes, start=1):
        cv2.rectangle(debug, (box.x, box.y), (box.x + box.w, box.y + box.h), (0, 0, 255), 4)
        label = str(index)
        cv2.putText(
            debug,
            label,
            (box.x + 8, max(28, box.y + 36)),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            (0, 0, 255),
            3,
            cv2.LINE_AA,
        )
    return debug


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "；".join(normalize_text(item) for item in value if normalize_text(item))
    value = str(value)
    value = value.replace("\u3000", " ").replace("\r", "\n")
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ;；,，、\n\t")
    return value


def normalize_warning_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_text(item) for item in value if normalize_text(item)]
    text = normalize_text(value)
    return [text] if text else []


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_amount(value: Any) -> str:
    text = normalize_text(value)
    if not text or text in NULL_MARKERS:
        return ""

    parts = re.split(r"\s*[；;]\s*", text)
    if len(parts) > 1:
        return "；".join(part for part in (normalize_amount(part) for part in parts) if part)

    text = text.replace("人民币", "")
    text = text.replace("￥", "").replace("¥", "")
    text = text.replace("圆整", "元").replace("元整", "元")
    text = re.sub(r"[（(][^（）()]*[）)]", "", text)
    text = re.sub(r"\s+", "", text).strip("，,、；;")
    if not re.search(r"\d", text):
        return ""

    def convert_wan(match: re.Match[str]) -> str:
        raw_number = match.group(1).replace(",", "").replace("，", "")
        try:
            yuan_value = Decimal(raw_number) * Decimal("10000")
        except InvalidOperation:
            return match.group(0)
        return f"{format_decimal(yuan_value)}元"

    text = re.sub(r"(\d[\d,，]*(?:\.\d+)?)万元", convert_wan, text)

    if "元" not in text and "万" not in text and re.fullmatch(r"约?\d[\d,，]*(?:\.\d+)?(?:左右)?", text):
        text = f"{text}元"

    return text


def normalize_date(value: Any) -> str:
    text = normalize_text(value)
    if not text or text in NULL_MARKERS:
        return ""
    match = re.search(r"(\d{4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"(\d{4})(\d{2})(\d{2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return text


def normalize_record(record: dict[str, Any]) -> dict[str, str]:
    normalized = {field: normalize_text(record.get(field, "")) for field in FIELDS}
    for field in FIELDS:
        if normalized[field] in NULL_MARKERS:
            normalized[field] = ""
    normalized["借款金额"] = normalize_amount(normalized["借款金额"])
    normalized["本金余额"] = normalize_amount(normalized["本金余额"])
    normalized["贷款日"] = normalize_date(normalized["贷款日"])
    normalized["到期日"] = normalize_date(normalized["到期日"])
    return normalized


def looks_like_bad_amount(value: str) -> bool:
    if not value:
        return False
    return not bool(re.search(r"\d", value))


def looks_like_bad_date(value: str) -> bool:
    if not value:
        return False
    return not bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def suspicious_field_label_leak(row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    labels = set(FIELDS)
    for field, value in row.items():
        if not value:
            continue
        leaked = [label for label in labels if label != field and label in value]
        if leaked:
            issues.append(f"{field}疑似包含其他字段名:{'、'.join(leaked)}")
    return issues


def image_to_data_url(path: Path) -> str:
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def parse_model_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        raw = "".join(str(item) for item in raw)
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extraction_schema(strict: bool = True) -> dict[str, Any]:
    record_properties: dict[str, Any] = {field: {"type": "string"} for field in FIELDS}
    record_properties.update(
        {
            "row_index": {"type": "string"},
            "amount_basis": {"type": "string"},
            "confidence": {"type": "number"},
            "source_excerpt": {"type": "string"},
            "row_warnings": {"type": "array", "items": {"type": "string"}},
        }
    )
    return {
        "type": "object",
        "additionalProperties": False if strict else True,
        "properties": {
            "has_credit_claim_data": {"type": "boolean"},
            "block_summary": {"type": "string"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False if strict else True,
                    "properties": record_properties,
                    "required": [*FIELDS, *EXTRA_RECORD_FIELDS, "confidence", "source_excerpt", "row_warnings"],
                },
            },
        },
        "required": ["has_credit_claim_data", "block_summary", "records"],
    }


def build_prompt(source_image: str, block_no: int) -> str:
    fields = "、".join(FIELDS)
    return f"""
你是严谨的中文金融公告信息抽取员。请从这一张裁剪图中抽取债权/债权转让/催收公告里的明细数据。

来源图片：{source_image}
公告块编号：{block_no}

必须输出 JSON。records 内每条记录必须包含这些业务字段：{fields}。
同时每条记录还要包含 row_index、amount_basis、confidence、source_excerpt、row_warnings。

规则：
1. 图片可能旋转、倒置或倾斜，请先按视觉上可读的方向理解。
2. 只抽取债权、贷款、借款、债权转让、催收、资产包明细相关数据；普通新闻、声明、招标、仲裁送达公告等无关内容返回空 records。
3. 若一个公告块中有表格多行，每个表格行输出一条 record，不能把上一行的债务人、金额、保证人串到下一行。
4. 表格列优先级高于公告级文字：如果表格有逐行“受让人/受让方”列，受让方必须按该行列值填写；只有表格没有逐行受让方时，才使用公告级受让方。
5. 债务人、保证人、抵押物、质押物必须按列区分。不要把保证人、担保人、抵押人、出质人误拼到主债务人字段。
   - 如果原表列名是“担保人名称、担保人、担保方”，该列中列出的所有主体都填入“保证人”字段。
   - 如果主体后标注“（抵押人）”“（出质人）”，不要删除该主体，在“保证人”中保留角色标注，例如“某公司（抵押人）”。
   - “抵押物/质押物”只填具体财产、权利或物品，不填抵押人、出质人主体。
6. 本金余额只填原文明确为“本金、结欠本金、本金余额、贷款本金余额”的金额；“本息合计、债权合计、债权总额、转让债权合计”不是本金余额，不能直接填入本金余额，并在 row_warnings 说明金额口径不匹配。
7. 借款金额只填原文明确为“借款金额、贷款金额、借据金额、发放金额、原借款金额”的原始借款/贷款金额；如果原文只有“本金、本金余额、结欠本金、债权本金”列，不要把同一个金额复制到借款金额。
8. 如果同一行只有一个金额列，必须先判断列头口径：列头是本金/余额类时只填本金余额；列头是借款/发放类时只填借款金额；列头是本息/合计/债权总额类时两个字段都留空，并在 amount_basis、row_warnings 说明。
9. 字段别名只用于字段映射，不要根据相邻字段猜填：
   - 主债务人 = 借款人、客户名称。
   - 借款金额 = 贷款金额、本金总额、借款本金、贷款本金、借款额、初始本金。
   - 本金余额 = 剩余本金、接收时本金、结欠本金、贷款本金余额。
   - 保证人 = 担保人、担保方、担保人名称。
   - 贷款日 = 贷款发放日、借款日。
   - 诉讼状态 = 案件状态、执行状态、债权状态。
   - 债权人 = X持有Y结构中的X部分、X与Y结构中的Y部分、公告发布方、原债权人。
   - 受让方 = 接收方、买受人、购买方、受让人、X持有Y结构中的Y部分。
   - 转让方 = 出让方、委托方、委托人、出包方、原债权人。
10. 债权人/转让方/受让方角色必须按法律关系判断：
   - “原债权人、转让方、委托方、我单位、公告发布单位、银行/农商行将债权转让给某公司”通常是债权人或转让方。
   - “受让方、买受人、购买方、资产管理公司、债权转让给 A”中的 A 通常是受让方。
   - 如果原文是“A 将债权转让给 B”，A 填转让方，B 填受让方；债权人按原文公告口径填写，不能把受让方错填成债权人。
   - 如果原文是“根据 A 与 B 签订债权转让协议，B 将债权转让给 A”，则 B 是转让方/债权人，A 是受让方。
   - 拍卖公告中“我单位拟对所拥有的债权进行拍卖/转让”，落款单位通常是债权人或转让方，不是受让方。
11. 如果只看到金额列、利息列或表格右半边，但看不到该行的债务人/借款人列，不要输出该行记录；在 block_summary 或 row_warnings 说明疑似裁剪不完整。
12. 空字段填空字符串，不要猜测。多个保证人、抵押物、质押物用中文分号连接。
13. 金额输出为阿拉伯数字加“元”，去掉“人民币、￥、¥、元整”等修饰；原文为“万元”时换算成“元”。无法确定金额口径时保留空字段并写入 row_warnings。
14. amount_basis 填金额口径，例如：本金、结欠本金、本金余额、借款金额、本息合计、债权合计、未披露、无法判断。
15. row_index 填原表序号/第几宗/行号；原文没有序号则填空字符串。不要自己编造。
16. 日期尽量识别为原文日期，程序会再标准化。
17. 诉讼状态从诉讼、执行、仲裁、判决、未诉、已诉、调解等文字判断，不能判断则留空并在 row_warnings 说明。
18. confidence 取 0 到 1，反映该行字段对齐和识别可信度。
19. source_excerpt 填能支撑该行抽取的简短原文片段，不超过 80 个汉字，不要长篇复制。
""".strip()


def call_openai_extract(
    client: Any,
    crop_path: Path,
    model: str,
    source_image: str,
    block_no: int,
    timeout_retries: int,
    max_tokens: int,
) -> dict[str, Any]:
    prompt = build_prompt(source_image, block_no)
    data_url = image_to_data_url(crop_path)
    last_error: Exception | None = None

    for attempt in range(timeout_retries + 1):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "credit_claim_extraction",
                        "strict": True,
                        "schema": extraction_schema(),
                    }
                },
                max_output_tokens=max_tokens,
            )
            raw = response.output_text
            return parse_model_json(raw)
        except Exception as exc:  # OpenAI SDK raises typed errors across versions.
            last_error = exc
            if attempt < timeout_retries:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"OpenAI extraction failed for {crop_path}: {last_error}") from last_error


def call_ark_extract(
    client: Any,
    crop_path: Path,
    model: str,
    source_image: str,
    block_no: int,
    timeout_retries: int,
    max_tokens: int,
) -> dict[str, Any]:
    prompt = build_prompt(source_image, block_no)
    data_url = image_to_data_url(crop_path)
    last_error: Exception | None = None

    for attempt in range(timeout_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "credit_claim_extraction",
                        "strict": True,
                        "schema": extraction_schema(),
                    },
                },
                max_tokens=max_tokens,
            )
            raw = response.choices[0].message.content
            return parse_model_json(raw)
        except Exception as exc:
            last_error = exc
            error_text = str(exc)
            if re.search(r"response_format|json_schema|schema|structured", error_text, re.I):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt + "\n\n不要输出 Markdown，不要解释，只输出一个合法 JSON 对象。"},
                                    {"type": "image_url", "image_url": {"url": data_url}},
                                ],
                            }
                        ],
                        max_tokens=max_tokens,
                    )
                    return parse_model_json(response.choices[0].message.content)
                except Exception as fallback_exc:
                    last_error = fallback_exc
            if attempt < timeout_retries:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Ark extraction failed for {crop_path}: {last_error}") from last_error


def extract_block(
    provider: str,
    client: Any,
    crop_path: Path,
    model: str,
    source_image: str,
    block_no: int,
    timeout_retries: int,
    max_tokens: int,
) -> dict[str, Any]:
    if provider in {"ark", "ark_coding", "agnes", "compat", "xiaomi", "bailian"}:
        return call_ark_extract(client, crop_path, model, source_image, block_no, timeout_retries, max_tokens)
    return call_openai_extract(client, crop_path, model, source_image, block_no, timeout_retries, max_tokens)


def add_review(
    reviews: list[dict[str, Any]],
    image_name: str,
    block_no: int,
    issue_type: str,
    detail: str,
    confidence: Any,
    excerpt: str,
    crop_path: Path,
    bbox: Box,
) -> None:
    reviews.append(
        {
            "来源图片": image_name,
            "公告块编号": block_no,
            "问题类型": issue_type,
            "问题详情": detail,
            "置信度": confidence,
            "原文摘录": excerpt,
            "切块图片": str(crop_path),
            "bbox": json.dumps(bbox.as_list(), ensure_ascii=False),
        }
    )


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_images(input_dir: Path) -> list[Path]:
    extensions = {".jpg", ".jpeg", ".png"}
    return sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in extensions)


def cache_matches_run(payload: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.reuse_any_cache or args.cache_only:
        return True
    if args.reuse_success_cache and payload.get("status") == "ok":
        return True
    return payload.get("provider") == args.provider and payload.get("model") == args.model


def init_llm_client(args: argparse.Namespace, enabled: bool) -> Any | None:
    if not enabled:
        return None
    key_by_provider = {
        "ark": "ARK_API_KEY",
        "ark_coding": "ARK_CODING_API_KEY",
        "agnes": "AGNES_API_KEY",
        "compat": "COMPAT_API_KEY",
        "xiaomi": "XIAOMI_API_KEY",
        "bailian": "BAILIAN_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    key_name = args.api_key_env or key_by_provider[args.provider]
    api_key = os.getenv(key_name)
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed. Run pip install -r requirements.txt first.") from exc
    if args.provider in {"ark", "ark_coding", "agnes", "compat", "xiaomi", "bailian"}:
        return OpenAI(api_key=api_key, base_url=args.base_url, timeout=args.request_timeout)
    return OpenAI(api_key=api_key, timeout=args.request_timeout)


def crop_for_api(image: np.ndarray, box: Box, max_side: int) -> np.ndarray:
    crop = image[box.y : box.y + box.h, box.x : box.x + box.w]
    return resize_for_api(crop, max_side)


def warm_model_cache(
    image: np.ndarray,
    image_path: Path,
    boxes: list[Box],
    output_dir: Path,
    args: argparse.Namespace,
    client: Any,
) -> None:
    if args.workers <= 1:
        return

    crop_dir = output_dir / "crops" / image_path.stem
    cache_dir = output_dir / "cache" / image_path.stem
    jobs: list[tuple[int, Box, Path, Path]] = []
    for block_no, box in enumerate(boxes, start=1):
        if args.limit_blocks_per_image and block_no > args.limit_blocks_per_image:
            break
        crop_path = crop_dir / f"block_{block_no:03d}.jpg"
        cache_path = cache_dir / f"block_{block_no:03d}.json"
        crop = crop_for_api(image, box, args.max_crop_side)
        write_image(crop_path, crop, quality=args.jpeg_quality)
        if not args.no_cache and cache_path.exists():
            try:
                cached_payload = load_json(cache_path)
                if cache_matches_run(cached_payload, args):
                    continue
            except Exception:
                pass
        jobs.append((block_no, box, crop_path, cache_path))

    if not jobs:
        return

    print(f"  Warming {len(jobs)} uncached blocks with {args.workers} workers...")

    def run_job(job: tuple[int, Box, Path, Path]) -> tuple[int, Box, Path, Path, dict[str, Any] | None, str | None]:
        block_no, box, crop_path, cache_path = job
        try:
            result = extract_block(
                provider=args.provider,
                client=client,
                crop_path=crop_path,
                model=args.model,
                source_image=image_path.name,
                block_no=block_no,
                timeout_retries=args.retries,
                max_tokens=args.max_tokens,
            )
            return block_no, box, crop_path, cache_path, result, None
        except Exception as exc:
            return block_no, box, crop_path, cache_path, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(run_job, job): job for job in jobs}
        for future in as_completed(future_map):
            block_no, box, crop_path, cache_path, result, error = future.result()
            if result is not None:
                save_json(
                    cache_path,
                    {
                        "status": "ok",
                        "provider": args.provider,
                        "model": args.model,
                        "source_image": image_path.name,
                        "block_no": block_no,
                        "crop_path": str(crop_path),
                        "bbox": box.as_list(),
                        "result": result,
                    },
                )
                print(f"    cached block {block_no:03d}")
            else:
                save_json(
                    cache_path,
                    {
                        "status": "error",
                        "provider": args.provider,
                        "model": args.model,
                        "source_image": image_path.name,
                        "block_no": block_no,
                        "crop_path": str(crop_path),
                        "bbox": box.as_list(),
                        "error": error or "unknown error",
                    },
                )
                print(f"    error block {block_no:03d}: {error}")


def process_image(
    image_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    client: Any | None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    image = read_image(image_path)
    height, width = image.shape[:2]
    boxes = [Box(0, 0, width, height)] if args.whole_image else detect_blocks(image, args.min_area_ratio)

    debug_path = output_dir / "debug" / f"{image_path.stem}_boxes.jpg"
    write_image(debug_path, draw_debug_boxes(image, boxes), quality=90)

    crop_dir = output_dir / "crops" / image_path.stem
    cache_dir = output_dir / "cache" / image_path.stem
    rows: list[dict[str, str]] = []
    trace_rows: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []

    model_enabled = client is not None and not args.prepare_only and not args.cache_only
    if model_enabled:
        warm_model_cache(image, image_path, boxes, output_dir, args, client)

    for block_no, box in enumerate(boxes, start=1):
        if args.limit_blocks_per_image and block_no > args.limit_blocks_per_image:
            break
        seen_in_block: set[tuple[str, ...]] = set()
        empty_record_reviewed = False
        crop = crop_for_api(image, box, args.max_crop_side)
        crop_path = crop_dir / f"block_{block_no:03d}.jpg"
        cache_path = cache_dir / f"block_{block_no:03d}.json"
        write_image(crop_path, crop, quality=args.jpeg_quality)

        cached_payload: dict[str, Any] | None = None
        if not args.no_cache and cache_path.exists():
            try:
                cached_payload = load_json(cache_path)
                if not cache_matches_run(cached_payload, args):
                    cached_payload = None
            except Exception as exc:
                add_review(
                    reviews,
                    image_path.name,
                    block_no,
                    "缓存读取失败",
                    str(exc),
                    "",
                    "",
                    crop_path,
                    box,
                )

        if cached_payload is not None:
            if cached_payload.get("status") == "ok":
                result = cached_payload.get("result", {})
                result_provider = cached_payload.get("provider", "")
                result_model = cached_payload.get("model", "")
            else:
                add_review(
                    reviews,
                    image_path.name,
                    block_no,
                    "模型调用失败",
                    normalize_text(cached_payload.get("error", "cached error")),
                    "",
                    "",
                    crop_path,
                    box,
                )
                continue
        elif args.cache_only:
            add_review(
                reviews,
                image_path.name,
                block_no,
                "缺少缓存",
                "cache-only 模式未发现该块的模型结果缓存。",
                "",
                "",
                crop_path,
                box,
            )
            continue
        elif not model_enabled:
            add_review(
                reviews,
                image_path.name,
                block_no,
                "未调用模型",
                "prepare-only 或缺少当前 provider 的 API key；已完成切块，待抽取。",
                "",
                "",
                crop_path,
                box,
            )
            continue
        else:
            try:
                result = extract_block(
                    provider=args.provider,
                    client=client,
                    crop_path=crop_path,
                    model=args.model,
                    source_image=image_path.name,
                    block_no=block_no,
                    timeout_retries=args.retries,
                    max_tokens=args.max_tokens,
                )
                save_json(
                    cache_path,
                    {
                        "status": "ok",
                        "provider": args.provider,
                        "model": args.model,
                        "source_image": image_path.name,
                        "block_no": block_no,
                        "crop_path": str(crop_path),
                        "bbox": box.as_list(),
                        "result": result,
                    },
                )
                result_provider = args.provider
                result_model = args.model
            except Exception as exc:
                save_json(
                    cache_path,
                    {
                        "status": "error",
                        "provider": args.provider,
                        "model": args.model,
                        "source_image": image_path.name,
                        "block_no": block_no,
                        "crop_path": str(crop_path),
                        "bbox": box.as_list(),
                        "error": str(exc),
                    },
                )
                add_review(
                    reviews,
                    image_path.name,
                    block_no,
                    "模型调用失败",
                    str(exc),
                    "",
                    "",
                    crop_path,
                    box,
                )
                continue

        records = result.get("records", [])
        if not isinstance(records, list) or not records:
            add_review(
                reviews,
                image_path.name,
                block_no,
                "空结果",
                normalize_text(result.get("block_summary", "该块未抽取出目标债权记录。")),
                "",
                "",
                crop_path,
                box,
            )
            time.sleep(args.sleep)
            continue

        for raw_record in records:
            if not isinstance(raw_record, dict):
                add_review(
                    reviews,
                    image_path.name,
                    block_no,
                    "非对象记录",
                    normalize_text(raw_record),
                    "",
                    "",
                    crop_path,
                    box,
                )
                continue

            row = normalize_record(raw_record)
            confidence = raw_record.get("confidence", "")
            excerpt = normalize_text(raw_record.get("source_excerpt", ""))
            row_index_text = normalize_text(raw_record.get("row_index", ""))
            amount_basis_text = normalize_text(raw_record.get("amount_basis", ""))
            warnings = normalize_warning_list(raw_record.get("row_warnings", []))

            if not any(row.get(field) for field in FIELDS):
                if not empty_record_reviewed:
                    detail = (
                        f"模型返回了记录，但 {len(FIELDS)} 个业务字段均为空；通常是裁剪块只包含金额/利息/本息合计，"
                        "缺少主债务人或字段口径不符合要求。该块不作为重复记录处理，也不进入主表。"
                    )
                    add_review(
                        reviews,
                        image_path.name,
                        block_no,
                        "无有效业务字段",
                        detail,
                        confidence,
                        excerpt,
                        crop_path,
                        box,
                    )
                    empty_record_reviewed = True
                continue

            row_key = tuple(row[field] for field in FIELDS) + (row_index_text, amount_basis_text, excerpt)
            if row_key in seen_in_block:
                warnings.append("模型在同一公告块返回完全相同候选，已保留到追溯表并交由结果层去重")
            else:
                seen_in_block.add(row_key)
            rows.append(row)

            trace_rows.append(
                {
                    "来源图片": image_path.name,
                    "公告块编号": f"{block_no:03d}",
                    "切块图片": str(crop_path),
                    "bbox": json.dumps(box.as_list(), ensure_ascii=False),
                    "provider": result_provider,
                    "model": result_model,
                    "明细序号": row_index_text,
                    **row,
                    "金额口径": amount_basis_text,
                    "置信度": confidence,
                    "原文摘录": excerpt,
                    "行内警告": "；".join(warnings),
                }
            )
            missing = [field for field in KEY_FIELDS if not row.get(field)]
            issues = []

            try:
                numeric_confidence = float(confidence)
            except (TypeError, ValueError):
                numeric_confidence = 0.0

            if numeric_confidence < args.confidence_threshold:
                issues.append(f"置信度低于阈值 {args.confidence_threshold}")
            if len(missing) >= 2:
                issues.append(f"关键字段缺失:{'、'.join(missing)}")
            if looks_like_bad_amount(row["借款金额"]):
                issues.append("借款金额没有数字")
            if looks_like_bad_amount(row["本金余额"]):
                issues.append("本金余额没有数字")
            if looks_like_bad_date(row["贷款日"]):
                issues.append("贷款日不是 YYYY-MM-DD")
            if looks_like_bad_date(row["到期日"]):
                issues.append("到期日不是 YYYY-MM-DD")
            issues.extend(suspicious_field_label_leak(row))
            issues.extend(warnings)

            if issues:
                add_review(
                    reviews,
                    image_path.name,
                    block_no,
                    "记录需复核",
                    "；".join(issues),
                    confidence,
                    excerpt,
                    crop_path,
                    box,
                )
        time.sleep(args.sleep)

    stats = {
        "来源图片": image_path.name,
        "切块数量": len(boxes),
        "成功抽取记录数": len(rows),
        "复核问题数": len(reviews),
        "是否调用模型": "是" if model_enabled else "否",
    }
    return rows, trace_rows, reviews, stats


def core_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(normalize_text(row.get(field, "")) for field in FIELDS)


def nonempty_fields(row: dict[str, Any]) -> list[str]:
    return [field for field in FIELDS if normalize_text(row.get(field, ""))]


def amount_number_key(value: Any) -> str:
    text = normalize_text(value).replace(",", "").replace("，", "")
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if len(numbers) != 1:
        return ""
    try:
        normalized = format(Decimal(numbers[0]).normalize(), "f")
    except InvalidOperation:
        return numbers[0]
    return normalized.rstrip("0").rstrip(".") or "0"


def append_trace_warning(row: dict[str, Any], warning: str) -> None:
    existing = normalize_text(row.get("行内警告", ""))
    if warning in existing:
        return
    row["行内警告"] = f"{existing}；{warning}" if existing else warning


def apply_amount_basis_guard(row: dict[str, Any]) -> None:
    loan_amount_key = amount_number_key(row.get("借款金额", ""))
    balance_key = amount_number_key(row.get("本金余额", ""))
    if not loan_amount_key or not balance_key or loan_amount_key != balance_key:
        return

    amount_basis = normalize_text(row.get("金额口径", ""))
    loan_terms = ["借款金额", "贷款金额", "借据金额", "发放金额", "原借款金额"]
    principal_terms = ["本金", "本金余额", "结欠本金", "贷款本金余额", "债权本金", "转让本金"]
    total_terms = ["本息合计", "债权合计", "债权总额", "转让债权合计"]
    has_loan_basis = any(term in amount_basis for term in loan_terms)
    has_principal_basis = any(term in amount_basis for term in principal_terms)
    has_total_basis = any(term in amount_basis for term in total_terms)

    if has_total_basis:
        row["借款金额"] = ""
        row["本金余额"] = ""
        append_trace_warning(row, "金额口径为合计类，已清空借款金额和本金余额")
    elif has_loan_basis and not has_principal_basis:
        row["本金余额"] = ""
        append_trace_warning(row, "借款金额与本金余额相同，按金额口径保留借款金额")
    elif has_principal_basis and not has_loan_basis:
        row["借款金额"] = ""
        append_trace_warning(row, "借款金额与本金余额相同，按金额口径保留本金余额")


def row_sequence_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    row_index = normalize_text(row.get("明细序号", ""))
    match = re.search(r"\d+", row_index)
    if not match:
        return None
    return (
        normalize_text(row.get("来源图片", "")),
        normalize_text(row.get("公告块编号", "")),
        match.group(0),
    )


def row_quality_score(row: dict[str, Any]) -> tuple[int, float, int, int]:
    try:
        confidence = float(normalize_text(row.get("置信度", "")) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        len(nonempty_fields(row)),
        confidence,
        len(normalize_text(row.get("原文摘录", ""))),
        len(normalize_text(row.get("主债务人", ""))),
    )


def postprocess_trace_rows(
    trace_rows: list[dict[str, Any]], keep_sparse_duplicates: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    exact_seen: set[tuple[str, ...]] = set()

    for row in trace_rows:
        apply_amount_basis_guard(row)

    complete_balance_by_image: dict[str, set[str]] = {}
    for row in trace_rows:
        image_name = normalize_text(row.get("来源图片", ""))
        debtor = normalize_text(row.get("主债务人", ""))
        balance = normalize_text(row.get("本金余额", ""))
        if image_name and debtor and balance:
            complete_balance_by_image.setdefault(image_name, set()).add(balance.replace(",", "").replace("，", ""))

    for row_index, row in enumerate(trace_rows):
        key = (
            normalize_text(row.get("来源图片", "")),
            normalize_text(row.get("公告块编号", "")),
            normalize_text(row.get("明细序号", "")),
            *core_key(row),
        )
        if key in exact_seen:
            removed.append({"剔除原因": "业务字段完全重复记录，保留首次出现", **row})
            continue
        exact_seen.add(key)

        image_name = normalize_text(row.get("来源图片", ""))
        debtor = normalize_text(row.get("主债务人", ""))
        balance = normalize_text(row.get("本金余额", ""))
        normalized_balance = balance.replace(",", "").replace("，", "")
        if (
            not keep_sparse_duplicates
            and not debtor
            and balance
            and normalized_balance in complete_balance_by_image.get(image_name, set())
        ):
            removed.append({"剔除原因": "疑似半截/重叠表格：同图存在同本金余额且有主债务人的完整记录", **row})
            continue

        if not debtor:
            removed.append({"剔除原因": "无主债务人，无法形成债权明细记录", **row})
            continue

        kept.append(row)

    return kept, removed


def add_issue_row(
    issues: list[dict[str, Any]],
    row_no: Any,
    issue_type: str,
    detail: str,
    row: dict[str, Any],
) -> None:
    issues.append(
        {
            "行号": row_no,
            "问题类型": issue_type,
            "问题详情": detail,
            "来源图片": row.get("来源图片", ""),
            "公告块编号": row.get("公告块编号", ""),
            "债权人": row.get("债权人", ""),
            "主债务人": row.get("主债务人", ""),
            "借款金额": row.get("借款金额", ""),
            "本金余额": row.get("本金余额", ""),
            "受让方": row.get("受让方", ""),
            "转让方": row.get("转让方", ""),
            "置信度": row.get("置信度", ""),
            "原文摘录": row.get("原文摘录", ""),
            "切块图片": row.get("切块图片", ""),
        }
    )


def build_issue_rows(trace_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row_no, row in enumerate(trace_rows, start=2):
        present = nonempty_fields(row)
        creditor = normalize_text(row.get("债权人", ""))
        debtor = normalize_text(row.get("主债务人", ""))
        loan_amount = normalize_text(row.get("借款金额", ""))
        balance = normalize_text(row.get("本金余额", ""))
        if len(present) <= 3:
            add_issue_row(issues, row_no, "字段过少", f"非空字段数<=3:{'、'.join(present)}", row)
        if not creditor and debtor:
            add_issue_row(issues, row_no, "缺债权人", "已有主债务人但债权人为空，需回看公告标题/落款/转让关系", row)
        if not debtor and balance:
            add_issue_row(issues, row_no, "缺主债务人但有本金余额", "疑似半截表格、裁剪不完整或模型漏识别", row)
        if creditor and creditor == normalize_text(row.get("受让方", "")):
            add_issue_row(issues, row_no, "债权人与受让方相同", "如原表存在逐行受让人列，需重点复核是否错填", row)
        transferor = normalize_text(row.get("转让方", ""))
        assignee = normalize_text(row.get("受让方", ""))
        if transferor and assignee and transferor == assignee:
            add_issue_row(issues, row_no, "转让方与受让方相同", "转让关系两端相同，需回看原文确认是否角色错填", row)
        try:
            if normalize_text(row.get("置信度", "")) and float(row.get("置信度")) < 0.75:
                add_issue_row(issues, row_no, "低置信度", "confidence < 0.75", row)
        except (TypeError, ValueError):
            add_issue_row(issues, row_no, "置信度异常", "confidence 不是有效数字", row)
        if "；" in normalize_text(row.get("借款金额", "")) or ";" in normalize_text(row.get("借款金额", "")):
            add_issue_row(issues, row_no, "借款金额多金额", "同一字段含多个金额，需确认是否表示多笔贷款或应拆行", row)

        amount_basis = normalize_text(row.get("金额口径", ""))
        excerpt = normalize_text(row.get("原文摘录", ""))
        total_terms = ["本息合计", "债权合计", "债权总额", "转让债权合计"]
        loan_amount_key = amount_number_key(loan_amount)
        balance_key = amount_number_key(balance)
        if loan_amount_key and balance_key and loan_amount_key == balance_key:
            add_issue_row(issues, row_no, "借款金额与本金余额相同待确认", "两列金额完全相同，需确认是否模型把同一金额复制到两列", row)
        if row.get("本金余额") and any(term in amount_basis for term in total_terms):
            add_issue_row(issues, row_no, "本金余额口径异常", f"金额口径={amount_basis}", row)
        if row.get("本金余额") and not amount_basis and any(term in excerpt for term in total_terms) and "本金" not in excerpt:
            add_issue_row(issues, row_no, "本金余额口径待确认", "原文摘录出现合计类金额但未给出 amount_basis", row)

    block_rows: dict[tuple[str, str], list[tuple[int, dict[str, Any], int]]] = {}
    for row_no, row in enumerate(trace_rows, start=2):
        row_index = normalize_text(row.get("明细序号", ""))
        match = re.search(r"\d+", row_index)
        if not match:
            continue
        block_key = (normalize_text(row.get("来源图片", "")), normalize_text(row.get("公告块编号", "")))
        block_rows.setdefault(block_key, []).append((row_no, row, int(match.group(0))))

    for (_image, _block), rows in block_rows.items():
        numbers = sorted({number for _row_no, _row, number in rows})
        duplicate_numbers = sorted({number for _row_no, _row, number in rows if sum(1 for _rn, _r, n in rows if n == number) > 1})
        for duplicate_number in duplicate_numbers:
            first_row_no, first_row, _ = next((row_no, row, number) for row_no, row, number in rows if number == duplicate_number)
            add_issue_row(
                issues,
                first_row_no,
                "明细序号重复待复核",
                f"同一公告块内序号 {duplicate_number} 出现多条记录，可能是分段重叠或模型重复抽取，未自动剔除",
                first_row,
            )
        if len(numbers) >= 3 and numbers[-1] - numbers[0] + 1 != len(numbers):
            missing = [str(n) for n in range(numbers[0], numbers[-1] + 1) if n not in set(numbers)]
            first_row_no, first_row, _ = rows[0]
            add_issue_row(issues, first_row_no, "明细序号不连续", f"疑似漏行，缺少序号:{'、'.join(missing[:20])}", first_row)

    return issues


def main() -> int:
    args = parse_args()
    if args.smoke_test:
        args.limit_images = 1
        args.limit_blocks_per_image = 1
        args.no_cache = True
        args.workers = 1

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    images = find_images(input_dir)
    if args.limit_images:
        images = images[: args.limit_images]

    if not images:
        print(f"No images found in {input_dir}", file=sys.stderr)
        return 2

    api_requested = not args.prepare_only and not args.cache_only
    if api_requested and not args.model:
        print(f"{args.provider} model is missing; set the provider-specific MODEL environment variable or pass --model.", file=sys.stderr)
        return 2
    client = init_llm_client(args, api_requested)
    if api_requested and client is None:
        key_name = args.api_key_env or {
            "ark": "ARK_API_KEY",
            "ark_coding": "ARK_CODING_API_KEY",
            "agnes": "AGNES_API_KEY",
            "compat": "COMPAT_API_KEY",
            "xiaomi": "XIAOMI_API_KEY",
            "bailian": "BAILIAN_API_KEY",
            "openai": "OPENAI_API_KEY",
        }[args.provider]
        print(f"{key_name} is missing; running in prepare-only mode.", file=sys.stderr)

    all_trace_rows: list[dict[str, Any]] = []
    all_reviews: list[dict[str, Any]] = []
    all_stats: list[dict[str, Any]] = []

    for index, image_path in enumerate(images, start=1):
        print(f"[{index}/{len(images)}] Processing {image_path.name}")
        _rows, trace_rows, reviews, stats = process_image(image_path, output_dir, args, client)
        all_trace_rows.extend(trace_rows)
        all_reviews.extend(reviews)
        all_stats.append(stats)

    kept_trace_rows, removed_rows = postprocess_trace_rows(all_trace_rows, args.keep_sparse_duplicates)
    all_rows = [{field: row.get(field, "") for field in FIELDS} for row in kept_trace_rows]
    issue_rows = build_issue_rows(kept_trace_rows)

    write_csv(output_dir / "债权清洗结果.csv", all_rows, FIELDS)
    write_csv(output_dir / "债权清洗结果_带来源.csv", kept_trace_rows, TRACE_COLUMNS)
    write_csv(output_dir / "清洗结果问题清单.csv", issue_rows, ISSUE_COLUMNS)
    write_csv(output_dir / "剔除记录.csv", removed_rows, REMOVED_COLUMNS)
    write_csv(output_dir / "复核清单.csv", all_reviews, REVIEW_COLUMNS)
    write_csv(output_dir / "处理统计.csv", all_stats, STAT_COLUMNS)

    print(f"Done. Records: {len(all_rows)}; removed: {len(removed_rows)}; review issues: {len(all_reviews)}; result issues: {len(issue_rows)}")
    print(f"Output folder: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
