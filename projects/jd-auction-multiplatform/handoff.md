# 项目交接文档

更新时间：2026-07-10 18:55

## 1. 我们在做什么

项目目录：`F:\codex_project\jd`

目标是做一个多平台拍卖/资产交易数据采集系统：

- 支持京东拍卖、淘宝/阿里拍卖、e交易、山东产权公开门户、CQUAE、天津产权等平台。
- 数据统一写入 MySQL `auction_data`，后续不再以 SQLite 查看器为主。
- 通过 `platform_adapters` 统一各平台列表页、详情页、附件、图片、出价记录、特有字段等采集入口。
- 采集流程采用“主采集先入库，AI/附件/OCR 等异步补全”的模式，避免定时任务被单条数据拖死。
- 定时任务平台是后续主前端，需要支持任务管理、队列管理、模型配置、采集队列、质量报告、断点续采、全量/增量采集等。

当前正在处理的重点是：AI 队列多模型调度和队列状态可视化。

## 2. 已经完成的事情

### 2.1 数据库与架构方向

- 已明确后续统一使用 V2 MySQL 表，不再保留两套 V1/V2 表长期并行。
- 旧 SQLite 查看器路径、旧 MySQL 导入逻辑、历史迁移能力不再作为正式入口。
- 正式入口应统一到：
  - `multi_platform_runner.py`
  - `jd_mysql_store.py`
  - `platform_adapters/*`
  - `web_admin/*`

### 2.2 平台适配器方向

- 已要求 CQUAE 和 SDCQJY 分离为两个独立 adapter，不再把 SDCQJY 当作 CQUAE 的备选入口。
- 已要求京东 adapter 也放进 `platform_adapters`，所有平台统一入口。
- 淘宝/阿里拍卖数据量估算逻辑发现过问题：早先只看到部分页，实际房产等类别远超 5001 条，需要修复分页与分类采集逻辑后再估算全量。

### 2.3 AI 队列状态与暂停设计

用户确认采用以下状态模型：

- `pending`：候选/等待进入可领取队列。
- `running`：已经放入可领取队列，但还没有被具体 AI worker 真正处理。
- `parsing`：正在被某个模型实际解析。
- `paused`：已暂停，不会被 AI worker 领取。
- `success`：解析成功。
- `failed`：解析失败。

一键暂停只暂停 `pending` / `running` 中尚未实际解析的任务，不应强行中断 `parsing` 任务。

### 2.4 AI 多模型调度

关键背景：

- MySQL 里已有 `ai_model_profiles` 表，模型配置不只在 `.env` 中。
- 用户希望模型可以从定时任务平台配置和选择。
- Vision 模型也可以多开，但需要按独立 profile、并发上限、适用任务类型来控流。

已确认的问题：

- 用户配置了多个模型，但队列中只看到一个模型被使用。
- 直接原因是前端队列页面在模型选择为空时自动选择默认模型，导致“空=自动分配”的语义被破坏。
- 后端在 `ai_profile=""` 时可以按任务类型做自动分配，但前端之前没有真正传空值。

本次已修复：

- `web_admin/static/index.html`
  - 去掉 `loadProfiles()` 中自动选中默认模型的逻辑。
  - `loadAuto()` 不再把空 `ai_profile` 回填成已选模型。
  - 模型下拉框增加“自动分配（按适用任务）”选项。
  - 占位文案改为“模型配置（空=自动分配）”。
  - 手动处理队列后的提示改为显示 worker 数量。
- `tests/test_web_admin_task_runtime.py`
  - 增加回归测试 `test_queue_frontend_keeps_blank_profile_for_auto_distribution`。
- `tests/test_multi_platform_runner.py`
  - fake DB 的 `fetch_ai_enrichment_tasks()` 签名补充 `task_types=None`，适配当前实现。

验证结果：

- `python -m pytest tests\test_web_admin_task_runtime.py -q`
  - 结果：`13 passed in 2.15s`
- `python -m pytest tests\test_multi_platform_runner.py -q`
  - 结果：`27 passed in 1.98s`
- `python -m py_compile web_admin\routers\queues.py web_admin\services\task_trigger.py web_admin\services\ai_queue_auto.py multi_platform_runner.py jd_mysql_store.py`
  - 结果：退出码 0，无语法错误输出
- `python multi_platform_runner.py ai-enrich --help`
  - 结果：能看到 `--task-types TASK_TYPES` 参数
- `rg -n "defaultProfile|使用默认模型|自动分配|cfg\.ai_profile|workerCount|selectedProfile" web_admin\static\index.html`
  - 结果：未再出现 `defaultProfile` 或“使用默认模型”，保留“自动分配”和 worker 数量提示。

注意：

- 已经处于 `parsing` 的旧任务是在修复前被默认模型领取的，不会自动切换模型。
- 要验证多模型是否生效，需要重新触发新的任务，或把旧的非活跃 `parsing/running` 任务安全重置后再跑。

## 3. 当前卡在哪里

### 3.1 需要做一次真实页面验证

代码层面的静态与单元测试已经通过，但还需要在定时任务平台页面上手动验证：

1. 打开队列管理。
2. 模型选择保持空值，即“自动分配（按适用任务）”。
3. 点击处理 20 条。
4. 确认后端返回多个 `task_id` 或 worker 数。
5. 查看队列中 `处理模型` 是否按任务类型分配到多个 profile。

如果页面仍然只看到一个模型，需要优先排查：

