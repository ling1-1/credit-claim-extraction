# 多平台资产拍卖数据采集系统

本项目用于采集、提取、校验和预览多平台资产拍卖数据。当前主链路是：

```text
平台列表页/详情页/API/浏览器兜底
  -> 原始 JSON/HTML/页面文本归档
  -> AI 主提取 + 规则/API 验真
  -> MySQL V2 正式表结构
  -> Web Viewer 质量核验
```

系统重点不是只保存网页文本，而是把每个字段的最终值、来源证据、附件/图片/视频、债权明细、知识产权明细和质量报告一并保存，方便后续复核。

## 当前能力

- 支持平台：
  - 京东资产拍卖：`jd`
  - e交易：`ejy365`
  - 阿里资产/拍卖：`ali`
  - 重庆联交所/山东产权公开门户链路：`cquae`
  - 天津产权交易中心：`tpre`
  - 北交所/产权交易平台：`prechina`
  - 广西产权交易所：`gxcq`
- 支持资产类型：房地产、土地、车辆、设备、债权、股权、知识产权、物资产品、用益物权、其他。
- 支持 MySQL V2 正式表结构直写。
- 支持 AI 同步提取或异步补提取。
- 支持多平台并发和单平台多标的并发。
- 支持附件、图片、视频统一写入 `item_resources`。
- 支持字段证据追溯，所有字段候选值进入 `field_extractions`。
- 支持每次采集后生成模型采集数据质量报告。
- 保留 SQLite 兼容入口，但不再作为主存储方案。

## 目录结构

```text
.
├── jd/                         # 公共配置、AI 提取、字段标准化、日志、异常
├── platform_adapters/          # 各平台采集适配器
│   ├── jd_adapter.py
│   ├── ali_adapter.py
│   ├── ejy365_adapter.py
│   ├── cquae_adapter.py
│   ├── tpre_adapter.py
│   ├── prechina_adapter.py
│   └── gxcq_adapter.py
├── sql/mysql_schema_v2.sql      # MySQL V2 正式建表脚本
├── docs/                        # 架构、数据库、AI 配置、定时任务文档
├── tests/                       # 单元测试和回归测试
├── multi_platform_runner.py     # 多平台统一采集入口
├── jd_scraper_v2.py             # 京东采集主程序，保留单独入口
├── jd_mysql_store.py            # MySQL 存储层和 SQLite 导入工具
├── jd_viewer.py                 # 本地数据预览页面
└── .env.example                 # 本地配置模板，不包含真实密钥
```

## 环境准备

建议使用 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install requests pymysql pandas playwright
python -m playwright install chromium
```

如果只采集不需要浏览器兜底的平台，可以暂不安装 Playwright。阿里、CQUAE 等平台在登录态、风控或动态页面场景下通常需要浏览器兜底。

## 配置文件

复制模板：

```powershell
Copy-Item .env.example .env
```

`.env` 只保存在本地，不提交到 Git。

常用配置：

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=auction_data

AI_ACTIVE_PROFILE=qwen
AI_QWEN_API_KEY=your_qwen_api_key
AI_QWEN_MODEL_NAME=qwen-plus
AI_QWEN_VISION_MODEL=qwen-vl-plus
AI_QWEN_BASE_URL=https://dashscope.aliyuncs.com

AI_DEEPSEEK_API_KEY=your_deepseek_api_key
AI_DEEPSEEK_MODEL_NAME=deepseek-chat
AI_DEEPSEEK_BASE_URL=https://api.deepseek.com
```

AI 配置优先级：

```text
命令行参数 > MySQL ai_model_profiles > .env > 内置默认值
```

详见 [docs/ai_model_config.md](docs/ai_model_config.md)。

## 初始化 MySQL

创建数据库：

```sql
CREATE DATABASE IF NOT EXISTS auction_data
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
```

建表方式一：直接执行 SQL。

```powershell
mysql -h 127.0.0.1 -P 3306 -u <user> -p auction_data < sql/mysql_schema_v2.sql
```

建表方式二：让采集程序初始化表结构。

```powershell
python multi_platform_runner.py crawl `
  --platform jd `
  --limit 1 `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --ai-mode off
```

测试环境如需清空并重建表，必须同时传入两个参数：

```powershell
python multi_platform_runner.py crawl `
  --platform jd `
  --limit 1 `
  --reset-db `
  --confirm-reset-db
```

`--reset-db` 会删除并重建正式表，只能用于测试库。

## 采集命令

### 采集京东 10 条

```powershell
python multi_platform_runner.py crawl `
  --platform jd `
  --limit 10 `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --ai-profile qwen `
  --ai-mode async
```

### 采集所有平台，每个平台 10 条

```powershell
python multi_platform_runner.py crawl `
  --platform all `
  --limit 10 `
  --platform-concurrency 3 `
  --item-concurrency 3 `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --ai-profile qwen `
  --ai-mode async
