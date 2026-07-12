# 多平台拍卖/资产项目信息采集系统

这是一个面向拍卖、产权交易和资产处置信息的多平台采集系统。当前主线是 `MySQL + 定时任务 Web 后台 + AI 异步补提取`，旧 SQLite 查看器仅保留为兼容工具，不再作为主要使用入口。

## 主要能力

- 多平台采集：京东拍卖、阿里拍卖、e 交易、重庆产权/CQUAE、山东产权、天津产权、北京产权、贵州阳光产权、广西产权等。
- MySQL V2 结构化入库：公共字段、资产特有字段、原始 payload、附件资源、AI 队列、质量报告、采集断点统一存储。
- 全量/增量/样本采集：支持平台参数、分页参数、并发参数和浏览器兜底参数。
- 断点续传：通用 runner 和京东 `crawl_with_db` 路径已支持采集中持续写 checkpoint；其他 adapter 正在逐步补细粒度断点。
- AI 异步补提取：采集先入库，AI 队列后续异步处理，可按模型 profile 和任务类型调度。
- 附件与图片预览：资源统一进入 `item_resources`，Web 后台可预览图片、PDF、视频和常见办公文档。
- Web 管理后台：任务管理、批次管理、标的查看、AI 队列、采集队列、平台统计、质量报告、模型配置和模型选择。
- 可选认证：本机使用默认关闭；局域网/公网部署前可通过 `.env` 或命令行开启管理员登录。

## 目录结构

```text
.
|-- jd/                         # AI 配置、字段标准化、提取器、工具模块
|-- platform_adapters/          # 各平台采集适配器
|-- web_admin/                  # FastAPI 后台与单页管理前端
|-- sql/mysql_schema_v2.sql     # MySQL V2 建表脚本
|-- docs/                       # 架构、AI、定时任务、数据库文档
|-- tests/                      # 单元测试和回归测试
|-- multi_platform_runner.py    # 多平台统一采集/AI 队列入口
|-- jd_scraper_v2.py            # 京东采集入口
|-- jd_mysql_store.py           # MySQL 存储层、schema 初始化和迁移辅助
|-- jd_viewer.py                # 旧本地查看器，兼容保留
|-- handoff.md                  # 项目交接和最新开发记录
`-- .env.example                # 本地配置模板，不包含真实密钥
```

## 环境准备

建议使用 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install requests pymysql pandas fastapi uvicorn apscheduler croniter python-dotenv httpx playwright
python -m playwright install chromium
```

如果只跑纯 API 平台采集，可以暂不安装 Playwright；阿里、CQUAE 等平台在登录态、风控或动态页面场景下通常需要浏览器兜底。

## 配置文件

复制模板：

```powershell
Copy-Item .env.example .env
```

`.env` 只保存在本地，不提交 Git。常用配置：

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=auction_data

AI_ACTIVE_PROFILE=qwen
AI_QWEN_API_KEY=
AI_QWEN_MODEL_NAME=qwen-plus
AI_QWEN_VISION_MODEL=qwen-vl-plus
AI_QWEN_BASE_URL=https://dashscope.aliyuncs.com