- 前端是否加载了旧缓存。
- 自动处理配置里是否保存了固定 `ai_profile`。
- `ai_model_profiles.task_types` 是否配置过窄。
- 队列任务 `task_category` 是否都被归成同一类。
- 老的 `parsing` 任务是否是修复前已经被领取的。

### 3.2 队列中的旧任务可能干扰判断

如果表中已有大量旧 `parsing` / `running` 任务，页面看起来可能仍然像“只有一个模型在跑”。不要直接判断修复失败。

建议先查：

```sql
SELECT queue_status, ai_profile, provider, model_name, COUNT(*)
FROM ai_enrichment_queue
GROUP BY queue_status, ai_profile, provider, model_name;
```

如果字段名不存在，先执行：

```sql
SHOW COLUMNS FROM ai_enrichment_queue;
```

然后按真实字段名调整查询。

### 3.3 Git 状态

在 `F:\codex_project\jd` 执行 `git status --short` 返回：

```text
fatal: not a git repository (or any of the parent directories): .git
```

说明当前目录不是 git 仓库，不能直接在这里提交或查看 git diff。之前用户要求推送 GitHub，后续如果要提交，需要确认实际 git 仓库目录或远端工作副本位置。

## 4. 下一步计划

### P0：验证并修复 AI 队列多模型实际运行

1. 重启定时任务平台后端，强制浏览器刷新前端。
2. 在队列页面保持模型选择为空。
3. 点击处理 20 条。
4. 检查后台任务和队列表：
   - 是否生成多个 worker。
   - 是否按 `task_category` 分配不同 profile。
   - `parsing` 行是否显示具体模型。
5. 如果全部仍然落到一个模型：
   - 检查 `select_ai_profiles_for_task_types()` 或等价函数。
   - 检查 `ai_model_profiles.task_types` 解析逻辑。
   - 检查 `process_ai_enrichment_queue()` 是否把空 profile 又改成默认 profile。

### P1：完善队列页面

- 显示每条任务的：
  - `queue_status`
  - `task_category`
  - `ai_profile`
  - `provider`
  - `model_name`
  - `worker_id`
  - `claim_token`
  - `started_at`
  - `duration_ms`
  - `error_message`
- 增加按钮：
  - 暂停待处理/运行中但未解析任务。
  - 恢复暂停任务。
  - 重试失败任务。
  - 解锁超时卡住任务。
- 默认关闭自动处理，只有用户显式开启后才运行 AI/附件/OCR 队列。

### P2：继续数据质量修复

已知还需要继续处理：

- 京东：公告/须知/详情 tab 的获取和截断策略，特别是多标的一公告中只提取当前标的。
- 淘宝/阿里：分类列表分页、每类型样本采集、AI 主提取缺失字段补齐。
- e交易：附件 URL 为空、出价记录 JSON 不正确、债权明细缺失。
- 山东产权 SDCQJY：附件材料、明细、补充披露信息还存在缺失。
- CQUAE：平台风控和详情页字段抓取稳定性。
- 天津产权：字段缺失多，需要单独 adapter 和页面结构解析。
- 知识产权：明细表优先 HTML 表格提取，OCR 作为兜底异步任务。

### P3：质量报告制度化

用户要求：每次让系统跑数据，都要生成模型采集数据质量报告。

报告至少包含：

- 平台、批次、采集时间。
- 样本数量。
- 字段覆盖率。
- 缺失字段排行。
- 异常字段排行。
- 每个模型 profile 的：
  - 处理数量
  - 成功率
  - 平均耗时
  - 失败原因 TopN
- 随机抽样人工核验结果。
- 发现的问题和下一步修复建议。

## 5. 绝对不要再踩的坑

1. 不要把“配置了多个模型”等同于“系统会自动使用多个模型”。必须确认前端传空 profile、后端按任务类型分配、队列表显示具体模型。
2. 不要把空模型选择自动替换为默认模型。空值的语义已经确定为“自动分配”。
3. 不要用旧的 `parsing` 行判断新逻辑是否生效。旧任务可能是在修复前领取的。
4. 不要默认打开平台就自动处理 AI 队列。用户明确要求默认手动处理，开启自动处理后才运行。
5. 不要继续依赖 SQLite 查看器作为主流程。后续主前端是定时任务平台，数据源是 MySQL。
6. 不要混用 CQUAE 和 SDCQJY adapter。用户明确要求分开。
7. 不要为了速度牺牲字段准确性。当前阶段先保证完整和准确，后续再优化耗时。
8. 不要把页面中不属于当前标的的表格行提取到当前标的字段里。多标的一公告必须根据标题、标的编号、地址、车位号等锚定当前标的。
9. 不要把附件中的站点装饰图片、广告图、二维码当作现场图片。只保留与本次标的直接相关的图片。
10. 不要把 API Key、MySQL 密码等敏感配置写进 GitHub。
11. 不要手工重复跑同类验证。若反复做“清空数据→跑样本→质量报告”，应做成脚本或平台按钮。

## 6. 常用命令

启动定时任务平台前需要确认 `.env` 和 MySQL 配置。

常见查看器/后台命令以实际代码参数为准，先看帮助：

```powershell
python jd_viewer.py --help
python web_admin_app.py --help
python multi_platform_runner.py --help
python multi_platform_runner.py ai-enrich --help
```

本次已用验证命令：

```powershell
python -m pytest tests\test_web_admin_task_runtime.py -q
python -m pytest tests\test_multi_platform_runner.py -q
python -m py_compile web_admin\routers\queues.py web_admin\services\task_trigger.py web_admin\services\ai_queue_auto.py multi_platform_runner.py jd_mysql_store.py
python multi_platform_runner.py ai-enrich --help
```