```

说明：

- `--platform-concurrency`：同时跑几个平台。
- `--item-concurrency`：每个平台内同时处理几条标的。
- `--ai-mode async`：主采集先入库，缺失字段进入 AI 异步补提取队列，避免单条标的拖慢整批任务。
- `--ai-mode sync`：采集时立即 AI 提取，字段完整性更好但速度更慢。
- `--ai-mode off` 或 `--no-ai`：关闭 AI，仅保留规则/API 提取。

### 处理 AI 异步补提取队列

```powershell
python multi_platform_runner.py ai-enrich `
  --limit 50 `
  --worker-id ai-worker-1 `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --ai-profile qwen
```

### 单独使用京东采集入口

```powershell
python jd_scraper_v2.py crawl `
  --storage-backend mysql `
  --per-category-limit 2 `
  --categories 101,102,109 `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --ai-profile qwen
```

## Web Viewer

MySQL 查看器：

```powershell
python jd_viewer.py `
  --backend mysql `
  --mysql-host 127.0.0.1 `
  --mysql-port 3306 `
  --mysql-user <user> `
  --mysql-password <password> `
  --mysql-database auction_data `
  --host 127.0.0.1 `
  --port 8765 `
  --open
```

打开：

```text
http://127.0.0.1:8765/
```

Viewer 用于快速检查：

- 共有字段和特有字段是否缺失。
- 字段来源是 API、HTML、AI、规则还是校验过滤。
- 附件/图片/视频是否进入 `item_resources`。
- 债权明细、知识产权明细是否结构化入库。
- 质量报告和异常原因。

## 数据库主表说明

核心表：

- `auction_items`：所有平台、所有资产类型的共有字段主表。
- `raw_payloads`：原始 JSON、HTML、正文、附件文本归档。
- `field_extractions`：字段提取证据表，保存候选值、最终选中值、来源片段、置信度、提取方式。
- `item_resources`：附件、图片、视频统一资源表。
- `asset_*`：不同资产类型的特有字段表。
- `asset_debt_details`：债权多户明细。
- `asset_ip_details`：知识产权逐项明细。
- `ai_enrichment_queue`：AI 异步补提取队列。
- `ocr_retry_queue`：OCR/视觉兜底任务队列。
- `data_quality_reports`：批次质量报告。

完整表结构见 [docs/database_schema_v2.md](docs/database_schema_v2.md) 和 [sql/mysql_schema_v2.sql](sql/mysql_schema_v2.sql)。

## 质量报告

每次运行 `multi_platform_runner.py crawl` 或 `ai-enrich` 都会在输出目录生成模型采集数据质量报告：

```text
outputs/multi_platform/model_data_quality_report_YYYYMMDD_HHMMSS.md
```

报告会统计：

- 各平台成功/失败数量。
- 关键字段完整率。
- 缺失字段排行。
- 异常字段和可能原因。
- 后续需要人工复核的标的。

## 平台注意事项

### 京东

京东链路相对完整，优先使用接口和 HTML，再用 AI 补充字段。图片、附件、出价记录、实时价格等会尽量使用接口原始数据验真。

### 阿里

阿里页面经常依赖登录态、移动端页面或动态渲染。建议：

- 使用 `--ali-profile-path` 指定本机已登录浏览器 profile。
- 或使用 `--ali-item-url` 指定详情页 URL 做定点采集。
- 登录凭据、Cookie、账号密码不得写入代码或数据库。

### CQUAE / 山东产权公开门户

部分页面可能被 Knownsec/创宇盾等风控页拦截。处理策略：

- 先尝试普通 HTTP。
- 失败后使用浏览器兜底。
- 仍被拦截时，必须记录失败原因，不静默返回空数据。
- 生产环境建议接入授权接口或稳定可信浏览器会话。

### e交易、天津产权、北交所、广西产权

这些平台页面结构差异较大，当前策略是“平台适配器负责拿原文，AI 负责主提取，规则/API 负责验真”。如果字段缺失，优先检查原文是否完整进入 `raw_payloads`，再检查 AI 上下文截断和字段证据。

## 测试

运行全部单元测试：

```powershell
python -m unittest discover -s tests
```

测试覆盖内容包括：

- 京东字段提取回归。
- MySQL V2 存储层。
- 多平台 runner。
- 各平台 adapter 基础解析。
- AI 配置解析。
- Viewer 基础展示。

## 安全要求

不要提交以下内容：

- `.env`
- 真实 API Key
- MySQL 用户名/密码
- 浏览器 profile
- Cookie、Token、登录态
- 采集输出目录 `outputs/`
- SQLite/MySQL dump 数据库文件
- 日志文件和缓存文件

仓库只保留 `.env.example`，真实配置必须放在本地环境变量、`.env` 或安全的配置中心。

## 相关文档

- [数据库表结构 V2](docs/database_schema_v2.md)
- [多平台 AI 提取方案](docs/multi_platform_ai_extraction_plan.md)
- [AI 模型配置说明](docs/ai_model_config.md)
- [定时任务平台架构](docs/scheduled_task_architecture.md)
- [实施变更记录](IMPLEMENTATION_CHANGELOG.md)