WEB_ADMIN_AUTH_ENABLED=false
WEB_ADMIN_ADMIN_USERNAME=admin
WEB_ADMIN_ADMIN_PASSWORD=
WEB_ADMIN_CORS_ALLOW_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
```

认证配置也可以以后迁移到 MySQL 用户表，但当前推荐先放 `.env`：认证开关和首个管理员密码属于部署配置，数据库不可用时也应有明确行为。

## MySQL 初始化

创建数据库：

```sql
CREATE DATABASE IF NOT EXISTS auction_data
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
```

方式一：直接执行 SQL：

```powershell
mysql -h 127.0.0.1 -P 3306 -u <user> -p auction_data < sql/mysql_schema_v2.sql
```

方式二：让程序初始化 schema：

```powershell
python jd_mysql_store.py --host 127.0.0.1 --port 3306 --user <user> --password <password> --database auction_data
```

破坏性重置需要双确认和环境变量：

```powershell
$env:ALLOW_DB_RESET="1"
python multi_platform_runner.py crawl --reset-db --confirm-reset-db --platform jd --mode sample --limit 1
```

不要把 `ALLOW_DB_RESET=1` 长期写入生产环境。

## 启动 Web 后台

本机使用：

```powershell
python -m web_admin.main --host 127.0.0.1 --port 8000 --open
```

启用认证：

```powershell
$env:WEB_ADMIN_AUTH_ENABLED="true"
$env:WEB_ADMIN_ADMIN_USERNAME="admin"
$env:WEB_ADMIN_ADMIN_PASSWORD="<set-a-strong-password>"
python -m web_admin.main --host 127.0.0.1 --port 8000 --open
```

局域网/公网部署前至少要开启认证，并设置 CORS 白名单：

```powershell
$env:WEB_ADMIN_AUTH_ENABLED="true"
$env:WEB_ADMIN_ADMIN_PASSWORD="<set-a-strong-password>"
$env:WEB_ADMIN_CORS_ALLOW_ORIGINS="https://your-admin-domain.example"
python -m web_admin.main --host 0.0.0.0 --port 8000
```

公网长期运行建议放在 HTTPS 反向代理后面，并进一步引入持久化用户表、角色权限和审计日志。

## 常用采集命令

京东样本采集：

```powershell
python multi_platform_runner.py crawl --platform jd --mode sample --limit 10 --ai-mode async
```

京东全量采集：

```powershell
python multi_platform_runner.py crawl --platform jd --mode full --limit 0 --ai-mode async --item-concurrency 1
```

CQUAE 当前推荐全量命令：

```powershell
python multi_platform_runner.py crawl --platform cquae --mode full --limit 0 --ai-mode async --item-concurrency 1 --request-timeout 0 --browser-timeout-ms 0 --cquae-page-size 60 --cquae-max-pages 0 --cquae-browser-settle-ms 800 --cquae-profile-path "F:\codex_project\jd\.browser\cquae"
```

阿里如需使用本地 Chrome token：

```powershell
python _extract_ali_tk_token.py "C:\Users\<you>\AppData\Local\Google\Chrome\User Data\Profile 4"
python multi_platform_runner.py crawl --platform ali --mode sample --limit 3 --ali-tk-token <token>
```

只有显式加 `--save-env` 时，token 才会写入 `.env`。

## AI 队列和模型配置

处理队列：

```powershell
python multi_platform_runner.py ai-enrich --limit 20 --concurrency 3 --worker-id worker-1
```

指定模型 profile：

```powershell
python multi_platform_runner.py ai-enrich --limit 20 --concurrency 2 --ai-profile qwen
```

模型配置优先级：

```text
CLI 参数 > MySQL ai_model_profiles > .env > 内置默认值
```

Web 后台的“模型配置”和“模型选择”会写入 MySQL。生产环境建议优先使用 `api_key_env_var`，少用 `api_key_value` 明文入库。

## 断点续传

采集断点保存在 `crawl_checkpoints`：

- 记录平台、分类 key、模式、当前页、最后标的、已处理数量、状态和最后更新时间。
- 通用 runner 路径已在详情处理过程中持续写 checkpoint。
- 京东 `crawl_with_db` 路径已补细粒度 checkpoint 和继续采集入口。
- CQUAE/GXCQ/CBEX 等 adapter 仍需继续细化列表分页阶段 checkpoint。

Web 后台“采集断点”表可查看当前 checkpoint，并支持继续采集。

## Web 后台功能

- 运营总览：平台统计、趋势、任务概览。
- 任务管理：配置定时采集任务。
- 批次管理：查看批次状态、停止、重试。
- 标的查看：检索标的详情，查看出价记录、特有字段、资源附件和原始数据。
- 队列管理：AI 队列处理、暂停、恢复、失败重试、处理选中项。
- 采集队列：采集批次和 checkpoint。
- 平台管理：平台数据量、最近采集、成功率。
- 质量报告：字段覆盖、缺失和异常样本。
- 模型管理：模型配置和当前模型选择。

## 安全注意事项

- 不要提交 `.env`、API Key、MySQL 密码、浏览器 token、真实平台 secret。
- 后台默认只适合本机访问；开放局域网/公网前必须启用认证。
- `ALLOW_DB_RESET=1` 只在明确需要重置库时临时设置。
- GXCQ 如需 `appsecret`，使用 `GXCQ_LIST_APP_SECRET` 环境变量，不要写入代码。
- 资源代理已限制本地/私网 URL，但公网部署仍建议加反向代理和访问频率限制。
- 当前认证是单管理员、内存 session；公网长期运行时应升级为持久用户表和权限体系。

## 测试和静态检查

```powershell
python -m pytest tests -q
python -m compileall -q multi_platform_runner.py jd_scraper_v2.py jd_mysql_store.py web_admin platform_adapters _extract_ali_tk_token.py
```

前端脚本语法检查：

```powershell
$html = Get-Content -Raw -Encoding UTF8 web_admin\static\index.html
$m = [regex]::Match($html, '(?s)<script>(.*)</script>')
Set-Content -Path _tmp_frontend_script.js -Value $m.Groups[1].Value -Encoding UTF8
node --check _tmp_frontend_script.js
Remove-Item _tmp_frontend_script.js
```

安全回归测试：

```powershell
python -m pytest tests/test_security_hardening.py tests/test_web_admin_auth.py -q
```

## Git 工作流建议

当前项目同步到主仓库的子目录：

```text
F:\codex_project\credit-claim-extraction\projects\jd-auction-multiplatform
```

后续建议：

- 主分支只放已验证版本。
- 小修复直接做清晰提交，例如 `fix: protect admin auth and db reset`。
- 大功能使用分支，例如 `feature/jd-resume-cquae-checkpoint`。
- 每次提交前至少运行 `python -m pytest tests -q`。
- 不提交 `.env`、`outputs/`、日志、缓存、浏览器 profile、数据库导出。

## 当前优先级

1. 继续补齐 CQUAE/GXCQ/CBEX 等 adapter 的分页级断点续传。
2. 把“处理选中 AI 项目”从高优先级队列改为严格 task id 过滤。
3. 为局域网/公网部署补持久用户表、权限分级、审计日志和 HTTPS 部署说明。
4. 优化模型配置，减少 `api_key_value` 明文入库使用。