## 7. 给下一位 Codex 的建议

先不要急着继续大规模采集。下一步最有价值的是把“AI 队列多模型自动分配”在页面和数据库里验证闭环：

1. 新触发一批任务。
2. 确认任务类型分布。
3. 确认多个模型 profile 被实际领取。
4. 确认队列页面能显示“正在解析的是哪个模型”。
5. 生成一份小样本质量报告。

这个闭环跑通后，再继续处理淘宝、e交易、山东产权等平台的数据质量问题。

## 8. 2026-07-10 20:20 补充：队列按钮语义修复

本轮处理了队列页面按钮误导问题。

用户指出：

- 按钮不能再叫“处理 20 条”。
- 点击后页面只看到 3 条进入解析中，而且 3 条使用同一个模型。
- 重复点击后解析数量会增加，模型也可能变化。

结论：

- 这是当前架构下的正常行为，但原按钮文案错误。
- 该按钮不是同步处理固定 20 条数据，而是触发后台 AI worker。
- “20”只是本次最多领取的候选任务上限。
- 实际同时显示几条 `parsing` 由右侧并发数决定，例如并发为 3 时通常只会看到 3 条正在解析。
- 同一批任务使用同一个模型，通常是因为任务类型匹配同一个 profile，或当前 profile/策略只命中一个可用模型。
- 重复点击会启动新的后台 worker，所以解析数量会增加，也可能因自动路由命中其它模型。

已修改：

- `web_admin/static/index.html`
  - 按钮文案从“处理 20 条”改为“启动 AI 解析”。
  - 增加 tooltip，说明它会按模型策略触发后台解析、最多领取 20 个候选任务、实际并发由右侧并发控制、重复点击会启动新的后台 worker。
- `tests/test_web_admin_task_runtime.py`
  - 增加回归测试，防止按钮文案退回“处理 20 条”。

已验证：

```powershell
python -m pytest tests\test_web_admin_task_runtime.py -q
```

结果：

```text
14 passed in 1.73s
```

仍建议下一步补一个“活跃 worker 锁”或“同一页面只允许一个解析批次运行”的保护，否则重复点击虽然符合当前实现，但容易造成用户误以为系统重复处理、成本失控或任务数量异常增长。

## 9. 2026-07-10 20:35 补充：本轮代码已提交并推送

本轮把当前项目代码同步到了 GitHub 仓库：

- 本地工作目录：`F:\codex_project\jd`
- Git 仓库目录：`F:\codex_project\credit-claim-extraction`
- 仓库内项目目录：`projects/jd-auction-multiplatform`
- 提交：`5f7798a Sync auction collection platform updates`
- 推送方式：HTTPS 推送连续失败，报 `HTTP 408` / `Connection was reset`；随后使用 SSH URL `git@github.com:ling1-1/credit-claim-extraction.git` 推送成功。

提交前检查：

```powershell
python -m pytest tests\test_web_admin_task_runtime.py -q
python -m py_compile web_admin\routers\queues.py web_admin\services\task_trigger.py web_admin\services\ai_queue_auto.py
```

结果：

```text
14 passed
py_compile exit 0
```

推送前已确认未把 `.env`、数据库文件、日志、输出目录、图片、Excel、pyc 等文件加入暂存区。源码敏感词扫描只命中了 `.env.example`、文档占位符、测试假 key 和函数参数名，没有发现真实 API Key 或 MySQL 密码。
## 10. 2026-07-10 20:45 补充：远端同步状态已核验

本轮继续完成了 GitHub 远端状态核验：

- 使用 SSH 显式 fetch：
  `git -C F:\codex_project\credit-claim-extraction fetch git@github.com:ling1-1/credit-claim-extraction.git main:refs/remotes/origin/main`
- 本地 `HEAD` 与 `origin/main` 均为：`bca747a`
- `git status --short --branch` 只显示 `## main...origin/main`，没有额外未提交文件。

因此本轮代码与 handoff 更新已经同步到 GitHub。

需要向用户说明的行为结论：

- 队列按钮原先叫“处理 20 条”是不准确的，已经改为“启动 AI 解析”。
- 该按钮触发后台 AI worker，不是同步处理固定 20 条。
- “20”只是最多领取候选任务数量；实际同时解析数量由并发控制决定。
- 如果并发为 3，页面只看到 3 条 `parsing` 是正常的。
- 同一批任务使用同一个模型通常是因为任务类型路由命中了同一个 profile。
- 重复点击会启动新的后台 worker，所以解析数量会增加，后续也可能由不同模型处理。

仍建议后续增加“活跃 worker 防重复触发”或“同一页面同一批次只允许一个运行中 worker”的保护，避免误点击导致成本和任务数量失控。

## 11. 2026-07-11 补充：AI 队列自动处理行为说明

用户追问“如果开启自动处理会怎样”。当前代码逻辑如下：

- `web_admin/main.py` 启动时会启动 `web_admin/services/ai_queue_auto.py` 的后台守护线程。
- 默认配置在 `web_admin/config.py` 中是手动模式：`ai_queue_auto_enabled = False`。
- 开启自动处理后，守护线程按间隔检查 `ai_enrichment_queue`。
- 只有同时满足以下条件才会触发 AI worker：
  - 自动处理已启用；
  - 队列里存在 `pending` 任务；
  - 当前没有 `ai_enrich` 后台任务正在运行。
