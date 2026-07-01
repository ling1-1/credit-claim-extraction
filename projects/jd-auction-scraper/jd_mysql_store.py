from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from jd_scraper_v2 import (
    ASSET_GROUP_LABELS,
    ASSET_TABLES,
    COMMON_FIELDS,
    SPECIAL_FIELDS,
    SPECIAL_NORMALIZED_COLUMNS,
    date_to_db,
    datetime_to_db,
    is_valid_assessment_price_time,
)


@dataclass(frozen=True)
class MySQLConfig:
    host: str = field(default_factory=lambda: os.getenv("JD_MYSQL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("JD_MYSQL_PORT", "3306")))
    user: str = field(default_factory=lambda: os.getenv("JD_MYSQL_USER", ""))
    password: str = field(default_factory=lambda: os.getenv("JD_MYSQL_PASSWORD", ""))
    database: str = field(default_factory=lambda: os.getenv("JD_MYSQL_DATABASE", "auction_data"))


def mysql_connection(config: MySQLConfig, *, database: bool = True):
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database if database else None,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )


def qmarks(count: int) -> str:
    return ", ".join(["%s"] * count)


def ensure_mysql_database(config: MySQLConfig) -> None:
    with mysql_connection(config, database=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()


MYSQL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS crawl_batches (
      batch_id VARCHAR(64) PRIMARY KEY COMMENT '采集批次唯一标识',
      started_at DATETIME NULL COMMENT '批次开始时间',
      finished_at DATETIME NULL COMMENT '批次完成时间',
      parameters_json LONGTEXT NULL COMMENT '采集参数 JSON',
      status VARCHAR(30) NULL COMMENT '批次状态',
      message TEXT NULL COMMENT '批次消息',
      summary_json LONGTEXT NULL COMMENT '批次统计、错误和参数汇总 JSON'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='采集批次管理表'
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_payloads (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '京东拍卖标的 ID',
      batch_id VARCHAR(64) NULL COMMENT '采集批次 ID',
      source_url VARCHAR(500) NULL COMMENT '标的页面 URL',
      list_json LONGTEXT NULL COMMENT '列表接口原始 JSON',
      detail_json LONGTEXT NULL COMMENT '详情接口原始 JSON',
      product_basic_json LONGTEXT NULL COMMENT '商品基础信息接口原始 JSON',
      realtime_json LONGTEXT NULL COMMENT '实时价格接口原始 JSON',
      description_html LONGTEXT NULL COMMENT '标的物详情 HTML 原文',
      notice_html LONGTEXT NULL COMMENT '竞买须知 HTML 原文',
      announcement_html LONGTEXT NULL COMMENT '竞买公告 HTML 原文',
      attachments_json LONGTEXT NULL COMMENT '附件和图片视频 JSON',
      attachment_texts LONGTEXT NULL COMMENT '附件解析文本 JSON',
      vendor_json LONGTEXT NULL COMMENT '处置方接口原始 JSON',
      crawled_at DATETIME NULL COMMENT '采集时间',
      KEY idx_raw_batch (batch_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='原始数据存档表'
    """,
    """
    CREATE TABLE IF NOT EXISTS field_catalog (
      field_namespace VARCHAR(64) NOT NULL COMMENT '字段命名空间',
      asset_group VARCHAR(32) NULL COMMENT '资产类型代码',
      field_key VARCHAR(80) NOT NULL COMMENT '字段英文键',
      field_label VARCHAR(120) NULL COMMENT '字段中文名',
      field_scope VARCHAR(30) NULL COMMENT '字段范围',
      data_type VARCHAR(50) NULL COMMENT '建议数据库类型',
      required_for_display TINYINT NULL COMMENT '是否必须展示',
      aliases_json LONGTEXT NULL COMMENT '同义字段 JSON',
      source_priority_json LONGTEXT NULL COMMENT '来源优先级 JSON',
      export_order INT NULL COMMENT '展示顺序',
      PRIMARY KEY (field_namespace, field_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='字段元数据表'
    """,
    """
    CREATE TABLE IF NOT EXISTS field_extractions (
      paimai_id VARCHAR(32) NOT NULL COMMENT '标的 ID',
      field_namespace VARCHAR(64) NOT NULL COMMENT '字段命名空间',
      asset_group VARCHAR(32) NULL COMMENT '资产类型',
      field_key VARCHAR(80) NOT NULL COMMENT '字段英文键',
      field_label VARCHAR(120) NULL COMMENT '字段中文名',
      raw_value LONGTEXT NULL COMMENT '原始提取值',
      normalized_value LONGTEXT NULL COMMENT '用于展示/导出的标准化值',
      status VARCHAR(30) NULL COMMENT 'extracted/missing_on_page/conflict 等',
      method VARCHAR(50) NULL COMMENT 'api/html_text_regex/ai 等提取方法',
      confidence DECIMAL(5,4) NULL COMMENT '置信度',
      source_payload_type VARCHAR(50) NULL COMMENT '来源数据类型',
      source_path VARCHAR(300) NULL COMMENT '来源路径',
      source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
      missing_reason VARCHAR(500) NULL COMMENT '缺失原因',
      extracted_at DATETIME NULL COMMENT '提取时间',
      PRIMARY KEY (paimai_id, field_namespace, field_key),
      KEY idx_fx_field (field_namespace, field_key),
      KEY idx_fx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='字段提取证据表'
    """,
    """
    CREATE TABLE IF NOT EXISTS auction_items_common (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '京东拍卖标的 ID',
      batch_id VARCHAR(64) NULL COMMENT '采集批次 ID',
      source_url VARCHAR(500) NULL COMMENT '标的页面 URL',
      source_platform VARCHAR(50) NOT NULL DEFAULT 'jd' COMMENT '来源平台',
      source_item_id VARCHAR(80) NULL COMMENT '来源平台标的 ID',
      asset_group VARCHAR(32) NOT NULL COMMENT '内部资产类型代码',
      asset_group_label VARCHAR(50) NULL COMMENT '资产类型中文名',
      jd_category_id VARCHAR(30) NULL COMMENT '京东类目 ID',
      jd_category_name VARCHAR(100) NULL COMMENT '京东类目名称',
      asset_type VARCHAR(80) NULL COMMENT '标的类型',
      asset_location VARCHAR(500) NULL COMMENT '标的所在地',
      project_status VARCHAR(50) NULL COMMENT '项目状态',
      auction_stage VARCHAR(50) NULL COMMENT '拍卖阶段',
      bid_records_json LONGTEXT NULL COMMENT '出价记录 JSON',
      data_source VARCHAR(100) NULL COMMENT '数据来源',
      project_name VARCHAR(500) NULL COMMENT '项目名称',
      signup_start_time DATETIME NULL COMMENT '报名/竞价开始时间',
      signup_end_time DATETIME NULL COMMENT '报名/竞价截止时间',
      disposal_party VARCHAR(500) NULL COMMENT '处置方',
      start_price_raw VARCHAR(100) NULL COMMENT '起拍价原始展示值',
      final_price_raw VARCHAR(100) NULL COMMENT '最终价/当前价原始展示值',
      contact_info VARCHAR(1000) NULL COMMENT '联系方式',
      special_notice LONGTEXT NULL COMMENT '特别告知/重大提示',
      assessment_price_time VARCHAR(500) NULL COMMENT '评估价格及时间原始展示值',
      attachments_json LONGTEXT NULL COMMENT '附件和图片视频 JSON',
      common_fields_json LONGTEXT NULL COMMENT '共有字段完整快照 JSON',
      signup_start_time_norm DATETIME NULL COMMENT '开始时间标准化值',
      signup_end_time_norm DATETIME NULL COMMENT '截止时间标准化值',
      start_price_amount DECIMAL(18,2) NULL COMMENT '起拍价标准化金额，单位元',
      final_price_amount DECIMAL(18,2) NULL COMMENT '最终价/当前价标准化金额，单位元',
      assessment_price_amount DECIMAL(18,2) NULL COMMENT '评估价标准化金额，单位元',
      assessment_amount DECIMAL(18,2) NULL COMMENT '评估价标准化金额，单位元',
      assessment_date DATE NULL COMMENT '评估基准日/评估日期',
      dedup_hash VARCHAR(64) NULL COMMENT '跨平台去重指纹',
      updated_at DATETIME NULL COMMENT '更新时间',
      UNIQUE KEY uk_source_item (source_platform, source_item_id),
      KEY idx_common_group (asset_group),
      KEY idx_common_status (project_status),
      KEY idx_common_dedup (dedup_hash)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='共有字段主表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_land (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      right_certificate_no VARCHAR(200) NULL COMMENT '权证编号',
      land_area VARCHAR(200) NULL COMMENT '土地面积原始值',
      land_area_sqm DECIMAL(18,2) NULL COMMENT '土地面积标准化值，单位平方米',
      land_use VARCHAR(300) NULL COMMENT '土地用途',
      use_term VARCHAR(300) NULL COMMENT '使用期限',
      land_location VARCHAR(500) NULL COMMENT '土地位置',
      right_holder VARCHAR(500) NULL COMMENT '权利人',
      land_status VARCHAR(300) NULL COMMENT '土地状态',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      site_images LONGTEXT NULL COMMENT '现场图片 JSON',
      land_type VARCHAR(200) NULL COMMENT '土地类型',
      assessment_time_value VARCHAR(500) NULL COMMENT '评估时间及价值原始值',
      assessment_amount DECIMAL(18,2) NULL COMMENT '评估价标准化金额，单位元',
      assessment_date DATE NULL COMMENT '评估日期',
      special_fields_json LONGTEXT NULL COMMENT '土地特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='土地特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_real_estate (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      right_certificate_no VARCHAR(200) NULL COMMENT '权证编号',
      building_area VARCHAR(200) NULL COMMENT '建筑面积原始值',
      building_area_sqm DECIMAL(18,2) NULL COMMENT '建筑面积标准化值，单位平方米',
      property_use VARCHAR(300) NULL COMMENT '房产用途',
      use_term VARCHAR(300) NULL COMMENT '使用年限/期限',
      property_location VARCHAR(500) NULL COMMENT '房产位置',
      property_structure VARCHAR(300) NULL COMMENT '房产结构',
      property_status VARCHAR(300) NULL COMMENT '房产状态',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      site_images LONGTEXT NULL COMMENT '现场图片 JSON',
      property_type VARCHAR(200) NULL COMMENT '房产类型',
      asset_highlights LONGTEXT NULL COMMENT '资产亮点',
      special_fields_json LONGTEXT NULL COMMENT '房地产特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='房地产特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_equipment (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      storage_location VARCHAR(500) NULL COMMENT '存放位置',
      equipment_status VARCHAR(300) NULL COMMENT '设备状态',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      site_images LONGTEXT NULL COMMENT '现场图片 JSON',
      equipment_type VARCHAR(200) NULL COMMENT '设备类型',
      special_fields_json LONGTEXT NULL COMMENT '设备特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='设备特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_vehicle (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      storage_location VARCHAR(500) NULL COMMENT '存放位置',
      vehicle_brand_model VARCHAR(500) NULL COMMENT '车型品牌',
      vehicle_usage VARCHAR(1000) NULL COMMENT '车辆使用情况',
      plate_number VARCHAR(100) NULL COMMENT '车牌号',
      vehicle_configuration VARCHAR(500) NULL COMMENT '车辆配置',
      vehicle_status VARCHAR(500) NULL COMMENT '车辆状态',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      vehicle_images LONGTEXT NULL COMMENT '车辆图片 JSON',
      vehicle_type VARCHAR(200) NULL COMMENT '车辆类型',
      special_fields_json LONGTEXT NULL COMMENT '车辆特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='车辆特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_debt (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      debtor_name VARCHAR(500) NULL COMMENT '主债务人名称汇总',
      creditor VARCHAR(500) NULL COMMENT '债权人',
      guarantee_method VARCHAR(300) NULL COMMENT '担保方式',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      litigation_status LONGTEXT NULL COMMENT '诉讼状态',
      household_count INT NULL COMMENT '户数',
      benchmark_date VARCHAR(100) NULL COMMENT '基准日原始值',
      benchmark_date_norm DATE NULL COMMENT '基准日标准化值',
      special_fields_json LONGTEXT NULL COMMENT '债权特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权汇总字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_debt_details (
      paimai_id VARCHAR(32) NOT NULL COMMENT '标的 ID',
      detail_index INT NOT NULL COMMENT '明细序号',
      sequence_no VARCHAR(50) NULL COMMENT '原表序号',
      debtor_name VARCHAR(500) NULL COMMENT '该户债务人',
      principal_balance VARCHAR(200) NULL COMMENT '本金余额原始值',
      interest_balance VARCHAR(200) NULL COMMENT '利息余额原始值',
      recovery_fee VARCHAR(200) NULL COMMENT '代垫费用原始值',
      claim_total VARCHAR(200) NULL COMMENT '债权合计原始值',
      principal_balance_amount DECIMAL(18,2) NULL COMMENT '本金余额标准化金额，单位元',
      interest_balance_amount DECIMAL(18,2) NULL COMMENT '利息余额标准化金额，单位元',
      recovery_fee_amount DECIMAL(18,2) NULL COMMENT '代垫费用标准化金额，单位元',
      claim_total_amount DECIMAL(18,2) NULL COMMENT '债权合计标准化金额，单位元',
      collateral LONGTEXT NULL COMMENT '抵质押物',
      guarantor LONGTEXT NULL COMMENT '保证人',
      litigation_status VARCHAR(500) NULL COMMENT '诉讼/执行状态',
      benchmark_date VARCHAR(100) NULL COMMENT '基准日原始值',
      benchmark_date_norm DATE NULL COMMENT '基准日标准化值',
      amount_unit VARCHAR(50) NULL COMMENT '金额单位',
      source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
      updated_at DATETIME NULL COMMENT '更新时间',
      PRIMARY KEY (paimai_id, detail_index),
      KEY idx_debt_detail_debtor (debtor_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='债权逐户明细表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_equity (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      transferor VARCHAR(500) NULL COMMENT '转让方',
      target_company VARCHAR(500) NULL COMMENT '标的企业',
      equity_ratio VARCHAR(100) NULL COMMENT '股权占比',
      company_nature VARCHAR(200) NULL COMMENT '企业性质',
      company_industry VARCHAR(300) NULL COMMENT '企业行业',
      business_scope LONGTEXT NULL COMMENT '经营范围',
      ownership_structure LONGTEXT NULL COMMENT '股权结构',
      financial_metrics LONGTEXT NULL COMMENT '财务指标',
      asset_valuation LONGTEXT NULL COMMENT '资产评估',
      disclosure_items LONGTEXT NULL COMMENT '公示事项',
      attached_assets LONGTEXT NULL COMMENT '附带标的',
      special_fields_json LONGTEXT NULL COMMENT '股权特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='股权特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_ip (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      subject_name VARCHAR(500) NULL COMMENT '标的名称',
      ip_count INT NULL COMMENT '知识产权数量',
      specific_category VARCHAR(300) NULL COMMENT '具体类别',
      right_holder VARCHAR(500) NULL COMMENT '权利人',
      subject_intro LONGTEXT NULL COMMENT '标的简介',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      right_term VARCHAR(300) NULL COMMENT '权利期限',
      special_fields_json LONGTEXT NULL COMMENT '知识产权特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识产权汇总字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_ip_details (
      paimai_id VARCHAR(32) NOT NULL COMMENT '标的 ID',
      detail_index INT NOT NULL COMMENT '明细序号',
      sequence_no VARCHAR(50) NULL COMMENT '原表序号',
      ip_name VARCHAR(500) NULL COMMENT '知识产权名称',
      certificate_no VARCHAR(300) NULL COMMENT '证书号/登记号/申请号',
      ip_type VARCHAR(200) NULL COMMENT '知产类型',
      application_date VARCHAR(100) NULL COMMENT '申请日/登记批准日原始值',
      application_date_norm DATE NULL COMMENT '申请日/登记批准日标准化值',
      patent_type VARCHAR(200) NULL COMMENT '专利类型',
      status VARCHAR(500) NULL COMMENT '状态',
      source_excerpt LONGTEXT NULL COMMENT '来源原文片段',
      updated_at DATETIME NULL COMMENT '更新时间',
      PRIMARY KEY (paimai_id, detail_index),
      KEY idx_ip_detail_name (ip_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识产权逐项明细表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_goods (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      goods_category VARCHAR(300) NULL COMMENT '物资种类',
      goods_name VARCHAR(500) NULL COMMENT '物资名称',
      goods_location VARCHAR(500) NULL COMMENT '物资所在位置',
      goods_details LONGTEXT NULL COMMENT '物资详情',
      right_holder VARCHAR(500) NULL COMMENT '权利人',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      right_burden LONGTEXT NULL COMMENT '权利负担',
      special_fields_json LONGTEXT NULL COMMENT '物资产品特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='物资产品特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_usufruct (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      right_category VARCHAR(300) NULL COMMENT '权益种类',
      subject_name VARCHAR(500) NULL COMMENT '标的名称',
      subject_location VARCHAR(500) NULL COMMENT '标的所在位置',
      subject_details LONGTEXT NULL COMMENT '标的物详情',
      valid_period VARCHAR(300) NULL COMMENT '有效期',
      original_right_holder VARCHAR(500) NULL COMMENT '原权利人',
      disclosed_defects LONGTEXT NULL COMMENT '公示瑕疵',
      right_burden LONGTEXT NULL COMMENT '权利负担',
      special_fields_json LONGTEXT NULL COMMENT '用益物权特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用益物权特有字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_other (
      paimai_id VARCHAR(32) PRIMARY KEY COMMENT '标的 ID',
      raw_detail_text LONGTEXT NULL COMMENT '原始详情文本',
      raw_table_pairs_json LONGTEXT NULL COMMENT '原始表格键值对 JSON',
      special_fields_json LONGTEXT NULL COMMENT '其他类型特有字段 JSON',
      updated_at DATETIME NULL COMMENT '更新时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='其他类型字段表'
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_dedup_index (
      source_platform VARCHAR(50) NOT NULL COMMENT '来源平台',
      source_item_id VARCHAR(80) NOT NULL COMMENT '来源平台标的 ID',
      paimai_id VARCHAR(32) NOT NULL COMMENT '本库标的 ID',
      dedup_hash VARCHAR(64) NOT NULL COMMENT '去重指纹',
      asset_group VARCHAR(32) NULL COMMENT '资产类型',
      project_name VARCHAR(500) NULL COMMENT '项目名称',
      asset_location VARCHAR(500) NULL COMMENT '标的所在地',
      updated_at DATETIME NULL COMMENT '更新时间',
      PRIMARY KEY (source_platform, source_item_id),
      KEY idx_dedup_hash (dedup_hash)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='跨平台去重索引表'
    """,
    """
    CREATE TABLE IF NOT EXISTS field_comments (
      table_name VARCHAR(80) NOT NULL COMMENT '表名',
      column_name VARCHAR(80) NOT NULL COMMENT '字段名',
      label VARCHAR(120) NULL COMMENT '中文显示名',
      comment TEXT NULL COMMENT '中文说明',
      field_scope VARCHAR(50) NULL COMMENT '字段范围',
      asset_group VARCHAR(32) NULL COMMENT '资产类型',
      display_order INT NULL COMMENT '显示顺序',
      PRIMARY KEY (table_name, column_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='预览页字段中文备注表'
    """,
]


def field_comment_rows() -> list[tuple[str, str, str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, str, str, int]] = []
    base_comments = {
        "auction_items_common": {
            "paimai_id": ("标的ID", "京东拍卖标的唯一 ID。"),
            "source_platform": ("来源平台", "数据来源平台，用于后续跨平台去重。"),
            "source_item_id": ("来源平台标的ID", "该平台上的原始标的 ID。"),
            "dedup_hash": ("去重指纹", "由资产类型、名称、地址、权证号、面积等关键要素生成。"),
            "assessment_price_amount": ("评估价-数值", "评估价标准化金额，单位元；原文无评估价时必须为空。"),
            "assessment_date": ("评估日期", "评估基准日或评估日期；原文无对应日期时为空。"),
        },
        "raw_payloads": {
            "description_html": ("标的详情HTML", "标的物详情页 HTML 原文。"),
            "notice_html": ("竞买须知HTML", "竞买须知页 HTML 原文。"),
            "announcement_html": ("竞买公告HTML", "竞买公告页 HTML 原文。"),
            "product_basic_json": ("商品基础信息原始JSON", "商品基础信息接口返回的原始数据，常包含竞价起止时间、评估价、处置/上传机构等结构化字段。"),
        },
    }
    for table, columns in base_comments.items():
        for order, (column, (label, comment)) in enumerate(columns.items(), start=1):
            rows.append((table, column, label, comment, "system", "", order))
    for order, field in enumerate(COMMON_FIELDS, start=100):
        rows.append(
            (
                "auction_items_common",
                field.key,
                field.label,
                f"所有资产类型都必须展示的共有字段：{field.label}。",
                "common",
                "ALL",
                order,
            )
        )
    for group, table in ASSET_TABLES.items():
        rows.append((table, "paimai_id", "标的ID", "京东拍卖标的唯一 ID。", "system", group, 1))
        for order, field in enumerate(SPECIAL_FIELDS[group], start=10):
            rows.append(
                (
                    table,
                    field.key,
                    field.label,
                    f"资产类型“{ASSET_GROUP_LABELS[group]}”的特有字段：{field.label}。",
                    "special",
                    group,
                    order,
                )
            )
        for offset, column in enumerate(SPECIAL_NORMALIZED_COLUMNS.get(group, {}), start=900):
            rows.append((table, column, column, "特有字段生成的标准化伴生列。", "normalized", group, offset))
    for table in ("asset_debt_details", "asset_ip_details"):
        detail_label = "债权逐户明细" if table == "asset_debt_details" else "知识产权逐项明细"
        rows.append((table, "paimai_id", "标的ID", detail_label, "detail", "", 1))
        rows.append((table, "detail_index", "明细序号", "同一标的下的明细序号。", "detail", "", 2))
    return rows


def ensure_mysql_schema(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            for sql in MYSQL_SCHEMA:
                cur.execute(sql)
            cur.execute("SHOW COLUMNS FROM raw_payloads LIKE 'product_basic_json'")
            if not cur.fetchone():
                cur.execute(
                    """
                    ALTER TABLE raw_payloads
                    ADD COLUMN product_basic_json LONGTEXT NULL COMMENT '商品基础信息接口原始 JSON'
                    AFTER detail_json
                    """
                )
            seed_mysql_field_comments(cur)
        conn.commit()


def seed_mysql_field_comments(cur) -> None:
    rows = field_comment_rows()
    if not rows:
        return
    cur.executemany(
        """
        INSERT INTO field_comments
          (table_name, column_name, label, comment, field_scope, asset_group, display_order)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          label=VALUES(label),
          comment=VALUES(comment),
          field_scope=VALUES(field_scope),
          asset_group=VALUES(asset_group),
          display_order=VALUES(display_order)
        """,
        rows,
    )


def mysql_column_types(cur, table: str) -> dict[str, str]:
    cur.execute(f"SHOW COLUMNS FROM `{table}`")
    return {row["Field"]: row["Type"].lower() for row in cur.fetchall()}


def sqlite_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def coerce_mysql_value(value: Any, column_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = str(value)
    if text == "":
        return None if any(token in column_type for token in ("date", "time", "decimal", "int")) else ""
    if "datetime" in column_type or "timestamp" in column_type:
        return datetime_to_db(text)
    if column_type == "date":
        return date_to_db(text)
    if any(token in column_type for token in ("decimal", "int")):
        try:
            number = Decimal(text.replace(",", ""))
        except (InvalidOperation, AttributeError):
            return None
        if "int" in column_type:
            return int(number)
        return format(number.quantize(Decimal("0.01")), "f")
    return text


def upsert_rows(cur, table: str, rows: Iterable[dict[str, Any]], column_types: dict[str, str]) -> int:
    inserted = 0
    dest_columns = set(column_types)
    for row in rows:
        columns = [column for column in row.keys() if column in dest_columns]
        if not columns:
            continue
        values = [coerce_mysql_value(row[column], column_types[column]) for column in columns]
        updates = ", ".join(f"`{column}`=VALUES(`{column}`)" for column in columns)
        sql = (
            f"INSERT INTO `{table}` ({', '.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({qmarks(len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        cur.execute(sql, values)
        inserted += 1
    return inserted


def clean_invalid_assessment_rows(sqlite_path: Path) -> int:
    if not sqlite_path.exists():
        return 0
    cleared = 0
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT c.paimai_id, c.assessment_price_time, fe.source_excerpt,
                   fe.source_payload_type, fe.source_path
            FROM auction_items_common c
            LEFT JOIN field_extractions fe
              ON fe.paimai_id = c.paimai_id
             AND fe.field_namespace = 'common'
             AND fe.field_key = 'assessment_price_time'
            WHERE c.assessment_price_time IS NOT NULL
              AND c.assessment_price_time != ''
            """
        ).fetchall()
        for row in rows:
            source_path = row["source_path"] or ""
            source_payload_type = row["source_payload_type"] or ""
            structured_assessment_field = source_payload_type in {"list_json", "detail_json", "product_basic_json"} and any(
                marker in source_path
                for marker in (
                    "assessmentPrice",
                    "marketPrice",
                    "judicatureBasicInfoResult.marketPrice",
                )
            )
            if is_valid_assessment_price_time(
                row["assessment_price_time"],
                row["source_excerpt"],
                structured_assessment_field=structured_assessment_field,
                require_source_assessment_signal=False,
            ):
                continue
            conn.execute(
                """
                UPDATE auction_items_common
                SET assessment_price_time='',
                    assessment_price_amount=NULL,
                    assessment_amount=NULL,
                    assessment_date=NULL
                WHERE paimai_id=?
                """,
                (row["paimai_id"],),
            )
            conn.execute(
                """
                UPDATE field_extractions
                SET raw_value='',
                    normalized_value='',
                    status='missing_on_page',
                    method='validation',
                    confidence=0,
                    source_payload_type='validation',
                    source_path='invalid_assessment_filtered',
                    source_excerpt=?,
                    missing_reason='原文未提供明确评估价格或评估日期，已过滤比例/比较描述'
                WHERE paimai_id=?
                  AND field_namespace='common'
                  AND field_key='assessment_price_time'
                """,
                (row["source_excerpt"], row["paimai_id"]),
            )
            cleared += 1
        conn.commit()
    finally:
        conn.close()
    return cleared


def import_sqlite_to_mysql(sqlite_path: Path, config: MySQLConfig, *, clean_assessment: bool = True) -> dict[str, int]:
    ensure_mysql_schema(config)
    if clean_assessment:
        clean_invalid_assessment_rows(sqlite_path)

    tables = [
        "crawl_batches",
        "raw_payloads",
        "field_catalog",
        "field_extractions",
        "auction_items_common",
        *ASSET_TABLES.values(),
        "asset_debt_details",
        "asset_ip_details",
        "asset_dedup_index",
    ]
    imported: dict[str, int] = {}
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    try:
        with mysql_connection(config) as dest:
            with dest.cursor() as cur:
                for table in tables:
                    if not sqlite_table_columns(source, table):
                        continue
                    column_types = mysql_column_types(cur, table)
                    rows = [dict(row) for row in source.execute(f"SELECT * FROM {table}")]
                    imported[table] = upsert_rows(cur, table, rows, column_types)
            dest.commit()
    finally:
        source.close()
    return imported


def get_items_mysql(config: MySQLConfig, filters: dict[str, str] | None = None) -> dict[str, Any]:
    filters = filters or {}
    clauses: list[str] = []
    params: list[Any] = []
    if filters.get("asset_group"):
        clauses.append("c.asset_group = %s")
        params.append(filters["asset_group"])
    if filters.get("project_status"):
        clauses.append("c.project_status = %s")
        params.append(filters["project_status"])
    if filters.get("q"):
        clauses.append(
            """
            (
              c.project_name LIKE %s
              OR c.asset_location LIKE %s
              OR c.disposal_party LIKE %s
              OR EXISTS (
                SELECT 1 FROM field_extractions fx
                WHERE fx.paimai_id = c.paimai_id
                  AND fx.raw_value LIKE %s
              )
            )
            """
        )
        like = f"%{filters['q']}%"
        params.extend([like, like, like, like])
    if filters.get("issue") == "missing":
        clauses.append("EXISTS (SELECT 1 FROM field_extractions fm WHERE fm.paimai_id=c.paimai_id AND fm.status!='extracted')")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  c.paimai_id,
                  c.asset_group,
                  c.asset_group_label,
                  c.jd_category_id,
                  c.jd_category_name,
                  c.project_name,
                  c.asset_location,
                  c.project_status,
                  c.start_price_raw,
                  c.final_price_raw,
                  c.disposal_party,
                  COUNT(fe.field_key) AS total_fields,
                  SUM(CASE WHEN fe.status='extracted' THEN 1 ELSE 0 END) AS extracted_fields,
                  SUM(CASE WHEN fe.status!='extracted' THEN 1 ELSE 0 END) AS issue_fields
                FROM auction_items_common c
                LEFT JOIN field_extractions fe ON fe.paimai_id = c.paimai_id
                {where}
                GROUP BY c.paimai_id
                ORDER BY CAST(c.jd_category_id AS UNSIGNED), c.paimai_id
                """,
                params,
            )
            items = cur.fetchall()
            cur.execute(
                "SELECT asset_group, asset_group_label, COUNT(*) AS count "
                "FROM auction_items_common GROUP BY asset_group, asset_group_label ORDER BY asset_group"
            )
            asset_groups = cur.fetchall()
            cur.execute(
                "SELECT DISTINCT project_status FROM auction_items_common "
                "WHERE project_status IS NOT NULL AND project_status!='' ORDER BY project_status"
            )
            statuses = [row["project_status"] for row in cur.fetchall()]
    return {"items": items, "asset_groups": asset_groups, "statuses": statuses}


def table_comments_mysql(cur, table: str) -> dict[str, dict[str, Any]]:
    cur.execute("SELECT * FROM field_comments WHERE table_name=%s", (table,))
    return {row["column_name"]: row for row in cur.fetchall()}


def get_item_detail_mysql(config: MySQLConfig, paimai_id: str) -> dict[str, Any]:
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM auction_items_common WHERE paimai_id=%s", (paimai_id,))
            item = cur.fetchone()
            if item is None:
                raise KeyError(f"找不到标的：{paimai_id}")
            group = item["asset_group"]
            special_table = ASSET_TABLES[group]
            cur.execute(f"SELECT * FROM `{special_table}` WHERE paimai_id=%s", (paimai_id,))
            special_row = cur.fetchone() or {}
            debt_details: list[dict[str, Any]] = []
            if group == "debt":
                cur.execute("SELECT * FROM asset_debt_details WHERE paimai_id=%s ORDER BY detail_index", (paimai_id,))
                debt_details = cur.fetchall()
            ip_details: list[dict[str, Any]] = []
            if group == "ip":
                cur.execute("SELECT * FROM asset_ip_details WHERE paimai_id=%s ORDER BY detail_index", (paimai_id,))
                ip_details = cur.fetchall()
            cur.execute("SELECT * FROM raw_payloads WHERE paimai_id=%s", (paimai_id,))
            raw = cur.fetchone() or {}
            duplicates: list[dict[str, Any]] = []
            if item.get("dedup_hash"):
                cur.execute(
                    """
                    SELECT source_platform, source_item_id, paimai_id, asset_group,
                           project_name, asset_location, updated_at
                    FROM asset_dedup_index
                    WHERE dedup_hash = %s
                      AND paimai_id != %s
                    ORDER BY updated_at DESC
                    """,
                    (item["dedup_hash"], paimai_id),
                )
                duplicates = cur.fetchall()
            common_comments = table_comments_mysql(cur, "auction_items_common")
            special_comments = table_comments_mysql(cur, special_table)

            def load_fields(namespace: str, comments: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
                cur.execute(
                    """
                    SELECT fe.*, fc.export_order
                    FROM field_extractions fe
                    LEFT JOIN field_catalog fc
                      ON fc.field_namespace = fe.field_namespace
                     AND fc.field_key = fe.field_key
                    WHERE fe.paimai_id = %s
                      AND fe.field_namespace = %s
                    ORDER BY COALESCE(fc.export_order, 999), fe.field_key
                    """,
                    (paimai_id, namespace),
                )
                fields = []
                for row in cur.fetchall():
                    comment = comments.get(row["field_key"])
                    fields.append(
                        {
                            "key": row["field_key"],
                            "label": row["field_label"],
                            "comment": comment["comment"] if comment else "字段说明未配置。",
                            "value": row["normalized_value"],
                            "raw_value": row["raw_value"],
                            "status": row["status"],
                            "status_label": row["status"],
                            "method": row["method"],
                            "confidence": row["confidence"],
                            "source_payload_type": row["source_payload_type"],
                            "source_path": row["source_path"],
                            "source_excerpt": row["source_excerpt"],
                            "missing_reason": row["missing_reason"],
                        }
                    )
                return fields

            common_fields = load_fields("common", common_comments)
            special_fields = load_fields(f"special.{group}", special_comments)

    return {
        "item": item,
        "special_row": special_row,
        "raw": raw,
        "common_fields": common_fields,
        "special_fields": special_fields,
        "debt_details": debt_details,
        "ip_details": ip_details,
        "duplicates": duplicates,
        "asset_group_label": ASSET_GROUP_LABELS[group],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步京东拍卖 SQLite 数据到 MySQL")
    parser.add_argument("--sqlite", type=Path, default=Path("outputs") / "v2_multi_preview" / "jd_auction.sqlite")
    parser.add_argument("--host", default=os.getenv("JD_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("JD_MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("JD_MYSQL_USER", ""))
    parser.add_argument("--password", default=os.getenv("JD_MYSQL_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("JD_MYSQL_DATABASE", "auction_data"))
    parser.add_argument("--no-clean-assessment", action="store_true", help="不清理明显误提取的评估价")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = MySQLConfig(args.host, args.port, args.user, args.password, args.database)
    imported = import_sqlite_to_mysql(args.sqlite, config, clean_assessment=not args.no_clean_assessment)
    print(json.dumps(imported, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
