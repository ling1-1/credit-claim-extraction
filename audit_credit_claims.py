from __future__ import annotations

import argparse
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw


TRACE_FILE = "债权清洗结果_带来源.csv"
ISSUE_FILE = "清洗结果问题清单.csv"
SAMPLE_FILE = "随机抽检样本.csv"
REPORT_FILE = "抽检报告.md"
CONTACT_SHEET_FILE = "随机抽检切块预览.jpg"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a random audit report for credit-claim extraction results.")
    parser.add_argument("--output-dir", default="outputs_image_whole", help="Folder containing extraction CSV outputs.")
    parser.add_argument("--sample-size", type=int, default=8, help="Number of rows to sample.")
    parser.add_argument("--seed", type=int, default=20260612, help="Stable random seed.")
    return parser.parse_args()


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def select_samples(trace_df: pd.DataFrame, sample_size: int, seed: int) -> pd.DataFrame:
    if trace_df.empty or sample_size <= 0:
        return pd.DataFrame(columns=trace_df.columns)

    rng = random.Random(seed)
    selected_indices: list[int] = []
    grouped: dict[str, list[int]] = {}
    for idx, row in trace_df.iterrows():
        grouped.setdefault(normalize(row.get("来源图片")), []).append(idx)

    for image_name in sorted(grouped):
        if len(selected_indices) >= sample_size:
            break
        selected_indices.append(rng.choice(grouped[image_name]))

    remaining = [idx for idx in trace_df.index.tolist() if idx not in set(selected_indices)]
    rng.shuffle(remaining)
    selected_indices.extend(remaining[: max(0, sample_size - len(selected_indices))])
    return trace_df.loc[selected_indices].copy()


def write_contact_sheet(sample_df: pd.DataFrame, output_dir: Path) -> Path | None:
    if sample_df.empty:
        return None

    thumbs: list[tuple[Image.Image, str]] = []
    for sample_no, (_, row) in enumerate(sample_df.iterrows(), start=1):
        crop_path = Path(normalize(row.get("切块图片")))
        if not crop_path.exists():
            continue
        try:
            image = Image.open(crop_path).convert("RGB")
        except Exception:
            continue
        image.thumbnail((360, 280))
        label = f"{sample_no}. {normalize(row.get('来源图片'))} / block {normalize(row.get('公告块编号'))}"
        thumbs.append((image.copy(), label))

    if not thumbs:
        return None

    cols = 2
    cell_w, cell_h = 420, 340
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(sheet)

    for idx, (thumb, label) in enumerate(thumbs):
        col = idx % cols
        row = idx // cols
        x = col * cell_w + 20
        y = row * cell_h + 40
        draw.text((x, row * cell_h + 12), label[:62], fill=(20, 20, 20))
        sheet.paste(thumb, (x, y))

    path = output_dir / CONTACT_SHEET_FILE
    sheet.save(path, quality=90)
    return path


def issue_summary(issue_df: pd.DataFrame) -> list[tuple[str, int]]:
    if issue_df.empty or "问题类型" not in issue_df.columns:
        return []
    counter = Counter(normalize(value) for value in issue_df["问题类型"].tolist() if normalize(value))
    return counter.most_common()


def write_report(trace_df: pd.DataFrame, issue_df: pd.DataFrame, sample_df: pd.DataFrame, output_dir: Path, contact_sheet: Path | None) -> Path:
    source_count = trace_df["来源图片"].nunique() if "来源图片" in trace_df.columns and not trace_df.empty else 0
    issue_rows = issue_summary(issue_df)
    lines = [
        "# 债权清洗随机抽检报告",
        "",
        f"- 抽取记录数：{len(trace_df)}",
        f"- 覆盖来源图片数：{source_count}",
        f"- 问题清单条数：{len(issue_df)}",
        f"- 抽检样本数：{len(sample_df)}",
    ]
    if contact_sheet:
        lines.append(f"- 抽检切块预览：{contact_sheet.resolve()}")
    lines.append("")

    lines.append("## 问题类型统计")
    if issue_rows:
        for issue_type, count in issue_rows:
            lines.append(f"- {issue_type}: {count}")
    else:
        lines.append("- 未发现问题清单记录")
    lines.append("")

    lines.append("## 抽检样本")
    if sample_df.empty:
        lines.append("无可抽检记录。")
    else:
        for sample_no, (_, row) in enumerate(sample_df.iterrows(), start=1):
            lines.append(
                f"{sample_no}. 来源={normalize(row.get('来源图片'))}，块={normalize(row.get('公告块编号'))}，"
                f"序号={normalize(row.get('明细序号'))}，主债务人={normalize(row.get('主债务人'))}，"
                f"借款金额={normalize(row.get('借款金额'))}，本金余额={normalize(row.get('本金余额'))}，"
                f"转让方={normalize(row.get('转让方'))}，受让方={normalize(row.get('受让方'))}，"
                f"警告={normalize(row.get('行内警告')) or '无'}"
            )
    lines.append("")
    lines.append("## 抽检建议")
    lines.append("- 优先核对大表格样本的明细序号、主债务人、金额列是否逐行对齐。")
    lines.append("- 对字段过少、金额口径异常、角色相同等问题，回看切块图和原文摘录后再决定是否入正式表。")

    report_path = output_dir / REPORT_FILE
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    trace_df = read_csv(output_dir / TRACE_FILE)
    issue_df = read_csv(output_dir / ISSUE_FILE)
    sample_df = select_samples(trace_df, args.sample_size, args.seed)
    sample_path = output_dir / SAMPLE_FILE
    sample_df.to_csv(sample_path, index=False, encoding="utf-8-sig")
    contact_sheet = write_contact_sheet(sample_df, output_dir)
    report_path = write_report(trace_df, issue_df, sample_df, output_dir, contact_sheet)
    print(f"Audit sample: {sample_path.resolve()}")
    print(f"Audit report: {report_path.resolve()}")
    if contact_sheet:
        print(f"Contact sheet: {contact_sheet.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