- 如果自动处理配置里指定了 `ai_profile`，只会使用该模型 profile。
- 如果 `ai_profile` 为空，系统会读取 MySQL `ai_model_profiles` 中所有启用模型，按 `priority` 排序，并按照各模型的 `task_types`、`max_concurrency` 分配 worker。
- 队列状态含义建议继续按当前设计解释：
  - `pending`：等待被 AI worker 领取；
  - `running`：已被 worker 领取/分派，但未必正在调用模型；
  - `parsing`：正在调用模型；
  - `paused`：暂停，不会被自动处理领取；
  - `success` / `failed`：完成或失败。

需要注意的风险：

- 当前自动处理通过 `get_running_tasks()` 判断是否已有 `ai_enrich` 后台任务，这对单机本地管理平台基本可用，但如果未来多实例部署或进程异常退出，仍建议增加数据库级 worker/run 锁。
- 如果页面只看到一个模型在解析，常见原因是：自动配置指定了单一 profile、任务类型只匹配一个 profile、其他模型未启用或 API Key/并发配置不可用、任务已在旧逻辑下被提前领取。
## 12. 2026-07-11 补充：CQUAE/重庆产权全量采集速度优化

本轮处理的问题：重庆产权全量采集速度慢、限制多。排查结论是瓶颈主要在 CQUAE 列表/详情获取链路，尤其是浏览器 fallback 和站点风控/页面加载，不是字段提取本身。

已修改文件：

- `platform_adapters/cquae_adapter.py`
  - `CquaeBrowserFetcher` 增加 `settle_ms` 参数，默认 800ms。
  - 浏览器访问从固定 `sleep(3)` 改为 `domcontentloaded + body selector + 可配置 settle_ms`。
  - 保留 `close()`，用于采集结束后释放浏览器资源。
- `multi_platform_runner.py`
  - `CquaeLiveHandler` 增加 `page_size`、`max_pages`、`browser_timeout_ms`、`browser_settle_ms` 参数。
  - CQUAE 列表默认 page size 改为可配置，减少全量采集时的列表页请求数。
  - CQUAE 浏览器 fallback 现在会继承 `--browser-timeout-ms`、`--cquae-headed`、`--cquae-profile-path`、`--cquae-browser-settle-ms`。
  - 浏览器点击翻页 fallback 从固定等待改为可配置等待，`networkidle` 改为 `domcontentloaded`，减少无谓等待。
  - `MultiPlatformRunner.crawl_platform()` 在 finally 中调用 handler `close()`，避免浏览器资源泄漏。
  - 新增 CLI 参数：
    - `--cquae-page-size`
    - `--cquae-max-pages`
    - `--cquae-browser-settle-ms`

已验证：

```powershell
python -m py_compile platform_adapters\cquae_adapter.py multi_platform_runner.py
python multi_platform_runner.py crawl --help | Select-String -Pattern "cquae-page-size|cquae-max-pages|cquae-browser-settle-ms|cquae-profile-path|browser-timeout-ms"
python multi_platform_runner.py crawl --platform cquae --mode sample --limit 1 --ai-mode off --item-concurrency 1 --request-timeout 20 --browser-timeout-ms 20000 --cquae-page-size 60 --cquae-browser-settle-ms 800
```

验证结果：

- `py_compile` 通过。
- CLI 参数已出现。
- CQUAE sample 采集 1 条成功：`success_count=1`，`failed_count=0`。
- 小样本耗时约 62 秒，说明链路可用，但站点/浏览器层仍是主要耗时点。

建议后续全量采集命令：

```powershell
python multi_platform_runner.py crawl --platform cquae --mode full --limit 0 --ai-mode async --item-concurrency 1 --request-timeout 0 --browser-timeout-ms 0 --cquae-page-size 60 --cquae-max-pages 0 --cquae-browser-settle-ms 800 --cquae-profile-path "F:\codex_project\jd\.browser\cquae"
```

如果被 Knownsec/创宇盾或登录态限制卡住，先用有界面浏览器建立可复用 profile：

```powershell
python multi_platform_runner.py crawl --platform cquae --mode sample --limit 3 --ai-mode off --cquae-headed --cquae-profile-path "F:\codex_project\jd\.browser\cquae" --cquae-page-size 60
```

仍需注意：

- 这次优化减少了固定等待和无谓翻页成本，但不能彻底消除 CQUAE 风控限制。
- 全量采集不要盲目提高并发。CQUAE 更适合低并发、断点续采、增量优先。
- 后续如果仍慢，优先做：列表 API/接口参数逆向、checkpoint 细化到 CQUAE 分类页、失败页重试队列、以及定时任务平台中 CQUAE 专属采集策略。

## 13. 2026-07-11 Supplement: crawl checkpoint and resume fix

Goal:
- Fix the empty "crawl checkpoint" panel in the admin UI.
- Persist crawl progress while a crawl is running, so interrupted generic crawls can resume from the last processed item.
- Make checkpoint API errors visible instead of silently showing an empty table.

Completed:
- `web_admin/routers/queues.py`
  - Added the missing `_table_exists()` helper used by `/api/queues/crawl/checkpoints`.
  - Extended the checkpoint API response with `batch_id`, `crawl_mode`, `checkpoint_status`, `message`, `completed_at`, and related progress fields.
- `web_admin/static/index.html`
  - `loadCheckpoints()` now stores and displays backend errors via `checkpointError`.
  - The checkpoint table now shows status, mode, batch id, message, and completed time.
- `sql/mysql_schema_v2.sql`
  - Added missing `crawl_checkpoints` columns: `batch_id`, `crawl_mode`, `checkpoint_status`, `message`, `completed_at`.
  - Added supporting indexes for status and update time.
