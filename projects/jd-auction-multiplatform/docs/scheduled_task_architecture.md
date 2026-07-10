# 拍卖数据采集系统 —— 定时任务平台架构设计

> 文档版本：v1.0
> 最后更新：2026-07-02
> 适用系统：多平台司法拍卖资产采集系统 (JD Auction Crawler v2.0)

---

## 一、架构概览

本系统采用 **三层异步任务调度架构**，以 MySQL 关系型数据库作为任务状态中心，支持多 Worker 并发、失败重试、Worker 抢占与超时回收。

```
┌────────────────────────────────────────────────────────────────────┐
│                    调度方式（外部触发层）                           │
│                                                                    │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  手动 CLI    │  │  Windows 计划  │  │  外部定时触发系统（未来）│ │
│  │  命令行调用   │  │  任务/系统cron  │  │   (APScheduler/分布式)  │ │
│  └──────┬──────┘  └──────┬───────┘  └────────────┬──────────────┘ │
│         │                │                        │                │
│         └────────────────┴────────────────────────┘                │
│                              │                                     │
│                              ▼                                     │
│              ┌──────────────────────────────┐                      │
│              │    Python CLI 入口层          │                      │
│              │  jd_scraper_v2.py/main()     │                      │
│              │  multi_platform_runner/main()│                      │
│              └──────────────┬───────────────┘                      │
│                             │                                      │
│                             ▼                                      │
│              ┌──────────────────────────────┐                      │
│              │    调度与采集执行层             │                      │
│              │  JDAuctionScraper            │                      │
│              │  MultiPlatformRunner         │                      │
│              └──────────────┬───────────────┘                      │
│                             │                                      │
│                             ▼                                      │
│              ┌──────────────────────────────┐                      │
│         ┌────┤    数据库任务队列层（MySQL）    ├────┐                 │
│         │    │  crawl_jobs                  │    │                 │
│         │    │  crawl_job_runs              │    │                 │
│         │    │  crawl_batches               │    │                 │
│         │    │  crawl_queue                 │    │                 │
│         │    │  ai_enrichment_queue         │    │                 │
│         │    │  ocr_retry_queue             │    │                 │
│         │    │  review_queue                │    │                 │
│         └────┴──────────────────────────────┴────┘                 │
└────────────────────────────────────────────────────────────────────┘
```

---

## 二、设计目标与原则

| 目标 | 说明 |
|------|------|
| **去中心化** | 无中央调度器，任何 Worker 都可以通过 CLI 独立触发任务 |
| **数据库即队列** | 任务状态全部持久化在 MySQL，不依赖内存队列或第三方 MQ |
| **抢占式 Worker** | Worker 通过 `locked_by` + `locked_at` 实现任务抢占，支持超时回收 |
| **分阶段解耦** | 采集 → AI 提取 → OCR 兜底三个阶段独立调度，异步执行 |
| **失败重试** | 每个队列支持 `retry_count`/`max_retries`/`last_error` |
| **可观测性** | `crawl_job_runs` / `crawl_batches` 记录每次执行的全量统计 |

---

## 三、分层架构详解

### 3.1 第一层：外部定时触发（操作系统级）

系统**自身不内置定时调度器**。定时执行通过外部 OS 级任务计划实现：

**Windows 计划任务 (Task Scheduler)**

```
任务名称: AuctionCrawler-Daily-JD
触发器: 每天 08:00（按需调整）
操作: python jd_scraper_v2.py crawl --per-category-limit 20

任务名称: AuctionCrawler-Daily-Other
触发器: 每天 08:30
操作: python multi_platform_runner.py crawl --platform all --limit 20

任务名称: AuctionCrawler-AI-Enrich
触发器: 每天 09:00（待采集完成后执行）
操作: python multi_platform_runner.py ai-enrich --limit 50 --worker-id ai-worker-1
```

**Linux/Crontab（未来部署计划）**

```
# 每天 8:00 采集京东拍卖
0 8 * * * cd /opt/auction-crawler && python jd_scraper_v2.py crawl --per-category-limit 20

# 每天 8:30 采集其他平台
30 8 * * * cd /opt/auction-crawler && python multi_platform_runner.py crawl --platform all --limit 20

# 每天 9:00 AI 补提取
0 9 * * * cd /opt/auction-crawler && python multi_platform_runner.py ai-enrich --limit 50
```

