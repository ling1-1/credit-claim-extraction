# 多平台资产拍卖 AI 主提取采集方案

## 1. 当前目标

在现有京东资产拍卖采集系统基础上，扩展到多平台资产交易数据采集。首批平台包括：

- 京东资产拍卖：`paimai.jd.com` / `zcpm.jd.com`
- e 交易：`ejy365.com`
- 阿里拍卖：`zc-paimai.taobao.com`
- 山东产权公开门户：`sdcqjy.com`
- 重庆产权交易所：`cquae.com`
- 天津交易集团/天津产权交易中心：`trade.tpre.cn`
- 广西产权交易所：`gxcq.com.cn`
- 北京产权交易所/预披露平台：`cbex.com.cn`、`prechina.com`

整体策略是：

> AI 主提取，规则/API/浏览器只负责拿原文、构造证据、做验真，不再为每一种页面格式硬写一套完整字段规则。

这份文档描述后续开发、调试和验收都应遵守的统一方案。

## 2. 总体原则

1. **MySQL 是正式存储层**  
   后续采集结果直接写入本地 MySQL 数据库 `auction_data`。SQLite 只保留为历史兼容或临时调试，不作为正式数据源。

2. **字段结构优先复用现有 V2 表结构**  
   新平台优先写入 `auction_items`、各资产类型特有表、`field_extractions`、`raw_payloads`、`item_resources` 等现有表。只有现有字段无法表达时，才新增字段或表。

3. **字段值必须可追溯**  
   每个最终字段都要在 `field_extractions` 里保留来源：来源页面/接口、来源标签页、字段路径、原文片段、提取方法、置信度和缺失原因。

4. **AI 不允许无证据填值**  
   AI 可以判断字段、归纳字段、从长文本中抽取字段，但必须返回 `source_excerpt`。如果原文中找不到证据，该字段应为空，并记录缺失原因。

5. **主采集先入库，AI 异步补提取**  
   列表和详情抓取完成后，先把接口、HTML、附件、图片和规则可确认的字段写入 MySQL，再把完整 AI 上下文写入 `ai_enrichment_queue`。AI worker 后续异步补全缺失字段、债权明细、知识产权明细等内容，避免每天定时任务被单个慢模型或慢平台拖死。

6. **不绕过平台风控**  
   遇到验证码、登录墙、Knownsec/创宇盾等风控页，只做登录态复用、低频浏览器渲染、人工登录提示和失败原因记录，不做绕过。

7. **模型配置统一走 `.env` 或 MySQL 配置表**  
   当前本地默认模型配置为千问兼容接口：`AI_ACTIVE_PROFILE=qwen`、`AI_PROVIDER=qwen`、`AI_MODEL_NAME=qwen-plus`、`AI_VISION_MODEL=qwen-vl-plus`。后续需要切换 DeepSeek、OpenAI 或其他兼容模型时，只改 `.env` 或模型配置表，不在代码里硬编码 API Key、Base URL 或模型名。

## 3. 价格字段口径

用户最终需要的是“采集时刻的有效价格”，因此主表保留两组价格：

- `start_price_amount` / `start_price_display`：起拍价、挂牌价、转让底价。
- `final_price_amount` / `final_price_display`：本次采集时刻的最终价/有效价。

不再把 `current_price_*` 作为业务主字段输出。其逻辑并入 `final_price_*`：

- 未开始：`final_price = start_price`
- 正在进行：`final_price = 当前价`
- 已成交：优先取平台最终价/当前价/最后出价价；缺失时才用 `起拍价 + 出价次数 * 加价幅度` 兜底
- 未成交、撤拍、中止：优先取平台当前价；没有出价时回退到起拍价

价格来源必须写入 `field_extractions`，例如：

- `list_json/startPrice`
- `realtime_json/currentPrice`
- `detail_html/marketPrice`
- `ai_extraction/price_sentence`
- `formula_fallback/start_plus_bid_count`

## 4. “原始展示值”和“展示值”的处理

正式表不再保留两套重复字段。

- 主表/特有表中的 `*_display`：用于 Viewer 展示的最终展示值。
- `field_extractions.source_excerpt`：保留原文片段。
- `field_extractions.normalized_text`、`numeric_value`、`date_value`、`datetime_value`：保留标准化结果。

也就是说：

- 页面展示用 `*_display`
- 查询统计用 `*_amount` / `*_sqm` / `*_date`
- 审核追溯看 `field_extractions`

## 5. MySQL 数据模型

### 5.1 主表：`auction_items`

存所有平台共有字段，一条标的一条记录。

关键字段包括：