- `multi_platform_runner.py`
  - Added checkpoint helpers and runner methods for loading/saving crawl checkpoints.
  - Generic `fetch_list -> detail -> write` crawl now writes a running checkpoint after each successfully processed item.
  - `full` and `incremental` modes can skip already processed list items when a running checkpoint exists.
  - `crawl_with_db` adapters now at least write coarse start/end/failed checkpoints.
  - Repaired syntax-breaking mojibake damage in quality report generation.

Verified:
```powershell
python -m py_compile .\multi_platform_runner.py .\jd_mysql_store.py .\web_admin\routers\queues.py
python -m pytest tests/test_multi_platform_runner.py -q
python -m py_compile .\web_admin\main.py .\web_admin\routers\queues.py
```

Results:
- `py_compile` passed.
- `tests/test_multi_platform_runner.py`: 34 passed.
- Web admin modules compile.

Current limitations:
- True fine-grained resume is only implemented for the generic runner path.
- Adapters that implement `crawl_with_db` bypass the generic loop, so they currently only get coarse start/end/failed checkpoints. For those adapters, real disconnect-safe resume still needs adapter-level checkpoint callbacks inside their own page/item loops.
- Existing MySQL databases must be migrated or recreated to include the new `crawl_checkpoints` columns before the UI can show the new fields.

Do not repeat these mistakes:
- Do not hide checkpoint API errors in the frontend; an empty panel can mean the endpoint failed.
- Do not write checkpoints only in a `finally` block; hard stops and network loss need progress persisted during the loop.
- Do not assume every platform goes through `MultiPlatformRunner`'s generic item loop; `crawl_with_db` adapters bypass it.
- Do not run broad encoding rewrites on mixed Chinese/mojibake source files. A previous broad rewrite corrupted string literals and caused syntax errors.

## 14. 2026-07-12 补充：京东断点续采、继续采集按钮、选中 AI 任务处理

本轮处理用户反馈：

- 手动停止京东全量采集后，采集断点“已处理”为 0。
- 用户询问继续采集是否会从断点后续内容开始。
- 用户希望队列管理支持“处理选中项目”，便于小范围测试。

结论：

- “采集断点”记录的是主采集进度，不是 AI 解析进度；已处理为 0 与是否进行 AI 解析无关。
- 京东此前走 `crawl_with_db` 老路径，只写粗粒度 start/end/failed checkpoint，手动停止时通常只留下 started 且 total_items_seen=0。
- 仅有“重试”不等于“继续采集”；继续采集应该显式读取 `crawl_checkpoints` 并按平台/模式重新触发采集。

本轮已修改：

- `multi_platform_runner.py`
  - `crawl_with_db` 路径现在会向 adapter 传入 `checkpoint_callback` 和 `resume_checkpoint`。
  - adapter 内部调用 callback 时，会写入 running checkpoint。
- `platform_adapters/jd_adapter.py`
  - `crawl_sample()` 增加 `checkpoint_callback` 与 `resume_checkpoint` 参数，并传给 `JDAuctionScraper`。
- `jd_scraper_v2.py`
  - `crawl_sample()` 增加 `checkpoint_callback` 与 `resume_checkpoint` 参数。
  - 京东采集逐条成功后写 checkpoint，更新 `total_items_seen`、`last_item_id`、`current_page`。
  - 京东 full/incremental 模式收到 running checkpoint 时，会跳过直到 `last_item_id` 之后再继续采集。
  - 京东 checkpoint 统一写入 `category_key=default`，保证前端京东断点行能显示真实进度，而不是长期 0。
- `web_admin/routers/queues.py`
  - 新增 `POST /api/queues/crawl/checkpoints/resume`，用于从 checkpoint 触发继续采集。
  - 新增 `POST /api/queues/ai/process-selected`，用于将选中 AI 任务置为 pending、提高优先级并启动 worker。
- `web_admin/static/index.html`
  - 队列管理增加“处理选中项”按钮。
  - 采集断点表每行增加“继续采集”按钮。
- `tests/test_multi_platform_runner.py`
  - 增加京东 `crawl_with_db` 细粒度 checkpoint 回归测试。
  - 增加京东 `resume_checkpoint` 传递回归测试。
- `tests/test_web_admin_task_runtime.py`
  - 增加继续采集 checkpoint 触发回归测试。
  - 增加处理选中 AI 任务回归测试。

已验证：

```powershell
python -m pytest tests/test_multi_platform_runner.py::MultiPlatformRunnerTests::test_jd_crawl_with_db_adapter_can_write_fine_grained_checkpoint -q
python -m pytest tests/test_web_admin_task_runtime.py::WebAdminTaskRuntimeTests::test_resume_crawl_checkpoint_triggers_platform_from_checkpoint tests/test_web_admin_task_runtime.py::WebAdminTaskRuntimeTests::test_process_selected_ai_tasks_resets_selected_rows_and_triggers_worker -q
python -m pytest tests/test_multi_platform_runner.py::MultiPlatformRunnerTests::test_jd_crawl_with_db_receives_running_checkpoint_for_resume tests/test_multi_platform_runner.py::MultiPlatformRunnerTests::test_jd_crawl_with_db_adapter_can_write_fine_grained_checkpoint -q
python -m pytest tests/test_web_admin_frontend.py -q
python -m pytest tests/test_multi_platform_runner.py tests/test_web_admin_task_runtime.py -q
python -m py_compile multi_platform_runner.py jd_scraper_v2.py platform_adapters\jd_adapter.py web_admin\routers\queues.py web_admin\main.py
```

验证结果：