**设计考量：**
- **为什么不内置调度器？** 当前项目部署在单机 Windows 环境，Windows 计划任务稳定可靠。如未来需要分布式部署，可以引入 APScheduler 或 Celery Beat + Redis 作为调度中心，但后台队列层（MySQL）无需修改。
- **OS 级调度 vs 应用内调度**：OS 级调度更简单、调试直观、运维人员无需了解 Python 调度框架。当前架构下任务执行是一次性的 CLI 进程，运行完即退出，不占用内存。

---

### 3.2 第二层：Python CLI 入口层

所有任务通过统一的 CLI 入口触发，采用 `argparse` 子命令模式：

| 入口文件 | 命令 | 功能 |
|---------|------|------|
| `jd_scraper_v2.py` | `crawl` | 采集京东拍卖数据 |
| `multi_platform_runner.py` | `crawl` | 采集阿里/重庆产权/e交易数据 |
| `multi_platform_runner.py` | `ai-enrich` | 消费 `ai_enrichment_queue` 执行异步 AI 提取 |
| `jd_mysql_store.py` | `schema/store` | MySQL V2 schema and storage layer |

每个 CLI 命令是一个独立进程，执行完毕后正常退出。**无守护进程、无常驻内存**。

---

### 3.3 第三层：调度与采集执行层

#### 3.3.1 京东采集调度 (`JDAuctionScraper.crawl_sample`)

```
开始
  │
  ├─ init_schema()          ← 确保数据库表存在
  ├─ seed_field_catalog()   ← 初始化字段字典
  ├─ start_batch()          ← 创建批次记录 (crawl_batches)
  │
  ├─ 按类目循环
  │   ├─ 搜索类目下的标的列表
  │   ├─ 逐条调用 _crawl_one()
  │   │   ├─ fetch_detail()         ← 调用京东 API 获取详情
  │   │   ├─ parse_detail_html()    ← HTML→纯文本解析
  │   │   ├─ extract_common_fields() ← 通用字段提取
  │   │   ├─ extract_special_fields() ← 特有字段提取
  │   │   ├─ extract_ai_fields()    ← AI 批量提取（同步可选）
  │   │   ├─ upsert_item()          ← 写入数据库
  │   │   └─ enqueue_ai/ocr()       ← 异步任务入队
  │   └─ 异常捕获，单条失败不影响批次
  │
  ├─ finish_batch()         ← 关闭批次
  └─ export_csvs()          ← 可选导出 CSV
```

#### 3.3.2 多平台采集调度 (`MultiPlatformRunner.crawl_platform`)

```
开始
  │
  ├─ start_batch()          ← 创建批次记录
  │
  ├─ handler.fetch_list()   ← 获取平台列表数据
  ├─ 对每条：
  │   ├─ handler.fetch_detail()   ← 获取平台详情
  │   ├─ handler.build_record()   ← 构建统一记录
  │   ├─ _apply_ai()              ← 同步 AI 提取
  │   ├─ _write_record()          ← 写入数据库
  │   └─ _enqueue_ai()            ← 异步 AI 入队
  │
  ├─ finish_batch()         ← 关闭批次
  └─ 返回 PlatformCrawlResult
```

#### 3.3.3 AI 补提取调度 (`MultiPlatformRunner.process_ai_enrichment_queue`)

```
开始
  │
  ├─ fetch_ai_enrichment_tasks()  ← 从 MySQL 拉取待处理任务
  │   ├─ 状态 = pending ← 待处理
  │   ├─ 状态 = failed + retry_count < max_retries ← 可重试
  │   └─ 状态 = running + locked_at 超时 ← Workers 崩溃回收
  │
  ├─ 对每条任务（乐观锁）：
  │   ├─ 设置 queue_status=running, locked_by=worker_id
  │   ├─ 调用 _batch_extract_ai() 执行 AI 提取
  │   ├─ 写入结果到数据库
  │   ├─ mark_ai_enrichment_task_success()
  │   └─ 失败 → mark_ai_enrichment_task_failed()
  │
  └─ 返回处理统计
```

---

### 3.4 第四层：数据库任务队列层

#### 3.4.1 队列全景