- 平台标识：`source_platform`、`source_item_id`、`source_url`
- 资产类型：`asset_group`、`asset_type`
- 项目信息：`project_name`、`asset_location`、`project_status`、`auction_stage`
- 时间：`signup_start_time`、`signup_end_time`
- 处置信息：`disposal_party`、`disposal_agency`
- 价格：`start_price_amount/display`、`final_price_amount/display`、`assessment_price_amount/display/date`
- 联系方式：`contact_info`
- 风险提示：`special_notice`、`disclosed_defects`
- 批次与状态：`batch_id`、`crawl_status`、`created_at`、`updated_at`

### 5.2 特有表

每种资产类型一张表：

- `asset_real_estate`
- `asset_land`
- `asset_vehicle`
- `asset_debt`
- `asset_equity`
- `asset_equipment`
- `asset_ip`
- `asset_goods`
- `asset_usufruct`
- `asset_other`

原则：

- 只放该类型确实特有的字段。
- 通用字段不重复放入特有表。
- 图片、附件、视频不在特有表塞 JSON，统一写 `item_resources`。
- 大段原文不放特有表，放 `raw_payloads` 或 `field_extractions.source_excerpt`。

### 5.3 证据表：`field_extractions`

每个字段的每个候选值一条记录。

关键字段：

- `field_namespace`：`common` / `special`
- `asset_group`
- `field_key`
- `display_value`
- `normalized_text`
- `numeric_value`
- `date_value`
- `datetime_value`
- `method`：`api` / `html_table` / `text_regex` / `ai_extraction` / `ocr`
- `source_payload_type`
- `source_tab`
- `source_path`
- `source_excerpt`
- `confidence`
- `is_selected`
- `missing_reason`

如果同一个字段来自多个来源，全部保留候选值，只把最终采用的那条标记为 `is_selected=1`。

### 5.4 资源表：`item_resources`

存图片、视频、附件链接。

资源类型：

- `image`
- `video`
- `attachment`
- `ocr_image`

现场图片、车辆图片、标的图片等都进入该表，通过 `resource_type=image` 和 `source_tab`、`source_section` 区分来源。

### 5.5 原始载荷表：`raw_payloads`

保存所有接口 JSON、页面 HTML、渲染文本、公告 HTML、须知 HTML。

该表用于复核和重新提取，不用于页面主展示。

## 6. 统一采集流程

每个平台 adapter 都遵守同一流程：

1. 抓列表页，得到 `PlatformListItem`
2. 抓详情页和辅助接口，得到 `PlatformDetailBundle`
3. 解析页面文本、表格、附件、图片，构造 AI 上下文
4. 规则/API 对标题、URL、价格、状态、附件、图片等可确认字段做验真或兜底
5. 标准化金额、日期、面积、电话
6. 写入 MySQL 主表、特有表、资源表、证据表、原始表
7. 将 AI 上下文写入 `ai_enrichment_queue`，由 worker 异步补提取共有字段、特有字段和明细表
8. 生成字段覆盖率、缺失原因和冲突记录

## 7. AI 提取策略

### 7.1 输入材料

AI 输入按优先级拼接：

1. 当前标的标题、平台、URL、资产类型
2. 详情页核心字段
3. 竞买公告全文
4. 竞买须知全文
5. 标的物详情全文
6. 表格键值对
7. 附件清单
8. 图片/视频 URL 清单
9. 规则预提取的时间、价格、联系人、风险提示候选句

### 7.2 输出格式

AI 必须输出结构化 JSON：

```json
{
  "field_key": {
    "value": "",
    "display_value": "",
    "normalized_value": "",
    "source_tab": "",
    "source_excerpt": "",
    "confidence": 0.0,
    "missing_reason": ""
  }
}
```

### 7.3 重要提示词约束

通用约束：

- 只提取和当前标的标题、标的编号、权证号、车牌号、债务人、权利人能对应上的内容。
- 如果公告里列出多个标的，必须定位当前标的对应的那一行或那一段，不能把其他标的的面积、评估价、权证号、车位号提取过来。
- “竞价时间”“拍卖时间”“变卖时间”是报名/竞价起止时间的重要来源。
- 不要把公告发布日期、展示看样期、资质审核截止日误当成报名起止时间。
- `assessment_price_time` 只允许来自明确的“评估价、市场价、评估基准日、评估报告”。
- 不允许把“市场价 2 倍”“参考价比例”“欠费金额”“保证金”“起拍价”误填为评估价。
- 联系方式要尽量保留完整联系人和电话，不要只保留第一个号码。
- 特别告知只提取“特别提示、特别提醒、风险提示、重大事项、瑕疵说明、注意事项”等与风险或交易限制相关的段落。

