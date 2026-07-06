# AI 模型配置与切换

本项目不在代码中写入真实 API Key。模型配置支持三种来源，优先级如下：

1. 命令行参数：适合临时测试，优先级最高。
2. MySQL 表 `ai_model_profiles`：适合本地或生产长期运行。
3. `.env` 文件：适合本地开发测试。

如果三处都没有可用 API Key，采集程序会自动关闭 AI 提取，只保留规则/API 提取。

## .env 配置

复制项目根目录的 `.env.example` 为 `.env`，然后填写本机密钥：

```env
AI_ACTIVE_PROFILE=qwen

AI_QWEN_API_KEY=你的千问密钥
AI_QWEN_MODEL_NAME=qwen-plus
AI_QWEN_VISION_MODEL=qwen-vl-plus
AI_QWEN_BASE_URL=https://dashscope.aliyuncs.com

AI_DEEPSEEK_API_KEY=你的 DeepSeek 密钥
AI_DEEPSEEK_MODEL_NAME=deepseek-chat
AI_DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`.env` 已加入 `.gitignore`，不要提交真实密钥。

## MySQL 配置

推荐 MySQL 只保存环境变量名，真实密钥仍放在 `.env` 或系统环境变量里：

```sql
INSERT INTO ai_model_profiles
  (profile_name, provider, model_name, vision_model_name, base_url, api_key_env_var,
   timeout_seconds, max_retries, qps, enabled, is_default, note)
VALUES
  ('qwen_default', 'qwen', 'qwen-plus', 'qwen-vl-plus', 'https://dashscope.aliyuncs.com',
   'AI_QWEN_API_KEY', 0, 0, 10, 1, 1, '千问默认文本和视觉提取模型');
```

本地测试也可以直接写入 `api_key_value`，但不建议在生产环境这样做。

## 命令示例

京东采集：

```powershell
python jd_scraper_v2.py crawl --ai-profile qwen_default
```

多平台采集：

```powershell
python multi_platform_runner.py crawl --platform all --ai-mode async --ai-profile qwen_default
```

异步补提取：

```powershell
python multi_platform_runner.py ai-enrich --ai-profile qwen_default --limit 50
```

临时切换 DeepSeek：

```powershell
python jd_scraper_v2.py crawl --ai-provider deepseek --ai-model-name deepseek-chat --ai-api-key $env:AI_DEEPSEEK_API_KEY
```

## 字段含义

- `profile_name`：模型配置名称，可在命令行用 `--ai-profile` 指定。
- `provider`：供应商，目前支持 `qwen`、`deepseek`、`openai`。
- `model_name`：文本字段提取模型。
- `vision_model_name`：图片 OCR/视觉兜底模型。知识产权明细、图片表格等建议使用千问或 OpenAI 视觉模型。
- `base_url`：OpenAI 兼容接口地址。
- `api_key_env_var`：从环境变量读取密钥，推荐。
- `api_key_value`：直接存密钥，仅适合本地临时测试。
- `timeout_seconds`：`0` 表示不设置本地超时。
- `max_retries`：失败重试次数。
- `qps`：每秒最大调用次数。
- `is_default`：没有显式指定 profile 时使用的默认启用配置。