```
┌──────────────────────────────────────────────────────────────────┐
│                        MySQL 数据库                              │
│                                                                  │
│  crawl_jobs           ─── 定时任务配置（静态元数据）                │
│      │                                                          │
│      ▼                                                          │
│  crawl_job_runs       ─── 每次调度的执行记录                      │
│      │                                                          │
│      ▼                                                          │
│  crawl_batches        ─── 采集批次（每次采集的唯一批次）            │
│      │                                                          │
│      ├──────────────┬──────────────┬──────────────┐              │
│      ▼              ▼              ▼              ▼              │
│  auction_items   crawl_queue   raw_payloads   field_extractions  │
│      │                                                          │
│      ├──→ ai_enrichment_queue  (异步 AI 提取)                    │
│      ├──→ ocr_retry_queue      (异步 OCR 兜底)                    │
│      └──→ review_queue         (人工审核队列)                     │
│                                                                  │
│  asset_*_details   ─── 各资产类型明细表（逐户/逐项）               │
│  asset_dedup_index ─── 跨平台去重索引                             │
└──────────────────────────────────────────────────────────────────┘
```

#### 3.4.2 各队列的职责与设计

##### `crawl_jobs` — 定时任务配置表

| 字段 | 说明 |
|------|------|
| `job_id` | 自增主键 |
| `job_name` | 任务名称，如 "每日京东-房地产" |
| `source_platform` | 目标平台：jd/ali/ejy365/cquae |
| `cron_expr` | Cron 表达式（当前由外部 OS 调度，此字段为未来预留） |
| `category_scope` | JSON，限定类目或资产类型 |
| `page_limit` | 每次扫描页数 |
| `per_category_limit` | 每类最大采集数 |
| `throttle_seconds` | 请求间隔 |
| `ai_enabled` | 是否启用 AI 提取 |
| `enabled` | 启用/禁用开关 |

> **当前使用方式**：MySQL 中预置配置记录 → OS 定时任务读取（直接通过 CLI 参数覆盖）→ 执行采集。`cron_expr` 字段预留给未来集成 APScheduler 使用。

##### `crawl_job_runs` — 任务执行记录表

每次 OS 定时任务触发后，创建一条执行记录，关联到 `crawl_jobs` 配置。记录本次执行的全量统计（scan/queue/success/fail）。

##### `crawl_batches` — 采集批次表

一次采集调度的独立批次，关联 `crawl_job_runs.run_id`。`batch_id` 格式：`YYYYMMDD_HHmmSS_8位UUID`。

##### `crawl_queue` — 采集详情的任务队列

- 批量列表扫描后，逐条写入 `crawl_queue`
- Worker 通过 `locked_by`/`locked_at` 抢占
- 唯一键：`(source_platform, source_item_id, batch_id)`，防止同一批次同标的重复入队
- 支持 `pending → running → success/failed` 状态流转
- `max_retries=3`，超过后标记为 `failed`

##### `ai_enrichment_queue` — AI 补提取队列

| 字段 | 说明 |
|------|------|
| `item_id` | FK → auction_items |
| `task_type` | `field_enrichment` |
| `context_json` | 采集时保存的 AI 上下文（HTML 表格、公告文本、图片 URL） |
| `queue_status` | `pending/running/success/failed` |
| `priority` | 优先级，越小越优先 |
| `locked_by` | Worker 标识 |
| `locked_at` | 锁定时间 |
| `retry_count` | 当前重试次数 |
| `max_retries` | 最大重试次数（默认 3） |
| `result_json` | 成功后的 AI 提取结果 |

**Worker 抢占逻辑（`fetch_ai_enrichment_tasks`）：**

```
1. 查询条件：
   - queue_status='pending'
   - queue_status='failed' AND retry_count < max_retries
   - queue_status='running' AND locked_at < NOW() - 30分钟（超时回收）
2. 按 priority ASC, ai_task_id ASC 排序（先进先出+优先级）
3. 锁定：SET queue_status='running', locked_by=worker_id, locked_at=NOW()
4. Worker 处理完成后写入结果
5. 处理失败：retry_count+1，queue_status 回退为 'pending'（可重试）
   或 retry_count >= max_retries → 'failed'
```

##### `ocr_retry_queue` — OCR 视觉识别兜底队列