### 7.4 OCR 兜底

OCR 作为异步兜底任务，不阻塞主采集流程。

适用场景：

- 知识产权明细表是图片或扫描件。
- 债权清单在图片或附件截图中。
- 页面正文为空，但图片里存在关键表格。
- 附件接口可获取文件但正文解析失败。

OCR 结果写入：

- `raw_payloads`：保存 OCR 原始文本
- `field_extractions`：保存 OCR 提取出的候选字段
- 对应特有表：只写通过原文回检和字段校验的最终值

### 7.5 异步 AI 补提取

默认采集命令使用 `--ai-mode async`：

```powershell
python multi_platform_runner.py crawl --platform all --limit 10 --ai-mode async
```

该模式下，主采集阶段不会调用 AI，只负责抓取和入库：

- 已确认字段直接写入 `auction_items`、特有表和 `field_extractions`。
- 公告全文、须知全文、标的详情、表格键值对、附件列表、图片 URL 写入 `raw_payloads` 和 `item_resources`。
- 同一份 AI 上下文写入 `ai_enrichment_queue.context_json`。

AI worker 独立执行：

```powershell
python multi_platform_runner.py ai-enrich --limit 100 --worker-id ai-worker-1
```

Worker 从 `ai_enrichment_queue` 领取任务，调用 AI 后只更新非空且通过校验的字段；如果模型不可用、返回空结果、字段没有证据，或者 AI 结果全是空值/错误值，任务不得标记成功，而是进入重试或失败状态。这样主采集和 AI 补提取解耦，后续可以多开 worker 提升速度。

## 8. 平台适配器设计

### 8.1 京东资产拍卖

状态：已具备基础采集能力，正在向 MySQL V2 表结构迁移。

代码组织：

- 京东既有成熟采集链路暂时保留在 `jd_scraper_v2.py`。
- `platform_adapters/jd_adapter.py` 已作为统一 adapter 包入口，封装 `JDClient` 和 `JDAuctionScraper`。
- 下一步再把 JD 完整接入 `multi_platform_runner.py`，避免一次性重写稳定的京东 API 链路。

核心来源：

- 列表接口
- 详情接口
- 实时价格接口
- 描述 HTML
- 附件接口
- 处置方接口

重点问题：

- 多标的公告需要按当前标的标题/编号定位。
- 债权包需要写入债权明细表。
- 知识产权明细需要支持 HTML 表格、附件和 OCR 兜底；如果页面本身是单项知识产权且没有明细表，允许从“标的名称、标的证号、知产类型”生成一条 `asset_ip_details` 明细，但禁止把“7 项软件著作权”“17 项专利权”这类聚合描述当作逐项明细。
- 评估价必须严格验真，不能误提取“市场价 2 倍”等比例描述。

### 8.2 e 交易

状态：已具备初步列表和详情抓取能力，但 Viewer 查询要统一按 MySQL `source_platform/source_item_id` 查询，不能只按京东 ID 查询。

核心来源：

- 列表页：`jygg_more`
- 详情页 HTML
- 竞价记录接口
- 结束时间接口

重点问题：

- 列表采样需要覆盖多个 `project_type`，当前默认轮询 `ZQ`、`FC`、`CL`、`GQ`、`TD`、`ZSCQ`、`WZ`、`ZYSYQ`、`QT` 等类型；小批量测试时优先每类拿 1 条，避免 10 条样本都集中在债权。
- 债权类字段要优先提取主债务人、债权人、本金、利息、担保方式、抵押物、诉讼状态。
- e 交易项目编号不是数字，Viewer 路由必须支持字符串 ID。
- 部分详情页字段在附件或表格中，需要 AI 主提取。

### 8.3 阿里拍卖

状态：已验证 mtop 详情接口和公告接口可以拿到数据；新样本已能写入通用字段和房地产特有字段。

核心来源：

- mtop 列表/详情 JSON
- 公告内容接口：`mtop.com.taobao.auction.notice.content.get`
- 页面渲染文本
- 登录浏览器 profile 兜底

重点问题：

- 之前部分旧数据把公告接口请求参数误存为 `special_notice`，需要清空旧测试数据后重新跑。
- `page_text` 必须包含公告全文，AI 上下文不能只用渲染摘要。
- 房地产、车位、储藏室等类型要从标题和公告中稳定归入 `real_estate`。
- 处置方和处置机构要区分：法院、拍卖公司、资产公司不能混为一个字段。

### 8.4 山东产权公开门户

状态：已验证详情页和字段键值可以抓取，特有字段已能写入 MySQL。

核心来源：

