# Credit Claim Image Extraction Pipeline

用于将中文债权公告图片清洗抽取为结构化 CSV，并可写入 MySQL staging 暂存表。

## 能力

- 整图优先处理已裁好的公告/表格图片。
- 调用 OpenAI-compatible 视觉模型抽取债权字段。
- 输出 12 个业务字段：债权人、主债务人、借款金额、本金余额、保证人、抵押物、质押物、贷款日、到期日、诉讼状态、受让方、转让方。
- 生成带来源追溯 CSV、问题清单、随机抽检报告、逐条检查报告。
- 支持 MySQL staging 入库，并保留字段注释。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
Copy-Item .\.env.example .\.env
```

编辑 `.env`，填写模型和 MySQL 配置。

```powershell
.\.venv\Scripts\python.exe .\credit_claim_skill.py prepare --input-dir .\image --output-dir .\outputs_image_whole
.\.venv\Scripts\python.exe .\credit_claim_skill.py extract --input-dir .\image --output-dir .\outputs_image_whole --provider auto --smoke-first
.\.venv\Scripts\python.exe .\credit_claim_skill.py audit --output-dir .\outputs_image_whole --sample-size 8
.\.venv\Scripts\python.exe .\verify_credit_claim_results.py --output-dir .\outputs_image_whole
.\.venv\Scripts\python.exe .\credit_claim_skill.py load-db --output-dir .\outputs_image_whole --input-dir .\image
```

## 不包含内容

本仓库不包含图片、CSV 结果、模型缓存、数据库密码、API Key 或 `.env`。

字段口径见 `docs/字段说明.csv`。