- 仅当文本方式提取不到知产明细（IP Details）、且页面存在图片表格时写入
- `task_type`：`ip_image_details` 等
- 唯一键：`(source_platform, source_item_id, task_type)`，防止同一标的重复入队
- Worker 锁定机制与 `ai_enrichment_queue` 相同

##### `review_queue` — 人工审核队列

- 当字段出现冲突/低置信度/缺失时自动入队
- `issue_type`：`missing/conflict/low_confidence/invalid_value/duplicate`
- `candidate_values_json`：多个候选值供人工选择
- `final_value`：人工确认后的值

---

## 四、关键设计决策

### 4.1 为什么选择数据库即队列？

| 对比维度 | 数据库即队列（本方案） | 独立消息队列（如 RabbitMQ） |
|---------|----------------------|--------------------------|
| 部署复杂度 | 零额外组件 | 需额外安装运维 MQ |
| 事务一致性 | 原生 ACID，业务数据与队列在同一个事务 | 需分布式事务/最终一致性 |
| 任务持久化 | 天然持久化 | 需配置持久化策略 |
| 任务查询/审计 | 直接 SQL 查询 | 需管理工具 |
| 延迟要求 | 秒级（本系统可接受） | 毫秒级 |
| Worker 数量 | 多 Worker 通过乐观锁抢单 | 原生消费组支持 |

**结论**：对于拍卖数据采集这种对实时性不敏感（分钟级延迟可接受）、但重视审计和事务一致性的场景，数据库即队列是更简洁可靠的选择。

### 4.2 Worker 如何避免重复处理？

```
场景 1：同一任务被两个 Worker 同时取到
  ┌─────────────────────────────────────────┐
  │  Worker A: BEGIN TRANSACTION            │
  │  Worker A: SELECT ... WHERE status=pending LIMIT 10  │
  │  Worker A: UPDATE SET status=running,    │
  │            locked_by='worker-a',          │
  │            locked_at=NOW()               │
  │  Worker A: COMMIT                        │
  │                                          │
  │  Worker B: BEGIN TRANSACTION            │
  │  Worker B: SELECT ... (相同的 LIMIT 10)  │
  │  → 此时状态已为 running，不会被查到       │
  │  → 即使查到，UPDATE 也会跳过             │
  └─────────────────────────────────────────┘

场景 2：Worker 崩溃导致任务卡住
  ┌─────────────────────────────────────────┐
  │  Worker 锁定任务后崩溃                    │
  │  queue_status='running'                  │
  │  locked_at=<崩溃时间>                    │
  │                                          │
  │  30 分钟后，其他 Worker 查询：             │
  │  WHERE locked_at < NOW() - 30 MINUTES   │
  │  → 回收该任务，重新执行                   │
  └─────────────────────────────────────────┘
```

### 4.3 超时回收时间如何选择？

`stale_minutes=30` 的选择依据：

- 每个标的 AI 提取耗时通常在 10-30 秒
- 单次 `ai-enrich` 调用默认取 20 个任务，总耗时 < 10 分钟
- 留出 3 倍余量（30 分钟）避免正常慢任务被误回收
- 如需调整，通过 `fetch_ai_enrichment_tasks(stale_minutes=N)` 参数控制

### 4.4 失败与重试策略

```
                ┌──────────┐
                │  PENDING  │
                └────┬─────┘
                     │ Worker 抢占
                     ▼
                ┌──────────┐
                │  RUNNING  │ ← 超时 30 分钟自动回收
                └────┬─────┘
                     │
            ┌────────┴────────┐
            ▼                 ▼
       ┌────────┐      ┌──────────┐
       │ SUCCESS │      │  FAILED  │
       └────────┘      └────┬─────┘
                             │
                   retry_count < max_retries (3) ?
                   ┌────┴────┐
                   YES       NO
                   ▼          ▼
              ┌────────┐ ┌────────┐
              │ PENDING │ │ FAILED │ (最终态)
              └────────┘ └────────┘
```

---

## 五、Worker 部署方案

### 5.1 当前单机部署（Windows）

