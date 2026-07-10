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
