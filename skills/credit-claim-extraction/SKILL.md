---
name: credit-claim-extraction
description: Use when processing Chinese debt/credit-claim announcement images into structured CSVs or MySQL staging tables, especially workflows that need whole-image extraction, source traceability, field alias mapping, review/audit reports, or Ark CodingPlan/OpenAI-compatible vision providers.
metadata:
  short-description: Extract Chinese credit-claim data from images
---

# Credit Claim Extraction

Use the project scripts in `F:\test_0608\ceshi_pic` unless the user gives another workspace. Do not write API keys, database passwords, or provider secrets into code, docs, logs, PPTs, or final answers.

## Workflow

1. Prepare images:
   ```powershell
   python .\credit_claim_skill.py prepare --input-dir .\image --output-dir .\outputs_image_whole
   ```
2. Smoke test the preferred provider:
   ```powershell
   python .\credit_claim_skill.py smoke-test --input-dir .\image --output-dir .\outputs_image_whole_smoke --provider auto
   ```
   `auto` prefers `ARK_CODING_API_KEY` / Ark CodingPlan, then falls back to other configured providers.
3. Extract all records:
   ```powershell
   python .\credit_claim_skill.py extract --input-dir .\image --output-dir .\outputs_image_whole --provider auto --smoke-first
   ```
4. Audit:
   ```powershell
   python .\credit_claim_skill.py audit --output-dir .\outputs_image_whole --sample-size 8
   ```
5. Load to MySQL staging only after CSV review:
   ```powershell
   python .\credit_claim_skill.py load-db --output-dir .\outputs_image_whole --input-dir .\image
   ```

## Required Environment Variables

- Ark CodingPlan: `ARK_CODING_API_KEY`, `ARK_CODING_BASE_URL`, `ARK_CODING_MODEL`.
- Fallback providers as needed: `ARK_API_KEY`, `ARK_MODEL`, `XIAOMI_API_KEY`, `XIAOMI_MODEL`, etc.
- MySQL staging: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`.

Use `load-db --check` for `SELECT 1` only, and `load-db --dry-run` to validate CSV counts without connecting.

## Field Rules

Extract 12 business fields:
`债权人、主债务人、借款金额、本金余额、保证人、抵押物、质押物、贷款日、到期日、诉讼状态、受让方、转让方`.

Default image mode is whole-image: every source image is one block with `bbox=[0,0,width,height]`. Only segment long tables when JSON truncation, row loss, or image-size failure occurs.

Keep source traceability in `债权清洗结果_带来源.csv`: source image, block number, crop path, bbox, provider, model, detail sequence, amount basis, confidence, source excerpt, and row warnings.

Do not guess blank fields. Use field aliases only to map source labels to the target schema. Keep ambiguous amount basis out of `借款金额` / `本金余额` and let the issue list flag it.