```
┌─────────────────────────────────────────────┐
│              单机 Windows Server             │
│                                              │
│  08:00  python jd_scraper_v2.py crawl ...   │  ← 计划任务 1
│  08:30  python multi_platform_runner.py ...  │  ← 计划任务 2
│  09:00  python multi_platform_runner.py      │  ← 计划任务 3
│         ai-enrich --limit 50                 │
│  09:30  python multi_platform_runner.py      │  ← 计划任务 4
│         ai-enrich --limit 50 —worker-id ... │
│                                              │
│  MySQL (本地或局域网)                          │
└─────────────────────────────────────────────┘
```

### 5.2 未来分布式扩展

```
                        MySQL (中心数据库)
                     ┌──────────────────┐
                     │  任务队列         │
                     │  crawl_queue      │
                     │  ai_enrichment... │
                     └────────┬─────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
         ▼                    ▼                    ▼
   ┌────────────┐      ┌────────────┐      ┌────────────┐
   │ Worker 1   │      │ Worker 2   │      │ Worker N   │
   │ (采集)     │      │ (采集)     │      │ (AI提取)   │
   │ jd/ali     │      │ ejy365/     │      │ 多个副本   │
   │            │      │ cquae      │      │            │
   └────────────┘      └────────────┘      └────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   ┌────────────┐      ┌────────────┐      ┌────────────┐
   │ 每个 Worker │      │ 独立机器     │      │ 独立进程    │
   │ 独立机器     │      │ or 同一机器  │      │ 互不干扰    │
   └────────────┘      └────────────┘      └────────────┘
```

---

## 六、任务数据流时序图

```
OS 计划任务        CLI 入口           JDAuctionScraper          MySQL
    │                │                     │                     │
    │  触发 crawl    │                     │                     │
    │───────────────>│                     │                     │
    │                │  start_batch()      │                     │
    │                │─────────────────────│───── INSERT ───────->│
    │                │                     │                     │
    │                │  循环类目           │                     │
    │                │  │───── API ────────│─ fetch_list ────────>│
    │                │  │<──── 列表 ───────│<──── return ─────────│
    │                │  │                 │                     │
    │                │  │  逐条采集        │                     │
    │                │  │  ├─ API 详情     │<─ API ──> 京东      │
    │                │  │  ├─ HTML 解析    │                     │
    │                │  │  ├─ 字段提取     │                     │
    │                │  │  ├─ AI 提取      │                     │
    │                │  │  └─ upsert      │───── INSERT ────────>│
    │                │  │                 │  + 入队(可选)        │
    │                │  │                 │                     │
    │                │  │  ... 下一条      │                     │
    │                │                     │                     │
    │                │  finish_batch()    │───── UPDATE ────────>│
    │                │  export_csv()      │                     │
    │                │  print summary     │                     │
    │<───────────────│                     │                     │
    │                │                     │                     │
    │  触发 ai-enrich│                     │                     │
    │───────────────>│                     │                     │
    │                │  fetch tasks       │───── SELECT ────────>│
    │                │<─── tasks ──────────│<──── return ─────────│
    │                │                     │                     │
    │                │  for each task:    │                     │
    │                │  ├─ AI 提取        │                     │
    │                │  ├─ 写结果         │───── UPDATE ────────>│
    │                │  └─ 标记完成       │                     │
    │                │                     │                     │
    │<───────────────│  返回统计           │                     │
```

---

## 七、监控与运维

### 7.1 关键查询 SQL

```sql
-- 查看待处理的 AI 补提取任务
SELECT ai_task_id, source_item_id, asset_group, priority, retry_count, created_at
FROM ai_enrichment_queue
WHERE queue_status IN ('pending', 'running')
ORDER BY priority ASC, ai_task_id ASC;

-- 查看所有批次状态
SELECT batch_id, source_platform, status, scanned_count, success_count, failed_count,
       started_at, finished_at
FROM crawl_batches
ORDER BY started_at DESC
LIMIT 20;

-- 查看执行记录
SELECT jr.*, j.job_name
FROM crawl_job_runs jr
LEFT JOIN crawl_jobs j ON jr.job_id = j.job_id
ORDER BY jr.started_at DESC
LIMIT 20;

-- 运维：手动重试失败任务
UPDATE ai_enrichment_queue
SET queue_status='pending', retry_count=retry_count-1, last_error=NULL
WHERE queue_status='failed' AND retry_count > 0;

-- 运维：释放卡死的任务（注意：谨慎操作）
UPDATE ai_enrichment_queue
SET queue_status='pending', locked_by=NULL, locked_at=NULL
WHERE queue_status='running' AND locked_at < DATE_SUB(NOW(), INTERVAL 2 HOUR);
```