- 新增断点/继续采集/选中处理测试均通过。
- `tests/test_web_admin_frontend.py`: 10 passed。
- `tests/test_multi_platform_runner.py tests/test_web_admin_task_runtime.py`: 52 passed。
- `py_compile` 通过。

当前仍需注意：

- 京东断点续采现在能读取 `last_item_id` 并跳过到其后继续；但如果下一次列表中找不到这个 `last_item_id`，当前实现可能无法精确定位，需要后续增加更稳的分类/页号级恢复策略。
- “处理选中项”当前实现是把选中 AI 任务设为最高优先级并启动 worker；worker 仍按队列领取机制工作。通常会优先处理选中项，但严格“只处理这些 ID”需要后续给 `ai-enrich` CLI 和 `fetch_ai_enrichment_tasks()` 增加显式 task id 过滤。
- CQUAE/GXCQ/CBEX 等通用 handler 的详情阶段已有逐条 checkpoint，但长分页 `fetch_list()` 阶段仍没有页级 checkpoint；超长列表在列表阶段中断时仍可能重扫列表。

## 15. 2026-07-12 补充：CQUAE 前端触发命令对齐、标的详情展示优化

本轮处理用户反馈：

- 前端任务按钮/重试/继续采集链路需要确认是否使用最新版 CQUAE 全量命令。
- 标的详情中 `bid_records_json` 直接显示 JSON，不利于人工查看。
- 特有字段里的现场图片/图片 URL 需要做成可预览缩略图。

本轮已修改：

- `web_admin/services/task_trigger.py`
  - `trigger_crawl()` 在 `mode="full"` 时统一把 CLI `--limit` 写为 `0`，与当前全量采集约定一致。
  - `platform="cquae"` 时自动追加最新版 CQUAE 参数：
    - `--request-timeout 0`
    - `--browser-timeout-ms 0`
    - `--cquae-page-size 60`
    - `--cquae-max-pages 0`
    - `--cquae-browser-settle-ms 800`
    - `--cquae-profile-path <project_root>\.browser\cquae`
  - 因任务管理、批次重试、继续采集都走 `trigger_crawl()`，这些入口会继承同一套 CQUAE 默认命令。
- `web_admin/static/index.html`
  - 标的详情的出价记录从 JSON 文本改为表格展示：
    - 出价金额
    - 出价时间
    - 出价人
    - 状态（领先/我的出价）
  - 出价时间支持毫秒时间戳/秒时间戳/普通时间字符串格式化。
  - 特有字段值中识别图片 URL，渲染为可点击缩略图，并复用资源预览弹窗。
  - 支持京东 `jfs/...` 图片路径归一化为 `https://img30.360buyimg.com/popWaterMark/...`。
- `tests/test_web_admin_task_runtime.py`
  - 增加 CQUAE full 触发命令回归测试，锁定最新版参数。
- `tests/test_web_admin_frontend.py`
  - 增加出价记录表格化回归测试。
  - 增加特有字段图片可预览回归测试。

已验证：

```powershell
python -m pytest tests/test_web_admin_task_runtime.py::WebAdminTaskRuntimeTests::test_trigger_crawl_cquae_full_uses_latest_browser_defaults -q
python -m pytest tests/test_web_admin_frontend.py::WebAdminFrontendTests::test_item_detail_renders_bid_records_as_table tests/test_web_admin_frontend.py::WebAdminFrontendTests::test_item_detail_special_image_fields_are_previewable -q
python -m pytest tests/test_web_admin_task_runtime.py tests/test_web_admin_frontend.py -q
python -m pytest tests -q
python -m py_compile web_admin\services\task_trigger.py web_admin\routers\jobs.py web_admin\services\scheduler_service.py web_admin\routers\queues.py multi_platform_runner.py
node --check _tmp_frontend_script.js
```

验证结果：

- `tests`: 168 passed。
- `py_compile` 通过。
- 前端 `<script>` 提取后 `node --check` 通过。

当前仍需注意：

- CQUAE 专属参数目前是服务层默认值，不是任务表单中的可编辑字段；如果后续需要在页面中按任务单独调整 page size、profile path、settle ms，需要给 `crawl_jobs` 增加字段或做 `parameters_json` 配置。
- 出价记录表格覆盖了常见字段名：`price/bidPrice/currentPrice/amount`、`bidTime/time/createTime/createdAt`、`username/userName/bidderName/bidder/userId`。如果某个平台返回完全不同结构，需要再补映射。
- 特有字段图片预览主要识别 http(s) 图片 URL、协议相对 URL 和京东 `jfs/...` 路径；附件表里的图片仍以“资源附件”页签为主展示。

## 16. 2026-07-12 补充：代码安全审核与硬化修复

本轮处理用户要求：
- 全面审核当前代码中的明显安全漏洞，并对能本地验证的高优先级问题直接修复。

本轮已修复：
- `web_admin/static/index.html`
  - 标的详情“来源页面”不再拼接 HTML 字符串并通过 `v-html` 渲染。
  - 改为 `safeSourceUrl()` 校验 http(s) URL 后用 Vue `:href` 属性绑定输出，避免数据库字段混入 HTML/脚本造成前端 XSS。
- `web_admin/routers/items.py`
  - 资源代理新增 `_is_safe_proxy_url()`，阻止代理 `localhost`、回环地址、私网地址、link-local、保留地址、非 http(s) scheme。
  - 去掉 `httpx.AsyncClient(..., verify=False)`，恢复 TLS 证书校验。