- 列表页 HTML
- 详情页 HTML
- 字段表格
- 正文说明
- 附件链接：包括 `/attachment/noauthorizefiles/...` 这类非授权附件路径

重点问题：

- 已修正“处置机构”别名过宽导致误填评估机构的问题。
- 已修正权证号别名过宽导致误提取 ICP 备案号的问题。
- 已修正“企业类型/公司性质”等字段污染 `asset_type` 的问题。资产类型优先从标题和标的名称判断，`股份有限公司`、`有限责任公司`、`国有企业` 等只作为企业性质，不作为标的类型。
- 已修正标题中明确出现“债权/股权/设备/车辆/房产/土地”等词时，被正文里的“租赁、房产、市场价”等噪声带偏的问题。
- 已验证 `齐鲁银行5户债权资产包` 这类页面可解析出 `asset_group=debt`、`asset_type=债权`，并能拿到附件 URL。旧库里附件为空的记录需要重新采集。
- 当前部分字段内容偏长，例如房产状态、资产亮点，需要后续做摘要收窄。
- 评估价只在明确出现“评估价/评估基准日”时写入。

### 8.5 重庆产权交易所 CQUAE

状态：普通请求会被 Knownsec/创宇盾风控拦截。

处理策略：

- 首选低频浏览器渲染。
- 支持用户登录态 profile。
- 失败时写入 `raw_payloads` 和 `crawl_queue` 的失败原因。
- 不绕过验证码和风控。
- 如果列表链路不稳定，可先支持用户提供详情 URL 的定向采集。

### 8.6 天津交易集团/天津产权交易中心

状态：列表 API 可用，企业增资、产权转让正式项目、产权转让预披露项目的详情 API 已验证。

已验证：

- 列表 API：`/up/biz/project/anmuas/equity-trading/page`
- 企业增资预披露详情 API：`/transaction/biz/sa/increase/prepare/anmuas/get?viewId=...`
- 产权转让正式项目详情 API：`/transaction/biz/sa/property/right/project/anmuas/get?viewId=...`
- 产权转让预披露详情 API：`/transaction/biz/sa/property/right/prepare/anmuas/get?viewId=...`
- 企业增资、产权转让类均可获取联系人、交易机构、转让方/标的企业、地区、状态、行业等字段。

附件结构：

- 详情 API 的附件在 `viewAttachment -> attachmentTypes -> attachments` 中。
- 附件对象通常只有 `pkId` / `attachmentName`，没有现成 URL。
- 下载地址按 `https://trade.tpre.cn/attachment/api/download/{pkId}` 构造，并写入 `item_resources`。

当前缺口：

- 部分项目本身未公开价格或附件，允许对应字段为空，但必须在 `field_extractions.missing_reason` 说明原因。
- TPRE 旧测试数据大量来自列表 fallback，处置方、联系人、附件、特别告知、特有字段缺失，需要清空后按新详情 API 重跑。
- 部分挂牌价、交易底价在不同项目类型中字段名不一致，需要 AI worker 异步补提取并做证据回检。

后续处理：

1. 对 `formal-project-details`、`prepare-project-details` 按 `systemCode` 路由到已验证详情 API，禁止无故回退到 SPA 壳页面。
2. 对 `viewAttachment` 附件做结构化提取，写入 `item_resources`，并在 `field_extractions` 记录来源路径。
3. 对无价格项目保留空值，不用标题或列表噪声猜测。
4. 只有拿到详情原文或详情 JSON 后再让 AI 补字段，避免 AI 基于列表标题猜测。

### 8.7 广西产权交易所

状态：已修正 runner 中多余请求 SPA 壳页面的问题，详情优先使用 adapter 的 API 链路。

重点问题：

- 旧数据中字段缺失较多，主要来自旧链路抓到页面壳而非详情数据。
- 需要清空旧测试数据后按新链路重跑，再评估字段覆盖率。

### 8.8 北京产权交易所/预披露平台

状态：已有初步 adapter，仍需按资产类型扩展样本。

重点问题：

- 部分页面字段分散在正文、附件和表格中，需要 AI 异步补提取。
- 附件、图片、评估价必须按统一 MySQL 证据链入库。

### 8.9 其他产权平台

后续可按同一 adapter 模式扩展：

- 天津产权交易平台
- 北京产权交易所
- 其他地方产权公开门户

扩展原则不变：先拿原文，再 AI 提取，再证据验真，最后写统一 MySQL 表。

## 9. 多标的公告处理策略

很多司法拍卖公告会在一个页面里列出多个标的，当前页面只对应其中一个。

处理策略：