### 7.2 日志体系

系统采用结构化日志，每个关键动作记录事件标签：

| 日志标签 | 含义 |
|---------|------|
| `batch_started` | 批次开始 |
| `batch_finished` | 批次完成（含统计） |
| `category_processing` | 类目处理 |
| `item_crawl_success` | 单个标采集成功 |
| `item_crawl_failed` | 单个标采集失败 |
| `ai_extraction_success` | AI 提取成功 |
| `ai_extraction_failed` | AI 提取失败 |

日志格式示例：
```
[2026-07-02 08:00:15] [INFO] [batch_started] 开始采集批次: 20260702_080015_a1b2c3d4
[2026-07-02 08:00:16] [INFO] [category_processing] 处理类目: 房地产 (1001), 共 20 条
[2026-07-02 08:00:35] [INFO] [item_crawl_success] 采集完成: 309329493
[2026-07-02 08:10:42] [INFO] [batch_finished] 批次完成: 20260702_080015_a1b2c3d4, 成功 45, 失败 2
```

### 7.3 Windows 计划任务配置要点

```
1. 创建基本任务 → 填写名称和描述
2. 触发器 → 每日，开始时间 08:00
3. 操作 → 启动程序
   程序或脚本: C:\Users\AppData\Local\Programs\Python\Python311\python.exe
   添加参数: f:\codex_project\jd\jd_scraper_v2.py crawl --per-category-limit 20
   起始于: f:\codex_project\jd
4. 设置 → 允许任务按需运行
5. 设置 → 如果任务失败，重启每隔 5 分钟，最多 3 次
6. 设置 → 如果任务运行时间超过 60 分钟，停止任务（防止死循环）
```

---

## 八、当前局限性及未来优化方向

| 局限性 | 说明 | 改进方向 |
|--------|------|---------|
| 无内置调度引擎 | 依赖 OS 计划任务，分布式管理不便 | 集成 APScheduler 或 Celery Beat |
| 无动态 Worker 伸缩 | Worker 数量固定 | 基于队列长度自动扩缩容 |
| 无全局任务依赖编排 | AI 提取开始时间依赖经验估算 | 串行编排：采集完成 → AI 提取完成 → 报表生成 |
| 无 Web 管理界面 | 启停/重试需手动 SQL | 开发 Web Admin 面板 |
| 无告警通知 | 任务失败仅记录日志 | 集成企业微信/钉钉/邮件告警 |
| 无分布式锁 | 任务队列靠数据库乐观锁 | 任务量增大后可引入 Redis 分布式锁 |

---

## 九、附录：核心代码调用关系

```
multi_platform_runner.py              jd_scraper_v2.py              jd_mysql_store.py
┌───────────────────────┐        ┌──────────────────────┐        ┌───────────────────────┐
│                       │        │                      │        │                       │
│  main()               │        │  main()              │        │  start_batch()        │
│    ├─ parse_args()    │        │    ├─ parse_args()   │        │  finish_batch()       │
│    ├─ crawl           │        │    ├─ crawl_sample() │        │  enqueue_ai_enrich()  │
│    │  └─ crawl_platf  │        │    │  └─ _crawl_one()│        │  fetch_ai_enrich()    │
│    │     └─ _write_re │        │    │     ├─ extract_ │        │  mark_ai_task_*()     │
│    │     └─ _apply_ai │        │    │     └─ upsert   │        │  enqueue_ocr_retry()  │
│    │     └─ _enqueue  │        │    │                 │        │  upsert_*_item()      │
│    ├─ ai-enrich       │        │    └─ export_csvs()  │        │  upsert_raw_payloads()│
│    │  └─ process_ai.. │        │                      │        │  upsert_*_details()   │
│    └─ handlers        │        │  JDAuctionScraper    │        │                       │
│       ├─ Ejy365Live.. │        │  JDClient            │        │  MySQLJDScraperDB     │
│       ├─ CquaeLive..  │        │  MySQLJDScraperDatabase   │        │                       │
│       └─ AliLiveHandl │        └──────────────────────┘        └───────────────────────┘
└───────────────────────┘
```