- `platform_adapters/gxcq_adapter.py`
- `platform_adapters/gxcq_adapter_v2.py`
- `platform_adapters/_gxcq_adapter_no_future.py`
  - 移除硬编码 GXCQ appsecret。
  - `GXCQ_LIST_APP_SECRET` 改为只从环境变量读取；未配置时不再向请求参数加入 `appsecret`。
- `_extract_ali_tk_token.py`
  - 改为标准 `argparse` 入口。
  - 默认只输出 token，不再自动写入 `.env`。
  - 只有显式传 `--save-env` 时才写入 `ALI_TK_TOKEN`。
- `web_admin/main.py`
  - CORS 不再使用 `allow_origins=["*"]` 搭配 `allow_credentials=True`。
  - 默认只允许 `http://127.0.0.1:8000,http://localhost:8000`；如需外部前端，使用 `WEB_ADMIN_CORS_ALLOW_ORIGINS` 配置。
  - API 404/500 fallback 不再返回 `str(exc)`，避免异常详情泄露给前端。
- `tests/test_security_hardening.py`
  - 新增安全回归测试，覆盖资源代理 SSRF、GXCQ 硬编码 secret、阿里 token 落盘、CORS、异常详情泄露和 TLS 校验。
- `tests/test_web_admin_frontend.py`
  - 新增来源页面不使用 `v-html` 的回归测试。

已验证：
```powershell
python -m pytest tests/test_security_hardening.py -q
python -m pytest tests -q
python -m compileall -q multi_platform_runner.py jd_scraper_v2.py jd_mysql_store.py web_admin platform_adapters _extract_ali_tk_token.py
$html = Get-Content -Raw -Encoding UTF8 web_admin\static\index.html; $m = [regex]::Match($html, '(?s)<script>(.*)</script>'); Set-Content -Path _tmp_frontend_script.js -Value $m.Groups[1].Value -Encoding UTF8; node --check _tmp_frontend_script.js; Remove-Item _tmp_frontend_script.js
rg -n 'v-html|dangerouslySetInnerHTML|innerHTML\s*=|shell=True|os\.system|eval\(|exec\(|pickle\.loads|yaml\.load|PHPCMFA0EF8F01A56FF|Auto-save to \.env|verify=False' -S --glob '!__pycache__/**' --glob '!*.pyc' --glob '!outputs/**' --glob '!tests/**' .
rg -n 'allow_origins=\["\*"\]|detail.: str\(exc\)' -S --glob '!__pycache__/**' --glob '!*.pyc' --glob '!outputs/**' --glob '!tests/**' web_admin
```

验证结果：
- `tests/test_security_hardening.py`: 5 passed, 10 subtests passed。
- `tests`: 174 passed, 10 subtests passed。
- `compileall` 通过。
- 前端 `<script>` 提取后 `node --check` 通过。
- 高危模式复扫无命中。

当前仍需注意：
- `F:\codex_project\jd` 当前不是 Git 仓库；真实 Git 仓库看起来是 `F:\codex_project\credit-claim-extraction`，但本轮改动发生在 `jd` 目录，不会自动进入该仓库。
- `.env` 文件存在，本轮未读取、未输出、未提交其中内容。
- Web 管理后台仍缺少完整登录/权限体系；本轮只做了本地可验证的代码级硬化，没有实现认证授权。
- MySQL CLI 默认密码仍有开发便利性风险；如果要转生产，应改为必须从环境变量或命令行显式提供，并配合只读账号/最小权限账号。
- GXCQ 若实际接口必须带 `appsecret`，后续运行前需要在环境变量中配置 `GXCQ_LIST_APP_SECRET`。

## 17. 2026-07-12 补充：本机优先、未来局域网/公网访问的认证与重置保护

本轮处理用户补充说明：
- 当前 Web 后台只在本机使用。
- 后续可能开放到局域网或公网。
- 当前 MySQL 是本地库，后续会更换到其他数据库。

本轮策略：
- 不默认打断当前本机使用。
- 增加可配置认证能力，开放到局域网/公网前可以通过环境变量或命令行启用。
- 对数据库重置增加第二道环境变量确认，避免误删新数据库。

本轮已修复：
- `web_admin/config.py`
  - 新增 `auth_enabled`、`admin_username`、`admin_password`、`auth_session_ttl_seconds`、`auth_cookie_secure` 配置。
  - 支持通过 `WEB_ADMIN_AUTH_ENABLED`、`WEB_ADMIN_ADMIN_USERNAME`、`WEB_ADMIN_ADMIN_PASSWORD`、`WEB_ADMIN_AUTH_COOKIE_SECURE` 配置。
- `web_admin/auth.py`
  - 新增认证模块。
  - 提供 `/api/auth/status`、`/api/auth/login`、`/api/auth/logout`。
  - 登录成功后写入 HttpOnly cookie。
  - 认证启用时保护 `/api/*`，但放行认证接口。
- `web_admin/main.py`
  - 接入认证中间件和 auth router。
  - CLI 增加 `--auth-enabled`、`--admin-username`、`--admin-password`。
- `web_admin/static/index.html`
  - 增加登录门禁。
  - 认证未启用时保持原有本机使用体验。
  - 认证启用且未登录时显示登录页，登录后再加载后台数据。
- `jd_mysql_store.py`
  - 新增 `require_db_reset_allowed()`。
  - 数据库重置除原有 `--confirm-reset` 外，还要求环境变量 `ALLOW_DB_RESET=1`。
- `multi_platform_runner.py`
  - `--reset-db --confirm-reset-db` 之外，额外要求 `ALLOW_DB_RESET=1`。
