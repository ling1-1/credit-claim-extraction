-- 拍卖资产采集系统 MySQL V2 表结构
-- 数据库: auction_data
-- 字符集: utf8mb4
-- 说明:
-- 1. 本 DDL 面向正式 MySQL 存储，不再保留 current_price_* 字段。
-- 2. 主表只保留 amount/display 两段式字段；原文证据进入 field_extractions。
-- 3. 京东 paimai_id 等平台 ID 统一存为 source_item_id。

CREATE DATABASE IF NOT EXISTS auction_data
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE auction_data;

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS ai_model_profiles (
  profile_name VARCHAR(50) PRIMARY KEY COMMENT 'AI配置名称，如 deepseek_fast/qwen_default',
  provider VARCHAR(30) NOT NULL COMMENT '模型供应商: deepseek/qwen/openai',
  model_name VARCHAR(120) NULL COMMENT '文本提取模型名称',
  vision_model_name VARCHAR(120) NULL COMMENT '视觉/OCR模型名称',
  base_url VARCHAR(500) NULL COMMENT 'OpenAI兼容API地址',
  api_key_env_var VARCHAR(100) NULL COMMENT '推荐方式：从环境变量读取API Key',
  api_key_value TEXT NULL COMMENT '可选：直接存储API Key，本地测试可用，生产不推荐',
  timeout_seconds INT NULL COMMENT '单次请求超时秒数；0表示不设置本地超时',
  max_retries INT NULL COMMENT '失败重试次数',
  qps INT NULL COMMENT '调用限流QPS',
  max_concurrency INT NULL COMMENT '该模型配置建议最大并发数；为空则由任务参数决定',
  task_types JSON NULL COMMENT '适用任务类型数组，如 text/long_text/debt/vision/attachment；空表示通用',
  priority INT NOT NULL DEFAULT 100 COMMENT '调度优先级，数字越小越优先',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用',
  is_default TINYINT NOT NULL DEFAULT 0 COMMENT '是否默认配置',
  note VARCHAR(500) NULL COMMENT '备注',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_ai_profiles_enabled_default (enabled, is_default),
  KEY idx_ai_profiles_provider (provider),
  KEY idx_ai_profiles_routing (enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI模型配置表';

CREATE TABLE IF NOT EXISTS crawl_jobs (
  job_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '定时任务ID',
  job_name VARCHAR(200) NOT NULL COMMENT '任务名称',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台: jd/ali/ejy365/cquae',
  cron_expr VARCHAR(100) NULL COMMENT 'cron表达式',
  category_scope JSON NULL COMMENT '类目、资产类型、筛选条件配置',
  crawl_mode VARCHAR(20) NOT NULL DEFAULT 'incremental' COMMENT '采集模式: sample/full/incremental',
  page_limit INT NULL COMMENT '每次扫描页数',
  per_category_limit INT NULL COMMENT '每类最大采集数量',
  throttle_seconds DECIMAL(8,3) NULL COMMENT '请求间隔秒数',
  ai_enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用AI提取',
  attachment_parse_enabled TINYINT NOT NULL DEFAULT 0 COMMENT '是否解析附件正文',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_crawl_jobs_platform (source_platform),
  KEY idx_crawl_jobs_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时采集任务配置表';

CREATE TABLE IF NOT EXISTS crawl_job_runs (
  run_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '任务执行ID',
  job_id BIGINT NULL COMMENT '任务ID',
  batch_id VARCHAR(64) NULL COMMENT '采集批次ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
  finished_at DATETIME NULL COMMENT '结束时间',
  status VARCHAR(30) NOT NULL DEFAULT 'running' COMMENT 'running/success/partial_success/failed/cancelled',
  task_ref VARCHAR(120) NULL COMMENT '后台任务ID',
  scanned_count INT NOT NULL DEFAULT 0 COMMENT '扫描数量',
  queued_count INT NOT NULL DEFAULT 0 COMMENT '入队数量',
  success_count INT NOT NULL DEFAULT 0 COMMENT '成功数量',
  failed_count INT NOT NULL DEFAULT 0 COMMENT '失败数量',
  message TEXT NULL COMMENT '执行消息',
  summary_json JSON NULL COMMENT '执行统计JSON',
  KEY idx_job_runs_job (job_id),
  KEY idx_job_runs_batch (batch_id),
  KEY idx_job_runs_status (status),
  CONSTRAINT fk_job_runs_job FOREIGN KEY (job_id) REFERENCES crawl_jobs(job_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时任务执行记录表';

CREATE TABLE IF NOT EXISTS crawl_batches (
  batch_id VARCHAR(64) PRIMARY KEY COMMENT '采集批次唯一标识',
  run_id BIGINT NULL COMMENT '关联任务执行ID',
  source_platform VARCHAR(50) NULL COMMENT '来源平台',
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '批次开始时间',
  finished_at DATETIME NULL COMMENT '批次完成时间',
  parameters_json JSON NULL COMMENT '采集参数JSON',
  status VARCHAR(30) NOT NULL DEFAULT 'running' COMMENT 'running/success/partial_success/failed',
  message TEXT NULL COMMENT '批次消息',
  summary_json JSON NULL COMMENT '批次统计、错误和质量摘要',
  KEY idx_batches_run (run_id),
  KEY idx_batches_platform (source_platform),
  KEY idx_batches_status (status),
  CONSTRAINT fk_batches_run FOREIGN KEY (run_id) REFERENCES crawl_job_runs(run_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集批次表';

CREATE TABLE IF NOT EXISTS auction_items (
  item_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '系统内部标的ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台: jd/ali/ejy365/cquae',
  source_item_id VARCHAR(100) NOT NULL COMMENT '平台原始标的ID',
  source_url VARCHAR(1000) NOT NULL COMMENT '标的页面URL',
  source_site_name VARCHAR(200) NULL COMMENT '来源站点中文名',
  batch_id VARCHAR(64) NULL COMMENT '最近采集批次ID',
  asset_group VARCHAR(50) NOT NULL COMMENT '统一资产类型代码',
  asset_group_label VARCHAR(50) NULL COMMENT '统一资产类型中文名',
  source_category_id VARCHAR(100) NULL COMMENT '平台原始类目ID',
  source_category_name VARCHAR(200) NULL COMMENT '平台原始类目名称',
  asset_type VARCHAR(200) NULL COMMENT '标的具体类型',
  asset_location VARCHAR(1000) NULL COMMENT '标的所在地',
  project_status VARCHAR(50) NULL COMMENT '采集时项目状态',
  project_status_basis VARCHAR(200) NULL COMMENT '状态判断依据',
  auction_stage VARCHAR(100) NULL COMMENT '拍卖阶段: 一拍/二拍/变卖/招商等',
  bid_records_count INT NULL COMMENT '出价次数',
  bid_records_json JSON NULL COMMENT '出价记录快照JSON',
  data_source VARCHAR(200) NULL COMMENT '数据来源名称',
  project_name VARCHAR(1000) NULL COMMENT '项目名称',
  signup_start_time DATETIME NULL COMMENT '报名/竞价开始时间',
  signup_end_time DATETIME NULL COMMENT '报名/竞价截止时间',
  disposal_party VARCHAR(1000) NULL COMMENT '处置方: 法院、银行、AMC、破产管理人、转让方等',
  disposal_agency VARCHAR(1000) NULL COMMENT '处置机构/服务机构/店铺/拍辅机构',
  right_holder VARCHAR(1000) NULL COMMENT '权利人/所有权人/产权人，非处置方或债权人',
  start_price_amount DECIMAL(20,2) NULL COMMENT '起拍价/挂牌价/转让底价，单位元',
  start_price_display VARCHAR(200) NULL COMMENT '起拍价展示值',
  final_price_amount DECIMAL(20,2) NULL COMMENT '采集时有效价，单位元',
  final_price_display VARCHAR(200) NULL COMMENT '采集时有效价展示值',
  price_basis VARCHAR(100) NULL COMMENT '有效价依据: start_price_fallback/realtime_current_price/latest_bid_price/deal_price/formula_fallback',
  contact_info VARCHAR(2000) NULL COMMENT '联系人和联系电话',
  special_notice LONGTEXT NULL COMMENT '特别告知、重大提示、风险提示、注意事项',
  disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵、权利负担、风险瑕疵等所有资产类型共有风险信息',
  assessment_price_amount DECIMAL(20,2) NULL COMMENT '评估价格，单位元；无明确原文证据时为空',
  assessment_price_display VARCHAR(200) NULL COMMENT '评估价格展示值',
  assessment_price_basis VARCHAR(100) NULL COMMENT '评估价来源标签: 评估价/市场价/参考价/评估报告',
  assessment_date DATE NULL COMMENT '评估基准日或评估日期',
  dedup_hash CHAR(64) NULL COMMENT '跨平台去重指纹',
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最近发现时间',
  last_crawled_at DATETIME NULL COMMENT '最近详情采集时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_source_item (source_platform, source_item_id),
  KEY idx_items_platform (source_platform),
  KEY idx_items_asset_group (asset_group),
  KEY idx_items_status (project_status),
  KEY idx_items_stage (auction_stage),
  KEY idx_items_signup_start (signup_start_time),
  KEY idx_items_signup_end (signup_end_time),
  KEY idx_items_dedup_hash (dedup_hash),
  KEY idx_items_batch (batch_id),
  CONSTRAINT fk_items_batch FOREIGN KEY (batch_id) REFERENCES crawl_batches(batch_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='拍卖资产共有字段主表';

CREATE TABLE IF NOT EXISTS crawl_queue (
  queue_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '队列ID',
  batch_id VARCHAR(64) NULL COMMENT '批次ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  source_item_id VARCHAR(100) NOT NULL COMMENT '平台原始标的ID',
  source_url VARCHAR(1000) NOT NULL COMMENT '标的URL',
  item_id BIGINT NULL COMMENT '已入库标的ID',
  queue_status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/pending_ai/success/failed/updated/unchanged/skipped',
  priority INT NOT NULL DEFAULT 100 COMMENT '优先级，数字越小越优先',
  retry_count INT NOT NULL DEFAULT 0 COMMENT '重试次数',
  max_retries INT NOT NULL DEFAULT 3 COMMENT '最大重试次数',
  locked_by VARCHAR(100) NULL COMMENT 'Worker标识',
  locked_at DATETIME NULL COMMENT '锁定时间',
  last_error TEXT NULL COMMENT '最后错误',
  discovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发现时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_queue_source (source_platform, source_item_id, batch_id),
  KEY idx_queue_status (queue_status),
  KEY idx_queue_batch (batch_id),
  KEY idx_queue_item (item_id),
  CONSTRAINT fk_queue_batch FOREIGN KEY (batch_id) REFERENCES crawl_batches(batch_id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT fk_queue_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集队列表';

CREATE TABLE IF NOT EXISTS crawl_queue_events (
  event_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '队列事件ID',
  queue_id BIGINT NULL COMMENT '队列ID',
  batch_id VARCHAR(64) NULL COMMENT '批次ID',
  item_id BIGINT NULL COMMENT '标的ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  source_item_id VARCHAR(120) NULL COMMENT '平台原始标的ID',
  from_status VARCHAR(30) NULL COMMENT '变更前状态',
  to_status VARCHAR(30) NOT NULL COMMENT '变更后状态',
  event_type VARCHAR(50) NOT NULL DEFAULT 'status_change' COMMENT '事件类型',
  message TEXT NULL COMMENT '事件消息',
  error_detail LONGTEXT NULL COMMENT '错误详情',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  KEY idx_crawl_queue_events_queue (queue_id),
  KEY idx_crawl_queue_events_batch (batch_id),
  KEY idx_crawl_queue_events_source (source_platform, source_item_id),
  CONSTRAINT fk_queue_events_queue FOREIGN KEY (queue_id) REFERENCES crawl_queue(queue_id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT fk_queue_events_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集队列事件表';

CREATE TABLE IF NOT EXISTS dead_letter_queue (
  dead_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '死信ID',
  queue_id BIGINT NULL COMMENT '队列ID',
  task_type VARCHAR(50) NOT NULL DEFAULT 'crawl' COMMENT '任务类型 crawl/ai/ocr/attachment',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  source_item_id VARCHAR(120) NULL COMMENT '平台原始标的ID',
  source_url VARCHAR(1000) NULL COMMENT '来源URL',
  item_id BIGINT NULL COMMENT '标的ID',
  batch_id VARCHAR(64) NULL COMMENT '批次ID',
  failure_stage VARCHAR(80) NULL COMMENT '失败阶段',
  retry_count INT NOT NULL DEFAULT 0 COMMENT '失败时重试次数',
  error_message LONGTEXT NULL COMMENT '错误消息',
  payload_json JSON NULL COMMENT '失败上下文',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  resolved_at DATETIME NULL COMMENT '处理时间',
  resolved_status VARCHAR(30) NULL COMMENT '处理状态 ignored/requeued/fixed',
  KEY idx_dead_letter_status (resolved_status),
  KEY idx_dead_letter_source (source_platform, source_item_id),
  KEY idx_dead_letter_batch (batch_id),
  CONSTRAINT fk_dead_letter_queue FOREIGN KEY (queue_id) REFERENCES crawl_queue(queue_id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT fk_dead_letter_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='死信队列表';

CREATE TABLE IF NOT EXISTS raw_payloads (
  payload_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '原始数据ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  batch_id VARCHAR(64) NULL COMMENT '采集批次ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  payload_type VARCHAR(80) NOT NULL COMMENT '原始数据类型',
  source_url VARCHAR(1000) NULL COMMENT '来源URL',
  source_tab VARCHAR(100) NULL COMMENT '页面标签页: 竞买公告/竞买须知/标的物详情等',
  payload_text LONGTEXT NULL COMMENT 'HTML或纯文本原文',
  payload_json JSON NULL COMMENT 'JSON原文',
  payload_hash CHAR(64) NULL COMMENT '原文哈希',
  fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '获取时间',
  KEY idx_payload_item (item_id),
  KEY idx_payload_batch (batch_id),
  KEY idx_payload_type (payload_type),
  KEY idx_payload_hash (payload_hash),
  CONSTRAINT fk_payload_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_payload_batch FOREIGN KEY (batch_id) REFERENCES crawl_batches(batch_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='原始数据归档表';

CREATE TABLE IF NOT EXISTS field_catalog (
  field_namespace VARCHAR(50) NOT NULL COMMENT '字段命名空间: common/special/detail/system',
  asset_group VARCHAR(50) NOT NULL DEFAULT 'ALL' COMMENT '资产类型代码，ALL表示共有字段',
  field_key VARCHAR(100) NOT NULL COMMENT '字段英文键',
  field_label VARCHAR(200) NOT NULL COMMENT '字段中文名',
  field_comment TEXT NULL COMMENT '字段中文说明',
  data_type VARCHAR(50) NULL COMMENT '业务数据类型: text/money/date/datetime/area/json',
  required_for_display TINYINT NOT NULL DEFAULT 1 COMMENT '是否在Viewer默认展示',
  aliases_json JSON NULL COMMENT '字段同义词JSON',
  source_priority_json JSON NULL COMMENT '来源优先级JSON',
  export_order INT NOT NULL DEFAULT 1000 COMMENT '展示/导出顺序',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (field_namespace, asset_group, field_key),
  KEY idx_catalog_order (field_namespace, asset_group, export_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='字段字典和中文备注表';

CREATE TABLE IF NOT EXISTS field_extractions (
  extraction_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '字段提取记录ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  field_namespace VARCHAR(50) NOT NULL COMMENT '字段命名空间: common/special/detail',
  asset_group VARCHAR(50) NULL COMMENT '资产类型',
  field_key VARCHAR(100) NOT NULL COMMENT '字段英文键',
  field_label VARCHAR(200) NULL COMMENT '字段中文名',
  display_value LONGTEXT NULL COMMENT '展示值',
  normalized_text LONGTEXT NULL COMMENT '标准化文本值',
  numeric_value DECIMAL(20,6) NULL COMMENT '标准化数值，金额统一为元，面积统一为平方米',
  date_value DATE NULL COMMENT '标准化日期',
  datetime_value DATETIME NULL COMMENT '标准化时间',
  value_unit VARCHAR(50) NULL COMMENT '单位，如元、平方米',
  method VARCHAR(80) NOT NULL COMMENT '提取方式: api/html_regex/ai/derived/validation',
  source_payload_id BIGINT NULL COMMENT '来源原始数据ID',
  source_payload_type VARCHAR(80) NULL COMMENT '来源原始数据类型',
  source_tab VARCHAR(100) NULL COMMENT '来源页面标签页',
  source_path VARCHAR(500) NULL COMMENT 'JSON路径或HTML定位说明',
  source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
  confidence DECIMAL(5,4) NULL COMMENT '置信度，0到1',
  status VARCHAR(50) NOT NULL DEFAULT 'extracted' COMMENT 'extracted/missing/conflict/rejected/needs_review',
  is_selected TINYINT NOT NULL DEFAULT 0 COMMENT '是否为最终采用值',
  missing_reason VARCHAR(1000) NULL COMMENT '缺失、拒绝或冲突原因',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  KEY idx_fx_item_field (item_id, field_namespace, field_key),
  KEY idx_fx_selected (item_id, is_selected),
  KEY idx_fx_status (status),
  KEY idx_fx_payload (source_payload_id),
  CONSTRAINT fk_fx_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_fx_payload FOREIGN KEY (source_payload_id) REFERENCES raw_payloads(payload_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='字段提取证据表';

CREATE TABLE IF NOT EXISTS item_resources (
  resource_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '资源ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  resource_type VARCHAR(30) NOT NULL COMMENT 'attachment/image/video',
  resource_role VARCHAR(80) NULL COMMENT '资源角色: site_image/vehicle_image/subject_image/announcement_file/assessment_report/asset_list等',
  resource_name VARCHAR(1000) NULL COMMENT '文件名或图片说明',
  resource_url VARCHAR(2000) NOT NULL COMMENT '资源URL',
  resource_format VARCHAR(50) NULL COMMENT '文件格式',
  resource_size_bytes BIGINT NULL COMMENT '文件大小',
  source_section VARCHAR(100) NULL COMMENT '来源区块或标签页',
  source_payload_id BIGINT NULL COMMENT '来源原始数据ID',
  url_hash CHAR(64) NOT NULL COMMENT 'URL哈希',
  content_hash CHAR(64) NULL COMMENT '内容哈希，未下载时为空',
  is_downloaded TINYINT NOT NULL DEFAULT 0 COMMENT '是否已下载',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  UNIQUE KEY uk_resource_url (item_id, resource_type, url_hash),
  KEY idx_resource_item (item_id),
  KEY idx_resource_type (resource_type),
  KEY idx_resource_role (resource_role),
  KEY idx_resource_payload (source_payload_id),
  CONSTRAINT fk_resource_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_resource_payload FOREIGN KEY (source_payload_id) REFERENCES raw_payloads(payload_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='附件、图片、视频资源表';

CREATE TABLE IF NOT EXISTS ocr_retry_queue (
  ocr_task_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT 'OCR/视觉识别任务ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  source_platform VARCHAR(50) NOT NULL DEFAULT 'jd' COMMENT '来源平台',
  source_item_id VARCHAR(100) NOT NULL COMMENT '平台原始标的ID',
  task_type VARCHAR(80) NOT NULL COMMENT '任务类型，如 ip_image_details',
  resource_urls_json JSON NOT NULL COMMENT '待识别图片URL列表',
  queue_status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/parsing/paused/success/failed/skipped',
  priority INT NOT NULL DEFAULT 100 COMMENT '优先级，数字越小越优先',
  retry_count INT NOT NULL DEFAULT 0 COMMENT '重试次数',
  max_retries INT NOT NULL DEFAULT 3 COMMENT '最大重试次数',
  locked_by VARCHAR(100) NULL COMMENT 'Worker标识',
  locked_at DATETIME NULL COMMENT '锁定时间',
  running_profile_name VARCHAR(100) NULL COMMENT '实际处理AI配置名',
  running_provider VARCHAR(80) NULL COMMENT '实际处理AI供应商',
  running_model_name VARCHAR(200) NULL COMMENT '实际处理AI模型',
  last_error TEXT NULL COMMENT '最后错误或入队原因',
  result_json JSON NULL COMMENT '识别结果JSON，成功后写入',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_ocr_item_task (source_platform, source_item_id, task_type),
  KEY idx_ocr_item (item_id),
  KEY idx_ocr_status (queue_status),
  CONSTRAINT fk_ocr_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='异步OCR/视觉识别重试队列表';

CREATE TABLE IF NOT EXISTS ai_enrichment_queue (
  ai_task_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT 'AI字段补提取任务ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  source_platform VARCHAR(50) NOT NULL DEFAULT 'jd' COMMENT '来源平台',
  source_item_id VARCHAR(100) NOT NULL COMMENT '平台原始标的ID',
  asset_group VARCHAR(50) NOT NULL COMMENT '统一资产类型代码',
  task_type VARCHAR(80) NOT NULL DEFAULT 'field_enrichment' COMMENT '任务类型，如 field_enrichment',
  context_json JSON NOT NULL COMMENT 'AI提取上下文，包含已抓取的公告、须知、详情、表格和图片URL',
  field_keys_json JSON NULL COMMENT '本次计划补提取字段列表，空表示按资产类型全量补提取',
  queue_status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/parsing/paused/success/failed/skipped',
  priority INT NOT NULL DEFAULT 100 COMMENT '优先级，数字越小越优先',
  retry_count INT NOT NULL DEFAULT 0 COMMENT '重试次数',
  max_retries INT NOT NULL DEFAULT 3 COMMENT '最大重试次数',
  locked_by VARCHAR(100) NULL COMMENT 'Worker标识',
  locked_at DATETIME NULL COMMENT '锁定时间',
  running_profile_name VARCHAR(100) NULL COMMENT '实际处理AI配置名',
  running_provider VARCHAR(80) NULL COMMENT '实际处理AI供应商',
  running_model_name VARCHAR(200) NULL COMMENT '实际处理AI模型',
  last_error TEXT NULL COMMENT '最后错误或入队原因',
  result_json JSON NULL COMMENT 'AI补提取结果JSON，成功后写入',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_ai_item_task (source_platform, source_item_id, task_type),
  KEY idx_ai_item (item_id),
  KEY idx_ai_status (queue_status),
  KEY idx_ai_priority (queue_status, priority, ai_task_id),
  CONSTRAINT fk_ai_enrichment_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='异步AI字段补提取队列表';

CREATE TABLE IF NOT EXISTS asset_real_estate (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  right_certificate_no VARCHAR(500) NULL COMMENT '权证编号',
  building_area_sqm DECIMAL(20,6) NULL COMMENT '建筑面积，平方米',
  building_area_display VARCHAR(200) NULL COMMENT '建筑面积展示值',
  property_use VARCHAR(500) NULL COMMENT '房产用途',
  use_term VARCHAR(500) NULL COMMENT '使用年限/使用期限',
  property_location VARCHAR(1000) NULL COMMENT '房产位置',
  property_structure VARCHAR(500) NULL COMMENT '房产结构',
  property_status VARCHAR(1000) NULL COMMENT '房产状态',
  property_type VARCHAR(500) NULL COMMENT '房产类型',
  asset_highlights LONGTEXT NULL COMMENT '资产亮点',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_real_estate_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='房地产特有字段表';

CREATE TABLE IF NOT EXISTS asset_land (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  right_certificate_no VARCHAR(500) NULL COMMENT '权证编号',
  land_area_sqm DECIMAL(20,6) NULL COMMENT '土地面积，平方米',
  land_area_display VARCHAR(200) NULL COMMENT '土地面积展示值',
  land_use VARCHAR(500) NULL COMMENT '土地用途',
  use_term VARCHAR(500) NULL COMMENT '使用期限',
  land_location VARCHAR(1000) NULL COMMENT '土地位置',
  land_status VARCHAR(1000) NULL COMMENT '土地状态',
  land_type VARCHAR(500) NULL COMMENT '土地类型',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_land_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='土地特有字段表';

CREATE TABLE IF NOT EXISTS asset_equipment (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  storage_location VARCHAR(1000) NULL COMMENT '存放位置',
  equipment_status VARCHAR(1000) NULL COMMENT '设备状态',
  equipment_type VARCHAR(500) NULL COMMENT '设备类型',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_equipment_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='设备特有字段表';

CREATE TABLE IF NOT EXISTS asset_vehicle (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  storage_location VARCHAR(1000) NULL COMMENT '存放位置',
  vehicle_brand_model VARCHAR(1000) NULL COMMENT '车型品牌',
  vehicle_usage LONGTEXT NULL COMMENT '车辆使用情况',
  plate_number VARCHAR(100) NULL COMMENT '车牌号',
  vehicle_configuration LONGTEXT NULL COMMENT '车辆配置',
  vehicle_status LONGTEXT NULL COMMENT '车辆状态',
  vehicle_type VARCHAR(500) NULL COMMENT '车辆类型',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_vehicle_plate (plate_number),
  CONSTRAINT fk_vehicle_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='车辆特有字段表';

CREATE TABLE IF NOT EXISTS asset_debt (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  main_debtor_name VARCHAR(1000) NULL COMMENT '主债务人名称',
  debtor_names LONGTEXT NULL COMMENT '债务人名称汇总',
  creditor VARCHAR(1000) NULL COMMENT '债权人',
  principal_balance_amount DECIMAL(20,2) NULL COMMENT '本金余额，单位元',
  principal_balance_display VARCHAR(200) NULL COMMENT '本金余额展示值',
  interest_balance_amount DECIMAL(20,2) NULL COMMENT '利息余额，单位元',
  interest_balance_display VARCHAR(200) NULL COMMENT '利息余额展示值',
  claim_total_amount DECIMAL(20,2) NULL COMMENT '债权总额，单位元',
  claim_total_display VARCHAR(200) NULL COMMENT '债权总额展示值',
  benchmark_date DATE NULL COMMENT '基准日',
  guarantee_method VARCHAR(1000) NULL COMMENT '担保方式',
  guarantor LONGTEXT NULL COMMENT '保证人',
  collateral LONGTEXT NULL COMMENT '抵质押物',
  litigation_status LONGTEXT NULL COMMENT '诉讼状态',
  household_count INT NULL COMMENT '户数',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_debt_main_debtor (main_debtor_name(100)),
  KEY idx_debt_creditor (creditor(100)),
  CONSTRAINT fk_debt_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权汇总字段表';

CREATE TABLE IF NOT EXISTS asset_debt_details (
  debt_detail_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '债权明细ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  detail_index INT NOT NULL COMMENT '明细顺序',
  sequence_no VARCHAR(100) NULL COMMENT '原表序号',
  debtor_name VARCHAR(1000) NULL COMMENT '债务人',
  principal_balance_amount DECIMAL(20,2) NULL COMMENT '本金余额，单位元',
  interest_balance_amount DECIMAL(20,2) NULL COMMENT '利息余额，单位元',
  claim_total_amount DECIMAL(20,2) NULL COMMENT '债权总额，单位元',
  benchmark_date DATE NULL COMMENT '基准日',
  guarantor LONGTEXT NULL COMMENT '保证人',
  collateral LONGTEXT NULL COMMENT '抵质押物',
  litigation_status LONGTEXT NULL COMMENT '诉讼状态',
  source_payload_id BIGINT NULL COMMENT '来源原始数据ID',
  source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_debt_detail_index (item_id, detail_index),
  KEY idx_debt_detail_debtor (debtor_name(100)),
  KEY idx_debt_detail_payload (source_payload_id),
  CONSTRAINT fk_debt_detail_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_debt_detail_payload FOREIGN KEY (source_payload_id) REFERENCES raw_payloads(payload_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权逐户明细表';

CREATE TABLE IF NOT EXISTS asset_equity (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  transferor VARCHAR(1000) NULL COMMENT '转让方',
  target_company VARCHAR(1000) NULL COMMENT '标的企业',
  equity_ratio VARCHAR(200) NULL COMMENT '股权占比',
  company_nature VARCHAR(500) NULL COMMENT '企业性质',
  company_industry VARCHAR(500) NULL COMMENT '企业行业',
  business_scope LONGTEXT NULL COMMENT '经营范围',
  ownership_structure LONGTEXT NULL COMMENT '股权结构',
  financial_metrics LONGTEXT NULL COMMENT '财务指标',
  asset_valuation LONGTEXT NULL COMMENT '资产评估',
  disclosure_items LONGTEXT NULL COMMENT '公示事项',
  attached_assets LONGTEXT NULL COMMENT '附带标的',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_equity_company (target_company(100)),
  CONSTRAINT fk_equity_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='股权特有字段表';

CREATE TABLE IF NOT EXISTS asset_ip (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  subject_name VARCHAR(1000) NULL COMMENT '标的名称',
  ip_count INT NULL COMMENT '知识产权数量',
  certificate_no VARCHAR(1000) NULL COMMENT '标的证号汇总',
  ip_type VARCHAR(500) NULL COMMENT '知产类型',
  specific_category VARCHAR(500) NULL COMMENT '具体类别',
  subject_intro LONGTEXT NULL COMMENT '标的简介',
  right_term VARCHAR(500) NULL COMMENT '权利期限',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_ip_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识产权汇总字段表';

CREATE TABLE IF NOT EXISTS asset_ip_details (
  ip_detail_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '知识产权明细ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  detail_index INT NOT NULL COMMENT '明细顺序',
  sequence_no VARCHAR(100) NULL COMMENT '原表序号',
  ip_name VARCHAR(1000) NULL COMMENT '软件名称、专利名称、作品名称等',
  certificate_no VARCHAR(500) NULL COMMENT '证书号、登记号、申请号',
  registration_no VARCHAR(500) NULL COMMENT '登记号',
  acquire_method VARCHAR(500) NULL COMMENT '取得方式',
  application_date DATE NULL COMMENT '申请日',
  approval_date DATE NULL COMMENT '登记批准日',
  ip_type VARCHAR(500) NULL COMMENT '知产类型',
  patent_type VARCHAR(500) NULL COMMENT '专利类型',
  right_holder VARCHAR(1000) NULL COMMENT '权利人',
  right_status VARCHAR(1000) NULL COMMENT '权利状态',
  source_payload_id BIGINT NULL COMMENT '来源原始数据ID',
  source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_ip_detail_index (item_id, detail_index),
  KEY idx_ip_detail_name (ip_name(100)),
  KEY idx_ip_detail_certificate (certificate_no(100)),
  KEY idx_ip_detail_payload (source_payload_id),
  CONSTRAINT fk_ip_detail_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_ip_detail_payload FOREIGN KEY (source_payload_id) REFERENCES raw_payloads(payload_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识产权逐项明细表';

CREATE TABLE IF NOT EXISTS asset_goods (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  goods_category VARCHAR(500) NULL COMMENT '物资种类',
  goods_name VARCHAR(1000) NULL COMMENT '物资名称',
  goods_location VARCHAR(1000) NULL COMMENT '物资所在位置',
  goods_details LONGTEXT NULL COMMENT '物资详情',
  right_burden LONGTEXT NULL COMMENT '权利负担',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_goods_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='物资产品特有字段表';

CREATE TABLE IF NOT EXISTS asset_usufruct (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  right_category VARCHAR(500) NULL COMMENT '权益种类',
  subject_name VARCHAR(1000) NULL COMMENT '标的名称',
  subject_location VARCHAR(1000) NULL COMMENT '标的所在位置',
  subject_details LONGTEXT NULL COMMENT '标的物详情',
  valid_period VARCHAR(500) NULL COMMENT '有效期',
  original_right_holder VARCHAR(1000) NULL COMMENT '原权利人',
  right_burden LONGTEXT NULL COMMENT '权利负担',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_usufruct_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用益物权特有字段表';

CREATE TABLE IF NOT EXISTS asset_other (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  raw_detail_text LONGTEXT NULL COMMENT '原始详情文本',
  raw_table_pairs_json JSON NULL COMMENT '原始表格键值对',
  extracted_summary LONGTEXT NULL COMMENT 'AI提取摘要',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  CONSTRAINT fk_other_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='其他类型兜底字段表';

CREATE TABLE IF NOT EXISTS asset_dedup_index (
  item_id BIGINT PRIMARY KEY COMMENT '标的ID',
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  source_item_id VARCHAR(100) NOT NULL COMMENT '平台原始标的ID',
  dedup_hash CHAR(64) NOT NULL COMMENT '跨平台去重指纹',
  asset_group VARCHAR(50) NULL COMMENT '资产类型',
  project_name VARCHAR(1000) NULL COMMENT '项目名称',
  asset_location VARCHAR(1000) NULL COMMENT '所在地',
  identity_basis_json JSON NULL COMMENT '生成去重指纹的字段原值和标准化值',
  canonical_item_id BIGINT NULL COMMENT '疑似重复时建议主记录ID',
  duplicate_status VARCHAR(30) NOT NULL DEFAULT 'unique' COMMENT 'unique/suspected/confirmed/ignored',
  duplicate_confidence DECIMAL(5,4) NULL COMMENT '重复置信度',
  reviewed_at DATETIME NULL COMMENT '审核时间',
  reviewer VARCHAR(100) NULL COMMENT '审核人',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_dedup_source (source_platform, source_item_id),
  KEY idx_dedup_hash (dedup_hash),
  KEY idx_dedup_status (duplicate_status),
  KEY idx_dedup_canonical (canonical_item_id),
  CONSTRAINT fk_dedup_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_dedup_canonical FOREIGN KEY (canonical_item_id) REFERENCES auction_items(item_id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='跨平台去重索引表';

CREATE TABLE IF NOT EXISTS review_queue (
  review_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '审核ID',
  item_id BIGINT NOT NULL COMMENT '标的ID',
  field_namespace VARCHAR(50) NULL COMMENT '字段命名空间',
  field_key VARCHAR(100) NULL COMMENT '字段英文键',
  field_label VARCHAR(200) NULL COMMENT '字段中文名',
  issue_type VARCHAR(80) NOT NULL COMMENT '问题类型: missing/conflict/low_confidence/invalid_value/duplicate',
  issue_detail LONGTEXT NULL COMMENT '问题详情',
  candidate_values_json JSON NULL COMMENT '候选值JSON',
  final_value LONGTEXT NULL COMMENT '人工确认值',
  status VARCHAR(30) NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected/modified/ignored',
  reviewer VARCHAR(100) NULL COMMENT '审核人',
  reviewed_at DATETIME NULL COMMENT '审核时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  KEY idx_review_item (item_id),
  KEY idx_review_status (status),
  KEY idx_review_issue (issue_type),
  CONSTRAINT fk_review_item FOREIGN KEY (item_id) REFERENCES auction_items(item_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人工审核队列表';

CREATE TABLE IF NOT EXISTS data_quality_reports (
  report_id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '质量报告ID',
  batch_id VARCHAR(64) NOT NULL COMMENT '批次ID',
  source_platform VARCHAR(50) NULL COMMENT '来源平台',
  item_count INT NOT NULL DEFAULT 0 COMMENT '标的数量',
  total_fields INT NOT NULL DEFAULT 0 COMMENT '字段总数',
  extracted_fields INT NOT NULL DEFAULT 0 COMMENT '已提取字段数',
  missing_fields INT NOT NULL DEFAULT 0 COMMENT '缺失字段数',
  conflict_fields INT NOT NULL DEFAULT 0 COMMENT '冲突字段数',
  review_required_count INT NOT NULL DEFAULT 0 COMMENT '需审核数量',
  quality_score DECIMAL(8,4) NULL COMMENT '质量评分',
  report_json JSON NULL COMMENT '详细报告JSON',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  UNIQUE KEY uk_quality_batch (batch_id),
  KEY idx_quality_platform (source_platform),
  CONSTRAINT fk_quality_batch FOREIGN KEY (batch_id) REFERENCES crawl_batches(batch_id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='批次数据质量报告表';

CREATE TABLE IF NOT EXISTS crawl_list_fingerprints (
  source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
  source_item_id VARCHAR(128) NOT NULL COMMENT '平台原始标的ID',
  fingerprint VARCHAR(512) NOT NULL COMMENT '列表级指纹(编号/标题/价格/日期/状态等摘要)',
  updated_at DATETIME NOT NULL COMMENT '指纹更新时间',
  PRIMARY KEY (source_platform, source_item_id),
  KEY idx_fp_platform (source_platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='列表指纹表(支撑增量采集: 仅采集新增/变更标的)';

CREATE TABLE IF NOT EXISTS crawl_checkpoints (
  checkpoint_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_platform VARCHAR(50) NOT NULL COMMENT '平台',
  category_key VARCHAR(80) NOT NULL DEFAULT 'default' COMMENT '分类标识',
  current_page INT NOT NULL DEFAULT 1 COMMENT '当前页码',
  total_items_seen INT NOT NULL DEFAULT 0 COMMENT '已处理标的数',
  last_item_id VARCHAR(200) NULL COMMENT '最后处理的标的ID',
  batch_id VARCHAR(100) NULL COMMENT '批次ID',
  crawl_mode VARCHAR(30) NOT NULL DEFAULT 'full' COMMENT '采集模式',
  checkpoint_status VARCHAR(30) NOT NULL DEFAULT 'running' COMMENT '断点状态',
  message TEXT NULL COMMENT '断点说明或错误信息',
  started_at DATETIME NULL COMMENT '首次开始时间',
  completed_at DATETIME NULL COMMENT '完成时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  UNIQUE KEY uk_platform_category (source_platform, category_key),
  KEY idx_status (checkpoint_status),
  KEY idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集断点表(支撑断点续传)';