1. 从标题中提取锚点：房号、车位号、权证号、车牌号、债务人、企业名、金额。
2. 在公告表格中找到最匹配的行。
3. 只从匹配行提取面积、评估价、起拍价、权证号等字段。
4. 如果无法唯一匹配，字段留空并写 `missing_reason=multi_item_unmatched`。
5. 不允许从公告第一行或前几行默认取值。

该策略对京东、阿里、法院司法拍卖和产权平台都适用。

## 10. 去重策略

### 10.1 平台内去重

唯一键：

- `source_platform + source_item_id`

同平台同 ID 重复采集时更新主表，保留历史 payload 和字段提取记录。

### 10.2 跨平台疑似重复

生成 `dedup_hash`，来源字段包括：

- 标题标准化
- 资产类型
- 所在地
- 起拍价/有效价
- 权证号
- 车牌号
- 主债务人
- 债权人
- 标的企业
- 权利人

疑似重复不自动覆盖，进入 Viewer 提示或审核队列。

## 11. 定时任务平台设计

### 11.1 第一阶段：CLI 稳定

先保证以下命令可重复运行：

```powershell
python jd_scraper_v2.py crawl --per-category-limit 10
python multi_platform_runner.py crawl --platform ejy365 --limit 10
python multi_platform_runner.py crawl --platform ali --limit 10
python multi_platform_runner.py crawl --platform cquae --limit 10
```

### 11.2 第二阶段：任务平台

新增调度表：

- `crawl_jobs`：任务配置
- `crawl_job_runs`：每次运行记录
- `crawl_queue`：详情页队列
- `crawl_checkpoints`：分页断点

进程模型：

- Web：配置任务、查看状态、查看数据质量
- Scheduler：使用 APScheduler + MySQL JobStore 保存 cron 配置
- Worker：独立进程，从 MySQL 队列抢任务执行采集

Worker 要求：

- 单条失败不影响整批。
- `running` 任务超时后可回收。
- 支持断点续爬。
- 支持增量采集。
- 支持失败重试。

## 12. Viewer 改造要求

Viewer 必须直接读 MySQL。

功能要求：

- 支持数字 ID 和字符串 ID，例如 e 交易项目编号。
- 支持按 `source_platform + source_item_id` 查询。
- 展示主表字段、特有表字段、资源表、证据表、原始 payload。
- 对 AI 提取字段显示置信度和原文片段。
- 对缺失字段显示缺失原因。
- 对冲突字段显示多个候选值。
- 图片/附件从 `item_resources` 展示，不再从大 JSON 文本里找。

## 13. 当前已验证情况

截至当前版本：

- 阿里拍卖：已验证 10 条样本可以写入 MySQL。主采集阶段先保存 mtop JSON、公告正文、页面文本和图片资源，再由 `ai_enrichment_queue` 异步补齐特别告知、联系方式、房地产/债权等深层字段。
- 山东产权公开门户：已验证 10 条样本可以写入 MySQL，房地产、车辆、设备、用益物权特有字段均有写入。部分字段仍偏长，需要继续做摘要收窄。
- e 交易：已验证 10 条债权样本可以写入 MySQL，共有字段覆盖较好。Viewer 字符串 ID 查询需要继续完善，避免只能按京东数字 ID 打开详情。
- CQUAE：已确认会遇到 Knownsec/创宇盾风控，需要浏览器 profile 或详情 URL 定向采集；已补充详情页图片资源解析，能够把详情页图片写入 `item_resources` 和 AI 上下文。

## 14. 当前遗留问题

1. 旧测试数据仍可能污染 Viewer 展示，需要清空后重新采集。
2. 山东产权部分字段段落过长，需要做摘要收窄。
3. 阿里部分页面如果没有公告接口内容，需要浏览器 profile 兜底。
4. 京东多标的公告需要当前标的行定位。
5. 债权明细表为空时，需要检查附件/HTML 表格/图片 OCR 三层来源。
6. 知识产权明细需要把 HTML 表格、附件表格、OCR 结果统一写入明细表。
7. 采集耗时仍偏长，等准确率稳定后再做并发和 AI 分层优化。

## 15. 验收标准

1. 每个平台至少能稳定采集 10 条样本。
2. 每条样本必须写入 `auction_items`。
3. 能识别资产类型的样本必须写入对应特有表。
4. 图片、视频、附件必须写入 `item_resources`。
5. 每个非空字段必须在 `field_extractions` 有证据。
6. 评估价不能误提取比例、欠费、保证金、起拍价。
7. 多标的公告不能串行提取其他标的字段。
8. Viewer 能按平台和原始标的 ID 打开数据。
9. 遇到风控、登录、验证码时要记录失败原因，不静默失败。