- `jd_scraper_v2.py`
  - `--reset-db --confirm-reset-db` 之外，额外要求 `ALLOW_DB_RESET=1`。
- `.env.example`
  - 移除真实/疑似真实 GXCQ appsecret 示例值。
  - 增加 Web auth、CORS、数据库重置保护配置示例。
- `tests/test_web_admin_auth.py`
  - 新增认证启用/禁用、登录成功/失败、数据库 reset gate 测试。
- `tests/test_web_admin_frontend.py`
  - 新增前端认证门禁静态回归测试。

启用认证示例：
```powershell
$env:WEB_ADMIN_AUTH_ENABLED="true"
$env:WEB_ADMIN_ADMIN_USERNAME="admin"
$env:WEB_ADMIN_ADMIN_PASSWORD="<set-a-strong-password>"
python -m web_admin.main --host 127.0.0.1 --port 8000
```

开放局域网/公网前至少需要：
```powershell
$env:WEB_ADMIN_AUTH_ENABLED="true"
$env:WEB_ADMIN_ADMIN_PASSWORD="<set-a-strong-password>"
$env:WEB_ADMIN_CORS_ALLOW_ORIGINS="https://your-admin-domain.example"
```

运行破坏性重置命令现在需要三重意图：
```powershell
$env:ALLOW_DB_RESET="1"
python multi_platform_runner.py crawl --reset-db --confirm-reset-db ...
```

已验证：
```powershell
python -m pytest tests/test_web_admin_auth.py tests/test_web_admin_frontend.py::WebAdminFrontendTests::test_frontend_has_auth_gate_for_future_remote_access -q
python -m pytest tests -q
python -m compileall -q multi_platform_runner.py jd_scraper_v2.py jd_mysql_store.py web_admin platform_adapters _extract_ali_tk_token.py
$html = Get-Content -Raw -Encoding UTF8 web_admin\static\index.html; $m = [regex]::Match($html, '(?s)<script>(.*)</script>'); Set-Content -Path _tmp_frontend_script.js -Value $m.Groups[1].Value -Encoding UTF8; node --check _tmp_frontend_script.js; Remove-Item _tmp_frontend_script.js
```

验证结果：
- 新增认证/reset 测试通过。
- 全量测试通过：180 passed, 1 warning, 10 subtests passed。
- `compileall` 通过。
- 前端 `<script>` 提取后 `node --check` 通过。

当前仍需注意：
- 认证目前是单管理员、内存 session。服务重启会要求重新登录；这对当前阶段可以接受，后续如果公网长期运行，应迁移到持久 session 或反向代理认证。
- 认证默认关闭，符合当前本机使用；一旦监听局域网/公网地址，必须显式开启。
- `ALLOW_DB_RESET=1` 是临时操作开关，不能长期写在生产环境变量里。

## 18. 2026-07-12 补充：登录页美化、README、同步 GitHub 仓库准备

本轮处理用户要求：
- 认证相关配置可以写入 `.env`，后续 MySQL 可用于用户/权限表。
- 登录页面需要更美观。
- 写一份完整 README。
- 同步到现有 GitHub 仓库 `F:\codex_project\credit-claim-extraction`，后续按版本/分支/功能提交。

设计结论：
- 当前阶段认证开关、管理员账号密码、CORS 白名单继续放 `.env`/环境变量。
- 后续局域网/公网长期运行时，再把 MySQL 用作用户表、角色表、审计日志表。
- 不建议把“是否启用认证”和“首个管理员密码”只放 MySQL，避免数据库不可用时认证状态不可控。

本轮已修改：
- `web_admin/static/index.html`
  - 登录页从简单卡片升级为管理后台登录布局：
    - 左侧系统说明与状态指标。
    - 右侧管理员登录卡片。
    - 响应式移动端布局。
    - 保持认证未启用时本机体验不变。
- `tests/test_web_admin_frontend.py`
  - 新增登录页视觉结构回归测试。
- `README.md`
  - 新增完整项目 README，覆盖：
    - 项目定位和能力。
    - 目录结构。
    - 环境准备。
    - `.env` 配置。
    - MySQL 初始化。
    - Web 后台启动和认证启用。
    - 常用采集命令。
    - CQUAE 最新命令。
    - AI 队列和模型配置。
    - 断点续传。
    - 安全注意事项。
    - 测试命令。
    - Git 工作流建议。

已验证：
```powershell
python -m pytest tests/test_web_admin_frontend.py::WebAdminFrontendTests::test_login_page_has_polished_admin_layout tests/test_web_admin_frontend.py::WebAdminFrontendTests::test_frontend_has_auth_gate_for_future_remote_access -q
python -m pytest tests -q
$html = Get-Content -Raw -Encoding UTF8 web_admin\static\index.html; $m = [regex]::Match($html, '(?s)<script>(.*)</script>'); Set-Content -Path _tmp_frontend_script.js -Value $m.Groups[1].Value -Encoding UTF8; node --check _tmp_frontend_script.js; Remove-Item _tmp_frontend_script.js
```

验证结果：
- 登录页/认证门禁前端测试通过。
- 全量测试通过：181 passed, 1 warning, 10 subtests passed。
- 前端 `<script>` 提取后 `node --check` 通过。

Git 同步目标：
```text
F:\codex_project\credit-claim-extraction\projects\jd-auction-multiplatform
```

同步注意事项：
- 不同步 `.env`。
- 不同步 `outputs/`、日志、缓存、`__pycache__`、浏览器 profile、数据库导出。
- 根仓库 README 只补项目入口，不覆盖原债权图片提取说明。
