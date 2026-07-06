from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from jd_scraper_v2 import (
    ASSET_GROUP_LABELS,
    ASSET_TABLES,
    COMMON_FIELDS,
    COMMON_FIELD_DATA_TYPES,
    DEBT_LEGACY_AGGREGATE_FIELDS,
    DEDUP_FIELDS_CONFIG,
    FieldDef,
    SPECIAL_FIELDS,
    SPECIAL_FIELD_DATA_TYPES,
    SPECIAL_NORMALIZED_COLUMNS,
    area_sqm_to_db,
    compact_text,
    compute_dedup_hash,
    date_to_db,
    decimal_to_db,
    datetime_to_db,
    has_assessment_date_text,
    is_valid_assessment_price_time,
    money_numeric,
    normalize_dedup_part,
    normalized_common_db_values,
    normalized_special_db_values,
    now_text,
    safe_json_dumps,
    typed_field_extraction_values,
)


@dataclass(frozen=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = "auction_data"


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
      value_type VARCHAR(30) NULL COMMENT '值类型：text/money/date/datetime',
      numeric_value DECIMAL(18,2) NULL COMMENT '数值或金额类字段的标准化数值',
      date_value DATE NULL COMMENT '日期类字段的标准化日期',
      datetime_value DATETIME NULL COMMENT '时间类字段的标准化时间',
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
      disposal_agency VARCHAR(500) NULL COMMENT '处置机构/上传机构',
      start_price_raw VARCHAR(100) NULL COMMENT '起拍价原始展示值',
      start_price_display VARCHAR(100) NULL COMMENT '起拍价展示值',
      final_price_raw VARCHAR(100) NULL COMMENT '最终价/当前价原始展示值',
      current_price_display VARCHAR(100) NULL COMMENT '当前价展示值',
      current_price_amount DECIMAL(18,2) NULL COMMENT '当前价标准化金额，单位元',
      final_price_display VARCHAR(100) NULL COMMENT '最终价/当前价展示值',
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
      identity_basis_json LONGTEXT NULL COMMENT '生成去重指纹的字段原值与标准化值',
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


def ensure_legacy_mysql_schema(config: MySQLConfig) -> None:
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
            ensure_mysql_columns(
                cur,
                "auction_items_common",
                {
                    "disposal_agency": "VARCHAR(500) NULL COMMENT '处置机构/上传机构'",
                    "start_price_display": "VARCHAR(100) NULL COMMENT '起拍价展示值'",
                    "current_price_display": "VARCHAR(100) NULL COMMENT '当前价展示值'",
                    "current_price_amount": "DECIMAL(18,2) NULL COMMENT '当前价标准化金额，单位元'",
                    "final_price_display": "VARCHAR(100) NULL COMMENT '最终价/当前价展示值'",
                },
            )
            ensure_mysql_columns(
                cur,
                "field_extractions",
                {
                    "value_type": "VARCHAR(30) NULL COMMENT '值类型：text/money/date/datetime'",
                    "numeric_value": "DECIMAL(18,2) NULL COMMENT '数值或金额类字段的标准化数值'",
                    "date_value": "DATE NULL COMMENT '日期类字段的标准化日期'",
                    "datetime_value": "DATETIME NULL COMMENT '时间类字段的标准化时间'",
                },
            )
            ensure_mysql_columns(
                cur,
                "asset_dedup_index",
                {
                    "identity_basis_json": "LONGTEXT NULL COMMENT '生成去重指纹的字段原值与标准化值'",
                },
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


def ensure_mysql_columns(cur, table: str, columns: dict[str, str]) -> None:
    column_types = mysql_column_types(cur, table)
    existing = set(column_types)
    for column, definition in columns.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


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


def update_row_columns(
    cur,
    table: str,
    key_column: str,
    key_value: Any,
    values: dict[str, Any],
    column_types: dict[str, str],
) -> int:
    columns = [
        column
        for column, value in values.items()
        if column in column_types and column != key_column and value is not None
    ]
    if not columns:
        return 0
    assignments = ", ".join(f"`{column}`=%s" for column in columns)
    params = [coerce_mysql_value(values[column], column_types[column]) for column in columns]
    params.append(key_value)
    cur.execute(f"UPDATE `{table}` SET {assignments} WHERE `{key_column}`=%s", params)
    return cur.rowcount


def legacy_mysql_table_names() -> list[str]:
    return [
        "field_comments",
        "asset_dedup_index",
        "asset_ip_details",
        "asset_debt_details",
        *ASSET_TABLES.values(),
        "auction_items_common",
        "field_extractions",
        "field_catalog",
        "raw_payloads",
        "crawl_batches",
    ]


def reset_legacy_mysql_tables(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            for table in legacy_mysql_table_names():
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        conn.commit()


class _LegacyMySQLJDScraperDatabase:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config

    def init_schema(self) -> None:
        ensure_legacy_mysql_schema(self.config)

    def seed_field_catalog(self) -> None:
        rows: list[dict[str, Any]] = []
        for order, field in enumerate(COMMON_FIELDS, start=1):
            rows.append(
                {
                    "field_namespace": "common",
                    "asset_group": "ALL",
                    "field_key": field.key,
                    "field_label": field.label,
                    "field_scope": "common",
                    "data_type": COMMON_FIELD_DATA_TYPES.get(field.key, "TEXT"),
                    "required_for_display": 1,
                    "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                    "source_priority_json": safe_json_dumps(["structured_api", "html_table", "html_text"]),
                    "export_order": order,
                }
            )
        for group, fields in SPECIAL_FIELDS.items():
            for order, field in enumerate(fields, start=10):
                rows.append(
                    {
                        "field_namespace": f"special.{group}",
                        "asset_group": group,
                        "field_key": field.key,
                        "field_label": field.label,
                        "field_scope": "special",
                        "data_type": SPECIAL_FIELD_DATA_TYPES.get(group, {}).get(field.key, "TEXT"),
                        "required_for_display": 1,
                        "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                        "source_priority_json": safe_json_dumps(["structured_api", "html_table", "html_text"]),
                        "export_order": order,
                    }
                )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                column_types = mysql_column_types(cur, "field_catalog")
                upsert_rows(cur, "field_catalog", rows, column_types)
            conn.commit()

    def start_batch(self, parameters: dict[str, Any]) -> str:
        import datetime as _dt
        import uuid as _uuid

        batch_id = _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _uuid.uuid4().hex[:8]
        row = {
            "batch_id": batch_id,
            "started_at": now_text(),
            "parameters_json": safe_json_dumps(parameters),
            "status": "running",
            "summary_json": safe_json_dumps({"parameters": parameters, "status": "running", "message": ""}),
        }
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, "crawl_batches", [row], mysql_column_types(cur, "crawl_batches"))
            conn.commit()
        return batch_id

    def finish_batch(self, batch_id: str, status: str, message: str = "") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT summary_json, parameters_json FROM crawl_batches WHERE batch_id=%s", (batch_id,))
                stored = cur.fetchone() or {}
                summary: dict[str, Any] = {}
                if stored.get("summary_json"):
                    try:
                        summary = json.loads(stored["summary_json"])
                    except json.JSONDecodeError:
                        summary = {}
                if not summary and stored.get("parameters_json"):
                    try:
                        summary["parameters"] = json.loads(stored["parameters_json"])
                    except json.JSONDecodeError:
                        summary["parameters"] = stored["parameters_json"]
                summary.update({"status": status, "message": message})
                cur.execute(
                    """
                    UPDATE crawl_batches
                    SET finished_at=%s, status=%s, message=%s, summary_json=%s
                    WHERE batch_id=%s
                    """,
                    (now_text(), status, message, safe_json_dumps(summary), batch_id),
                )
            conn.commit()

    def upsert_raw_payloads(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        source_url: str,
        source_platform: str = "jd",
        source_item_id: str | None = None,
        source_site_name: str | None = None,
        list_json: Any,
        detail_json: Any,
        realtime_json: Any,
        description_html: str | None,
        product_basic_json: Any = None,
        notice_html: str | None = None,
        announcement_html: str | None = None,
        attachments_json: Any = None,
        vendor_json: Any = None,
    ) -> None:
        row = {
            "paimai_id": paimai_id,
            "batch_id": batch_id,
            "source_url": source_url,
            "list_json": safe_json_dumps(list_json),
            "detail_json": safe_json_dumps(detail_json),
            "product_basic_json": safe_json_dumps(product_basic_json or {}),
            "realtime_json": safe_json_dumps(realtime_json),
            "description_html": description_html or "",
            "notice_html": notice_html or "",
            "announcement_html": announcement_html or "",
            "attachments_json": safe_json_dumps(attachments_json),
            "attachment_texts": "",
            "vendor_json": safe_json_dumps(vendor_json or {}),
            "crawled_at": now_text(),
        }
        self._upsert("raw_payloads", row)

    def update_attachment_texts(self, paimai_id: str, attachment_texts: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE raw_payloads SET attachment_texts=%s WHERE paimai_id=%s",
                    (safe_json_dumps(attachment_texts), paimai_id),
                )
            conn.commit()

    def upsert_common_item(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        asset_group: str,
        jd_category_id: str,
        jd_category_name: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any] | None = None,
    ) -> None:
        full_values = {field.key: compact_text(values.get(field.key)) for field in COMMON_FIELDS}
        normalized_values = normalized_common_db_values(
            asset_group=asset_group,
            common_values=full_values,
            special_values=special_values or {},
        )
        normalized_values["source_item_id"] = paimai_id
        row = {
            "paimai_id": paimai_id,
            "batch_id": batch_id,
            "source_url": f"https://paimai.jd.com/{paimai_id}",
            "asset_group": asset_group,
            "asset_group_label": ASSET_GROUP_LABELS[asset_group],
            "jd_category_id": jd_category_id,
            "jd_category_name": jd_category_name,
            **full_values,
            **normalized_values,
            "common_fields_json": safe_json_dumps(full_values),
            "updated_at": now_text(),
        }
        self._upsert("auction_items_common", row)
        self.upsert_dedup_index(
            paimai_id=paimai_id,
            asset_group=asset_group,
            common_values=full_values,
            special_values=special_values or {},
            dedup_hash=normalized_values.get("dedup_hash"),
        )
        self._upsert_field_extractions(
            paimai_id=paimai_id,
            namespace="common",
            asset_group=asset_group,
            fields=COMMON_FIELDS,
            values=full_values,
            field_results=field_results,
        )

    def upsert_special_item(
        self,
        *,
        paimai_id: str,
        asset_group: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        source_platform: str = "jd",
    ) -> None:
        fields = SPECIAL_FIELDS[asset_group]
        full_values = {field.key: compact_text(values.get(field.key)) for field in fields}
        table = ASSET_TABLES[asset_group]
        row = {
            "paimai_id": paimai_id,
            **full_values,
            **normalized_special_db_values(asset_group, full_values),
            "special_fields_json": safe_json_dumps(full_values),
            "updated_at": now_text(),
        }
        self._upsert(table, row)
        if asset_group == "debt":
            self._clear_legacy_debt_aggregate_columns(paimai_id)
        self._upsert_field_extractions(
            paimai_id=paimai_id,
            namespace=f"special.{asset_group}",
            asset_group=asset_group,
            fields=fields,
            values=full_values,
            field_results=field_results,
        )

    def upsert_dedup_index(
        self,
        *,
        paimai_id: str,
        asset_group: str,
        common_values: dict[str, Any],
        special_values: dict[str, Any],
        dedup_hash: Any,
    ) -> None:
        if not compact_text(dedup_hash):
            return
        fields = DEDUP_FIELDS_CONFIG.get(asset_group) or DEDUP_FIELDS_CONFIG["other"]
        identity_basis: dict[str, dict[str, str | None]] = {}
        for field_key in fields:
            raw_value = special_values.get(field_key)
            if not compact_text(raw_value):
                raw_value = common_values.get(field_key)
            normalized = normalize_dedup_part(field_key, raw_value)
            if normalized:
                identity_basis[field_key] = {"raw": compact_text(raw_value), "normalized": normalized}
        self._upsert(
            "asset_dedup_index",
            {
                "source_platform": "jd",
                "source_item_id": paimai_id,
                "paimai_id": paimai_id,
                "dedup_hash": compact_text(dedup_hash),
                "asset_group": asset_group,
                "project_name": common_values.get("project_name"),
                "asset_location": common_values.get("asset_location"),
                "identity_basis_json": safe_json_dumps(identity_basis),
                "updated_at": now_text(),
            },
        )

    def upsert_debt_details(self, *, paimai_id: str, details: list[dict[str, Any]]) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM asset_debt_details WHERE paimai_id=%s", (paimai_id,))
                column_types = mysql_column_types(cur, "asset_debt_details")
                rows = []
                for index, detail in enumerate(details, start=1):
                    principal_balance = compact_text(detail.get("principal_balance"))
                    interest_balance = compact_text(detail.get("interest_balance"))
                    recovery_fee = compact_text(detail.get("recovery_fee"))
                    claim_total = compact_text(detail.get("claim_total"))
                    benchmark_date = compact_text(detail.get("benchmark_date"))
                    rows.append(
                        {
                            "paimai_id": paimai_id,
                            "detail_index": index,
                            "sequence_no": compact_text(detail.get("sequence_no")),
                            "debtor_name": compact_text(detail.get("debtor_name") or detail.get("debtor_or_asset")),
                            "principal_balance": principal_balance,
                            "principal_balance_amount": decimal_to_db(money_numeric(principal_balance)),
                            "interest_balance": interest_balance,
                            "interest_balance_amount": decimal_to_db(money_numeric(interest_balance)),
                            "recovery_fee": recovery_fee,
                            "recovery_fee_amount": decimal_to_db(money_numeric(recovery_fee)),
                            "claim_total": claim_total,
                            "claim_total_amount": decimal_to_db(money_numeric(claim_total)),
                            "collateral": compact_text(detail.get("collateral")),
                            "guarantor": compact_text(detail.get("guarantor") or detail.get("guarantor_or_related_party")),
                            "litigation_status": compact_text(detail.get("litigation_status")),
                            "benchmark_date": benchmark_date,
                            "benchmark_date_norm": date_to_db(benchmark_date),
                            "amount_unit": compact_text(detail.get("amount_unit")),
                            "source_excerpt": (compact_text(detail.get("source_excerpt")) or "")[:500],
                            "updated_at": now_text(),
                        }
                    )
                upsert_rows(cur, "asset_debt_details", rows, column_types)
            conn.commit()

    def upsert_ip_details(self, *, paimai_id: str, details: list[dict[str, Any]]) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM asset_ip_details WHERE paimai_id=%s", (paimai_id,))
                column_types = mysql_column_types(cur, "asset_ip_details")
                rows = []
                for index, detail in enumerate(details, start=1):
                    application_date = compact_text(detail.get("application_date"))
                    rows.append(
                        {
                            "paimai_id": paimai_id,
                            "detail_index": index,
                            "sequence_no": compact_text(detail.get("sequence_no")) or str(index),
                            "ip_name": compact_text(detail.get("ip_name")),
                            "certificate_no": compact_text(detail.get("certificate_no")),
                            "ip_type": compact_text(detail.get("ip_type")),
                            "application_date": application_date,
                            "application_date_norm": date_to_db(application_date),
                            "patent_type": compact_text(detail.get("patent_type")),
                            "status": compact_text(detail.get("status")),
                            "source_excerpt": (compact_text(detail.get("source_excerpt")) or "")[:500],
                            "updated_at": now_text(),
                        }
                    )
                upsert_rows(cur, "asset_ip_details", rows, column_types)
            conn.commit()

    def export_csvs(self, output_dir: Path) -> dict[str, Path]:
        return {}

    def _upsert(self, table: str, row: dict[str, Any]) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, table, [row], mysql_column_types(cur, table))
            conn.commit()

    def _clear_legacy_debt_aggregate_columns(self, paimai_id: str) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                column_types = mysql_column_types(cur, ASSET_TABLES["debt"])
                columns = [column for column in DEBT_LEGACY_AGGREGATE_FIELDS if column in column_types]
                if columns:
                    assignments = ", ".join(f"`{column}`=NULL" for column in columns)
                    cur.execute(f"UPDATE `{ASSET_TABLES['debt']}` SET {assignments} WHERE paimai_id=%s", (paimai_id,))
            conn.commit()

    def _upsert_field_extractions(
        self,
        *,
        paimai_id: str,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        rows: list[dict[str, Any]] = []
        for field in fields:
            value = values.get(field.key)
            result = field_results.get(field.key, {})
            status = result.get("status") or ("extracted" if compact_text(value) else "missing_on_page")
            typed_values = typed_field_extraction_values(field.key, value)
            rows.append(
                {
                    "paimai_id": paimai_id,
                    "field_namespace": namespace,
                    "asset_group": asset_group,
                    "field_key": field.key,
                    "field_label": field.label,
                    "raw_value": compact_text(value),
                    "normalized_value": compact_text(value),
                    **typed_values,
                    "status": status,
                    "method": result.get("method", "not_found" if not compact_text(value) else "api_or_html"),
                    "confidence": float(result.get("confidence", 0.95 if compact_text(value) else 0.0)),
                    "source_payload_type": result.get("source_payload_type", ""),
                    "source_path": result.get("source_path", ""),
                    "source_excerpt": compact_text(result.get("source_excerpt")),
                    "missing_reason": "" if compact_text(value) else result.get("missing_reason", "页面或接口未提供该字段"),
                    "extracted_at": now_text(),
                }
            )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                valid_keys = [field.key for field in fields]
                if valid_keys:
                    cur.execute(
                        f"""
                        DELETE FROM field_extractions
                        WHERE paimai_id=%s
                          AND field_namespace=%s
                          AND field_key NOT IN ({qmarks(len(valid_keys))})
                        """,
                        [paimai_id, namespace, *valid_keys],
                    )
                upsert_rows(cur, "field_extractions", rows, mysql_column_types(cur, "field_extractions"))
            conn.commit()


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
    ensure_legacy_mysql_schema(config)
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
                    FROM (
                        SELECT *,
                            ROW_NUMBER() OVER (
                                PARTITION BY field_key
                                ORDER BY is_selected DESC, extraction_id DESC
                            ) AS rn
                        FROM field_extractions
                        WHERE paimai_id = %s AND field_namespace = %s
                    ) fe
                    LEFT JOIN field_catalog fc
                      ON fc.field_namespace = fe.field_namespace
                     AND fc.field_key = fe.field_key
                    WHERE fe.rn = 1
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


# ===== MySQL V2 storage adapter =====

V2_SCHEMA_PATH = Path(__file__).with_name("sql") / "mysql_schema_v2.sql"
V2_DROP_TABLES = [
    "review_queue",
    "data_quality_reports",
    "asset_dedup_index",
    "ai_enrichment_queue",
    "ocr_retry_queue",
    "asset_other",
    "asset_usufruct",
    "asset_goods",
    "asset_ip_details",
    "asset_ip",
    "asset_equity",
    "asset_debt_details",
    "asset_debt",
    "asset_vehicle",
    "asset_equipment",
    "asset_land",
    "asset_real_estate",
    "item_resources",
    "field_extractions",
    "field_catalog",
    "raw_payloads",
    "crawl_queue",
    "auction_items",
    "crawl_batches",
    "crawl_job_runs",
    "crawl_jobs",
    # Old preview-schema tables kept here so --reset removes mixed local test data.
    "field_comments",
    "auction_items_common",
]

V2_SPECIAL_TABLES = {
    "land": "asset_land",
    "real_estate": "asset_real_estate",
    "equipment": "asset_equipment",
    "vehicle": "asset_vehicle",
    "debt": "asset_debt",
    "equity": "asset_equity",
    "ip": "asset_ip",
    "goods": "asset_goods",
    "usufruct": "asset_usufruct",
    "other": "asset_other",
}


def _blank_to_none(value: Any) -> Any:
    text = compact_text(value)
    return text if text else None


def _money_decimal(value: Any) -> Decimal | None:
    amount = money_numeric(value)
    if amount is None:
        return None
    return amount.quantize(Decimal("0.01"))


def _area_decimal(value: Any) -> Decimal | None:
    area = area_sqm_to_db(value)
    if area is None:
        return None
    try:
        return Decimal(str(area)).quantize(Decimal("0.000001"))
    except InvalidOperation:
        return None


def _int_or_none(value: Any) -> int | None:
    text = compact_text(value)
    if not text:
        return None
    match = re.search(r"\d+", text.replace(",", ""))
    if not match:
        return None
    return int(match.group(0))


def _parse_json_maybe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    text = compact_text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return value


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return safe_json_dumps(value)


def _hash_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = safe_json_dumps(value)
    else:
        text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _payload_tab(payload_type: str) -> str:
    return {
        "list_json": "列表接口",
        "detail_json": "详情接口",
        "product_basic_json": "商品基础信息",
        "realtime_json": "实时价格",
        "description_html": "标的物详情",
        "notice_html": "竞买须知",
        "announcement_html": "竞买公告",
        "attachments_json": "附件材料",
        "vendor_json": "处置方信息",
        "attachment_texts": "附件解析文本",
    }.get(payload_type, payload_type)


def _resource_role(name: str, resource_type: str, asset_group: str | None = None) -> str:
    text = name or ""
    if resource_type == "image":
        if asset_group == "vehicle":
            return "vehicle_image"
        return "site_image"
    if resource_type == "video":
        return "site_video"
    if "评估" in text or "估价" in text:
        return "assessment_report"
    if "清单" in text or "明细" in text or "情况表" in text:
        return "asset_list"
    if "公告" in text:
        return "announcement_file"
    if "须知" in text:
        return "notice_file"
    if "协议" in text or "合同" in text:
        return "agreement_file"
    if "报名" in text or "受让" in text:
        return "registration_file"
    return "attachment_file"


def _normalize_resource_url(url: Any, resource_type: str) -> str | None:
    text = compact_text(url)
    if not text:
        return None
    if text.lower().startswith(("javascript:", "about:", "#")):
        return None
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://")):
        return text
    if resource_type == "image" and text.startswith("jfs/"):
        return "https://img30.360buyimg.com/popWaterMark/" + text
    return text


def _walk_media_urls(value: Any) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in {"imagepath", "imageurl", "imgurl", "imgsrc", "picurl"}:
                url = _normalize_resource_url(item, "image")
                if url:
                    results.append(("image", url))
                continue
            if low in {"videopath", "videourl", "videosrc"}:
                url = _normalize_resource_url(item, "video")
                if url:
                    results.append(("video", url))
                continue
            results.extend(_walk_media_urls(item))
    elif isinstance(value, list):
        for item in value:
            results.extend(_walk_media_urls(item))
    return results


def _valid_assessment_text(
    value: Any,
    *,
    source_excerpt: Any = None,
    source_payload_type: str = "",
    source_path: str = "",
) -> bool:
    text = compact_text(value)
    if not text:
        return False
    structured_field = source_payload_type in {"list_json", "detail_json", "product_basic_json"} and any(
        token in source_path for token in ("assessmentPrice", "marketPrice", "judicatureBasicInfoResult.marketPrice")
    )
    if structured_field:
        return _money_decimal(text) is not None
    if re.fullmatch(r"\s*(市场价|评估价|参考价)\s*\d+(?:\.\d+)?\s*", text):
        return False
    if re.search(r"\d+(?:\.\d+)?\s*(倍|折|成)", text) and not re.search(r"(元|万元|亿元|￥|¥)", text):
        return False
    if not re.search(r"(评估价|评估价格|评估价值|市场价|市场价格|参考价|估价)", text):
        return False
    if not re.search(r"(元|万元|亿元|￥|¥)", text):
        return False
    return is_valid_assessment_price_time(
        text,
        compact_text(source_excerpt),
        structured_assessment_field=False,
        require_source_assessment_signal=False,
    )


def _assessment_basis(value: Any) -> str | None:
    text = compact_text(value) or ""
    if "市场价" in text or "市场价格" in text:
        return "market_price"
    if "参考价" in text:
        return "reference_price"
    if "评估报告" in text:
        return "assessment_report"
    if "评估" in text or "估价" in text:
        return "assessment_price"
    return None


def _bid_count(value: Any) -> int | None:
    parsed = _parse_json_maybe(value)
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("bidList", "bids", "data"):
            item = parsed.get(key)
            if isinstance(item, list):
                return len(item)
    return None


def build_v2_common_item_row(
    *,
    paimai_id: str,
    batch_id: str | None,
    asset_group: str,
    jd_category_id: str | None,
    jd_category_name: str | None,
    values: dict[str, Any],
    special_values: dict[str, Any] | None = None,
    field_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    special_values = special_values or {}
    field_results = field_results or {}
    source_platform = _blank_to_none(values.get("source_platform")) or "jd"
    source_item_id = _blank_to_none(values.get("source_item_id")) or str(paimai_id)
    source_url = _blank_to_none(values.get("source_url")) or (
        f"https://paimai.jd.com/{paimai_id}" if source_platform == "jd" else ""
    )
    source_site_name = _blank_to_none(values.get("source_site_name")) or (
        "京东拍卖" if source_platform == "jd" else source_platform
    )
    start_display = _blank_to_none(values.get("start_price_raw"))
    explicit_final_display = _blank_to_none(values.get("final_price_raw"))
    final_display = explicit_final_display or start_display
    assessment_text = _blank_to_none(values.get("assessment_price_time")) or _blank_to_none(
        special_values.get("assessment_time_value")
    )
    assessment_result = field_results.get("assessment_price_time", {})
    assessment_valid = _valid_assessment_text(
        assessment_text,
        source_excerpt=assessment_result.get("source_excerpt"),
        source_payload_type=compact_text(assessment_result.get("source_payload_type")) or "",
        source_path=compact_text(assessment_result.get("source_path")) or "",
    )
    assessment_amount = _money_decimal(assessment_text) if assessment_valid else None
    assessment_date = (
        date_to_db(assessment_text)
        if assessment_valid and has_assessment_date_text(assessment_text)
        else None
    )
    common_for_hash = {field.key: values.get(field.key) for field in COMMON_FIELDS}
    common_for_hash["source_platform"] = source_platform
    common_for_hash["source_item_id"] = source_item_id
    right_holder = None if asset_group == "debt" else _blank_to_none(
        special_values.get("right_holder") or special_values.get("original_right_holder")
    )
    return {
        "source_platform": source_platform,
        "source_item_id": source_item_id,
        "source_url": source_url,
        "source_site_name": source_site_name,
        "batch_id": batch_id,
        "asset_group": asset_group,
        "asset_group_label": ASSET_GROUP_LABELS.get(asset_group, asset_group),
        "source_category_id": _blank_to_none(jd_category_id),
        "source_category_name": _blank_to_none(jd_category_name),
        "asset_type": _blank_to_none(values.get("asset_type")),
        "asset_location": _blank_to_none(values.get("asset_location")),
        "project_status": _blank_to_none(values.get("project_status")),
        "project_status_basis": _blank_to_none(values.get("project_status_basis")),
        "auction_stage": _blank_to_none(values.get("auction_stage")),
        "bid_records_count": _bid_count(values.get("bid_records_json")),
        "bid_records_json": _json_or_none(_parse_json_maybe(values.get("bid_records_json")) or []),
        "data_source": _blank_to_none(values.get("data_source")),
        "project_name": _blank_to_none(values.get("project_name")) or f"JD-{paimai_id}",
        "signup_start_time": datetime_to_db(values.get("signup_start_time")),
        "signup_end_time": datetime_to_db(values.get("signup_end_time")),
        "disposal_party": _blank_to_none(values.get("disposal_party")),
        "disposal_agency": _blank_to_none(values.get("disposal_agency")),
        "right_holder": right_holder,
        "start_price_amount": _money_decimal(start_display),
        "start_price_display": start_display,
        "final_price_amount": _money_decimal(final_display),
        "final_price_display": final_display,
        "price_basis": "effective_price" if explicit_final_display else "start_price_fallback",
        "contact_info": _blank_to_none(values.get("contact_info")),
        "special_notice": _blank_to_none(values.get("special_notice")),
        "disclosed_defects": _blank_to_none(special_values.get("disclosed_defects")),
        "assessment_price_amount": assessment_amount,
        "assessment_price_display": assessment_text if assessment_valid else None,
        "assessment_price_basis": _assessment_basis(assessment_text) if assessment_valid else None,
        "assessment_date": assessment_date,
        "dedup_hash": compute_dedup_hash(asset_group, common_for_hash, special_values),
        "last_seen_at": now_text(),
        "last_crawled_at": now_text(),
    }


def build_v2_resource_rows(
    *,
    item_id: int,
    attachments_json: Any,
    asset_group: str | None = None,
    source_payload_id: int | None = None,
) -> list[dict[str, Any]]:
    parsed = _parse_json_maybe(attachments_json)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_row(resource_type: str, url: Any, name: Any = None, fmt: Any = None, size: Any = None, section: str = "") -> None:
        normalized_url = _normalize_resource_url(url, resource_type)
        if not normalized_url:
            return
        key = (resource_type, normalized_url)
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "item_id": item_id,
                "resource_type": resource_type,
                "resource_role": _resource_role(compact_text(name) or "", resource_type, asset_group),
                "resource_name": _blank_to_none(name) or normalized_url.rsplit("/", 1)[-1],
                "resource_url": normalized_url,
                "resource_format": _blank_to_none(fmt),
                "resource_size_bytes": _int_or_none(size),
                "source_section": section or _payload_tab("attachments_json"),
                "source_payload_id": source_payload_id,
                "url_hash": _hash_text(normalized_url),
                "is_downloaded": 0,
            }
        )

    file_items: list[Any] = []

    def walk_file_items(value: Any) -> None:
        value = _parse_json_maybe(value)
        if isinstance(value, dict):
            has_url = any(
                compact_text(value.get(key))
                for key in (
                    "attachmentAddress",
                    "url",
                    "href",
                    "fileUrl",
                    "downloadUrl",
                    "downloadURL",
                    "attachmentUrl",
                    "filePath",
                    "resourceUrl",
                    "ossUrl",
                )
            )
            if has_url:
                file_items.append(value)
            for item in value.values():
                walk_file_items(item)
        elif isinstance(value, list):
            for item in value:
                walk_file_items(item)

    if isinstance(parsed, dict):
        for key in ("files", "data", "attachments", "attachmentList"):
            item = parsed.get(key)
            if isinstance(item, list):
                file_items.extend(item)
            elif item is not None:
                walk_file_items(item)
        media_source = parsed.get("media", parsed)
    elif isinstance(parsed, list):
        file_items.extend([item for item in parsed if isinstance(item, dict) and item.get("attachmentAddress")])
        walk_file_items(parsed)
        media_source = parsed
    else:
        media_source = None

    for item in file_items:
        if not isinstance(item, dict):
            continue
        add_row(
            "attachment",
            item.get("attachmentAddress")
            or item.get("url")
            or item.get("href")
            or item.get("fileUrl")
            or item.get("downloadUrl")
            or item.get("downloadURL")
            or item.get("attachmentUrl")
            or item.get("filePath")
            or item.get("resourceUrl")
            or item.get("ossUrl"),
            item.get("attachmentName") or item.get("name") or item.get("fileName") or item.get("title"),
            item.get("attachmentFormat") or item.get("format") or item.get("fileType"),
            item.get("attachmentSize") or item.get("size") or item.get("fileSize"),
        )

    for resource_type, url in _walk_media_urls(media_source):
        add_row(resource_type, url, url, None, None, "图片/视频")
    return rows


def build_v2_special_row(*, item_id: int, asset_group: str, values: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"item_id": item_id}
    if asset_group == "real_estate":
        row.update(
            {
                "right_certificate_no": _blank_to_none(values.get("right_certificate_no")),
                "building_area_sqm": _area_decimal(values.get("building_area")),
                "building_area_display": _blank_to_none(values.get("building_area")),
                "property_use": _blank_to_none(values.get("property_use")),
                "use_term": _blank_to_none(values.get("use_term")),
                "property_location": _blank_to_none(values.get("property_location")),
                "property_structure": _blank_to_none(values.get("property_structure")),
                "property_status": _blank_to_none(values.get("property_status")),
                "property_type": _blank_to_none(values.get("property_type")),
                "asset_highlights": _blank_to_none(values.get("asset_highlights")),
            }
        )
    elif asset_group == "land":
        row.update(
            {
                "right_certificate_no": _blank_to_none(values.get("right_certificate_no")),
                "land_area_sqm": _area_decimal(values.get("land_area")),
                "land_area_display": _blank_to_none(values.get("land_area")),
                "land_use": _blank_to_none(values.get("land_use")),
                "use_term": _blank_to_none(values.get("use_term")),
                "land_location": _blank_to_none(values.get("land_location")),
                "land_status": _blank_to_none(values.get("land_status")),
                "land_type": _blank_to_none(values.get("land_type")),
            }
        )
    elif asset_group == "equipment":
        row.update(
            {
                "storage_location": _blank_to_none(values.get("storage_location")),
                "equipment_status": _blank_to_none(values.get("equipment_status")),
                "equipment_type": _blank_to_none(values.get("equipment_type")),
            }
        )
    elif asset_group == "vehicle":
        row.update(
            {
                "storage_location": _blank_to_none(values.get("storage_location")),
                "vehicle_brand_model": _blank_to_none(values.get("vehicle_brand_model")),
                "vehicle_usage": _blank_to_none(values.get("vehicle_usage")),
                "plate_number": _blank_to_none(values.get("plate_number")),
                "vehicle_configuration": _blank_to_none(values.get("vehicle_configuration")),
                "vehicle_status": _blank_to_none(values.get("vehicle_status")),
                "vehicle_type": _blank_to_none(values.get("vehicle_type")),
            }
        )
    elif asset_group == "debt":
        debtor_names = _blank_to_none(values.get("debtor_name"))
        row.update(
            {
                "main_debtor_name": debtor_names.split("、", 1)[0] if debtor_names else None,
                "debtor_names": debtor_names,
                "creditor": _blank_to_none(values.get("creditor")),
                "principal_balance_amount": _money_decimal(values.get("principal_balance")),
                "principal_balance_display": _blank_to_none(values.get("principal_balance")),
                "interest_balance_amount": _money_decimal(values.get("interest_balance")),
                "interest_balance_display": _blank_to_none(values.get("interest_balance")),
                "claim_total_amount": _money_decimal(values.get("claim_total")),
                "claim_total_display": _blank_to_none(values.get("claim_total")),
                "benchmark_date": date_to_db(values.get("benchmark_date")),
                "guarantee_method": _blank_to_none(values.get("guarantee_method")),
                "guarantor": _blank_to_none(values.get("guarantor")),
                "collateral": _blank_to_none(values.get("collateral")),
                "litigation_status": _blank_to_none(values.get("litigation_status")),
                "household_count": _int_or_none(values.get("household_count")),
            }
        )
    elif asset_group == "equity":
        for key in (
            "transferor",
            "target_company",
            "equity_ratio",
            "company_nature",
            "company_industry",
            "business_scope",
            "ownership_structure",
            "financial_metrics",
            "asset_valuation",
            "disclosure_items",
            "attached_assets",
        ):
            row[key] = _blank_to_none(values.get(key))
    elif asset_group == "ip":
        row.update(
            {
                "subject_name": _blank_to_none(values.get("subject_name")),
                "ip_count": _int_or_none(values.get("ip_count")),
                "certificate_no": _blank_to_none(values.get("certificate_no")),
                "ip_type": _blank_to_none(values.get("ip_type")),
                "specific_category": _blank_to_none(values.get("specific_category")),
                "subject_intro": _blank_to_none(values.get("subject_intro")),
                "right_term": _blank_to_none(values.get("right_term")),
            }
        )
    elif asset_group == "goods":
        row.update(
            {
                "goods_category": _blank_to_none(values.get("goods_category")),
                "goods_name": _blank_to_none(values.get("goods_name")),
                "goods_location": _blank_to_none(values.get("goods_location")),
                "goods_details": _blank_to_none(values.get("goods_details")),
                "right_burden": _blank_to_none(values.get("right_burden")),
            }
        )
    elif asset_group == "usufruct":
        row.update(
            {
                "right_category": _blank_to_none(values.get("right_category")),
                "subject_name": _blank_to_none(values.get("subject_name")),
                "subject_location": _blank_to_none(values.get("subject_location")),
                "subject_details": _blank_to_none(values.get("subject_details")),
                "valid_period": _blank_to_none(values.get("valid_period")),
                "original_right_holder": _blank_to_none(values.get("original_right_holder")),
                "right_burden": _blank_to_none(values.get("right_burden")),
            }
        )
    else:
        row.update(
            {
                "raw_detail_text": _blank_to_none(values.get("raw_detail_text")),
                "raw_table_pairs_json": _json_or_none(_parse_json_maybe(values.get("raw_table_pairs_json"))),
                "extracted_summary": _blank_to_none(values.get("extracted_summary")),
            }
        )
    return {key: value for key, value in row.items() if value is not None or key == "item_id"}


def coerce_mysql_value(value: Any, column_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, Decimal):
        number = value
        text = str(value)
    else:
        text = str(value)
    if text == "":
        return None if any(token in column_type for token in ("date", "time", "decimal", "int", "json")) else ""
    if "datetime" in column_type or "timestamp" in column_type:
        return datetime_to_db(text)
    if column_type == "date":
        return date_to_db(text)
    if "json" in column_type:
        parsed = _parse_json_maybe(value)
        return safe_json_dumps(parsed)
    if any(token in column_type for token in ("decimal", "int", "bigint")):
        try:
            number = value if isinstance(value, Decimal) else Decimal(text.replace(",", ""))
        except (InvalidOperation, AttributeError):
            return None
        if "int" in column_type and "decimal" not in column_type:
            return int(number)
        match = re.search(r"decimal\(\d+\s*,\s*(\d+)\)", column_type)
        scale = int(match.group(1)) if match else 2
        quantum = Decimal("1").scaleb(-scale)
        return format(number.quantize(quantum), "f")
    return text


def mysql_table_names() -> list[str]:
    return list(V2_DROP_TABLES)


def ensure_mysql_schema(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    sql_text = V2_SCHEMA_PATH.read_text(encoding="utf-8")
    sql_text = re.sub(r"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+auction_data.*?;", "", sql_text, flags=re.I | re.S)
    sql_text = re.sub(r"USE\s+auction_data\s*;", f"USE `{config.database}`;", sql_text, flags=re.I)
    statements = [statement.strip() for statement in sql_text.split(";") if statement.strip()]
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        conn.commit()


def reset_mysql_tables(config: MySQLConfig) -> None:
    ensure_mysql_database(config)
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in V2_DROP_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    ensure_mysql_schema(config)


def _get_item_id(cur, source_item_id: str, *, source_platform: str = "jd", required: bool = True) -> int | None:
    cur.execute(
        "SELECT item_id FROM auction_items WHERE source_platform=%s AND source_item_id=%s",
        (source_platform, str(source_item_id)),
    )
    row = cur.fetchone()
    if row:
        return int(row["item_id"])
    if required:
        raise KeyError(f"item not found: {source_platform}:{source_item_id}")
    return None


def _ensure_item_stub(
    cur,
    paimai_id: str,
    batch_id: str | None = None,
    source_url: str | None = None,
    *,
    source_platform: str = "jd",
    source_site_name: str | None = None,
) -> int:
    source_item_id = str(paimai_id)
    row = {
        "source_platform": source_platform,
        "source_item_id": source_item_id,
        "source_url": source_url or (f"https://paimai.jd.com/{paimai_id}" if source_platform == "jd" else ""),
        "batch_id": batch_id,
        "source_site_name": source_site_name or ("京东拍卖" if source_platform == "jd" else source_platform),
        "asset_group": "other",
        "asset_group_label": ASSET_GROUP_LABELS.get("other", "其他"),
        "project_name": f"{source_platform}-{source_item_id}",
        "last_seen_at": now_text(),
        "last_crawled_at": now_text(),
    }
    upsert_rows(cur, "auction_items", [row], mysql_column_types(cur, "auction_items"))
    return int(_get_item_id(cur, source_item_id, source_platform=source_platform))


def _insert_payload(
    cur,
    *,
    item_id: int,
    batch_id: str | None,
    payload_type: str,
    value: Any,
    source_url: str | None,
    source_platform: str = "jd",
) -> int | None:
    if value is None:
        return None
    is_json_payload = payload_type.endswith("_json")
    payload_text = None if is_json_payload else str(value or "")
    payload_json = safe_json_dumps(value) if is_json_payload else None
    payload_hash = _hash_text(payload_json if is_json_payload else payload_text)
    row = {
        "item_id": item_id,
        "batch_id": batch_id,
        "source_platform": source_platform,
        "payload_type": payload_type,
        "source_url": source_url,
        "source_tab": _payload_tab(payload_type),
        "payload_text": payload_text,
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "fetched_at": now_text(),
    }
    upsert_rows(cur, "raw_payloads", [row], mysql_column_types(cur, "raw_payloads"))
    cur.execute(
        """
        SELECT payload_id FROM raw_payloads
        WHERE item_id=%s AND payload_type=%s AND payload_hash=%s
        ORDER BY payload_id DESC LIMIT 1
        """,
        (item_id, payload_type, payload_hash),
    )
    found = cur.fetchone()
    return int(found["payload_id"]) if found else None


class MySQLJDScraperDatabase:
    def __init__(self, config: MySQLConfig) -> None:
        self.config = config

    def init_schema(self) -> None:
        ensure_mysql_schema(self.config)

    def seed_field_catalog(self) -> None:
        rows: list[dict[str, Any]] = []
        for order, field in enumerate(COMMON_FIELDS, start=1):
            rows.append(
                {
                    "field_namespace": "common",
                    "asset_group": "ALL",
                    "field_key": field.key,
                    "field_label": field.label,
                    "field_comment": f"所有资产类型都应展示的共有字段：{field.label}",
                    "data_type": COMMON_FIELD_DATA_TYPES.get(field.key, "text"),
                    "required_for_display": 1,
                    "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                    "source_priority_json": safe_json_dumps(["api", "html", "ai"]),
                    "export_order": order,
                }
            )
        for group, fields in SPECIAL_FIELDS.items():
            for order, field in enumerate(fields, start=10):
                rows.append(
                    {
                        "field_namespace": "special",
                        "asset_group": group,
                        "field_key": field.key,
                        "field_label": field.label,
                        "field_comment": f"资产类型“{ASSET_GROUP_LABELS.get(group, group)}”的特有字段：{field.label}",
                        "data_type": SPECIAL_FIELD_DATA_TYPES.get(group, {}).get(field.key, "text"),
                        "required_for_display": 1,
                        "aliases_json": safe_json_dumps((field.label, *field.aliases)),
                        "source_priority_json": safe_json_dumps(["api", "html", "ai"]),
                        "export_order": order,
                    }
                )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, "field_catalog", rows, mysql_column_types(cur, "field_catalog"))
            conn.commit()

    def start_batch(self, parameters: dict[str, Any]) -> str:
        import uuid as _uuid

        batch_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _uuid.uuid4().hex[:8]
        row = {
            "batch_id": batch_id,
            "source_platform": compact_text(parameters.get("source_platform")) or "jd",
            "started_at": now_text(),
            "parameters_json": safe_json_dumps(parameters),
            "status": "running",
            "summary_json": safe_json_dumps({"parameters": parameters, "status": "running", "message": ""}),
        }
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                upsert_rows(cur, "crawl_batches", [row], mysql_column_types(cur, "crawl_batches"))
            conn.commit()
        return batch_id

    def finish_batch(self, batch_id: str, status: str, message: str = "") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT summary_json, parameters_json FROM crawl_batches WHERE batch_id=%s", (batch_id,))
                stored = cur.fetchone() or {}
                summary: dict[str, Any] = {}
                if stored.get("summary_json"):
                    try:
                        summary = json.loads(stored["summary_json"])
                    except (TypeError, json.JSONDecodeError):
                        summary = {}
                if not summary and stored.get("parameters_json"):
                    try:
                        summary["parameters"] = json.loads(stored["parameters_json"])
                    except (TypeError, json.JSONDecodeError):
                        summary["parameters"] = stored["parameters_json"]
                summary.update({"status": status, "message": message})
                cur.execute(
                    """
                    UPDATE crawl_batches
                    SET finished_at=%s, status=%s, message=%s, summary_json=%s
                    WHERE batch_id=%s
                    """,
                    (now_text(), status, message, safe_json_dumps(summary), batch_id),
                )
            conn.commit()

    def upsert_raw_payloads(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        source_url: str,
        source_platform: str = "jd",
        source_item_id: str | None = None,
        source_site_name: str | None = None,
        list_json: Any,
        detail_json: Any,
        realtime_json: Any,
        description_html: str | None,
        product_basic_json: Any = None,
        notice_html: str | None = None,
        announcement_html: str | None = None,
        attachments_json: Any = None,
        vendor_json: Any = None,
    ) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_key = source_item_id or paimai_id
                item_id = _ensure_item_stub(
                    cur,
                    item_key,
                    batch_id=batch_id,
                    source_url=source_url,
                    source_platform=source_platform,
                    source_site_name=source_site_name,
                )
                payload_types = [
                    "list_json",
                    "detail_json",
                    "product_basic_json",
                    "realtime_json",
                    "description_html",
                    "notice_html",
                    "announcement_html",
                    "attachments_json",
                    "vendor_json",
                ]
                cur.execute(
                    f"DELETE FROM raw_payloads WHERE item_id=%s AND batch_id=%s AND payload_type IN ({qmarks(len(payload_types))})",
                    [item_id, batch_id, *payload_types],
                )
                payload_id_by_type: dict[str, int | None] = {}
                for payload_type, value in (
                    ("list_json", list_json),
                    ("detail_json", detail_json),
                    ("product_basic_json", product_basic_json or {}),
                    ("realtime_json", realtime_json),
                    ("description_html", description_html or ""),
                    ("notice_html", notice_html or ""),
                    ("announcement_html", announcement_html or ""),
                    ("attachments_json", attachments_json),
                    ("vendor_json", vendor_json or {}),
                ):
                    payload_id_by_type[payload_type] = _insert_payload(
                        cur,
                        item_id=item_id,
                        batch_id=batch_id,
                        payload_type=payload_type,
                        value=value,
                        source_url=source_url,
                        source_platform=source_platform,
                    )
                resources = build_v2_resource_rows(
                    item_id=item_id,
                    attachments_json=attachments_json,
                    source_payload_id=payload_id_by_type.get("attachments_json"),
                )
                if resources:
                    cur.execute("DELETE FROM item_resources WHERE item_id=%s", (item_id,))
                    upsert_rows(cur, "item_resources", resources, mysql_column_types(cur, "item_resources"))
            conn.commit()

    def update_attachment_texts(self, paimai_id: str, attachment_texts: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = _ensure_item_stub(cur, paimai_id)
                _insert_payload(
                    cur,
                    item_id=item_id,
                    batch_id=None,
                    payload_type="attachment_texts",
                    value=attachment_texts,
                    source_url=f"https://paimai.jd.com/{paimai_id}",
                )
            conn.commit()

    def upsert_common_item(
        self,
        *,
        paimai_id: str,
        batch_id: str,
        asset_group: str,
        jd_category_id: str,
        jd_category_name: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any] | None = None,
    ) -> None:
        full_values = {field.key: compact_text(values.get(field.key)) for field in COMMON_FIELDS}
        for meta_key in ("source_platform", "source_item_id", "source_url", "source_site_name"):
            if meta_key in values:
                full_values[meta_key] = compact_text(values.get(meta_key))
        row = build_v2_common_item_row(
            paimai_id=paimai_id,
            batch_id=batch_id,
            asset_group=asset_group,
            jd_category_id=jd_category_id,
            jd_category_name=jd_category_name,
            values=full_values,
            special_values=special_values or {},
            field_results=field_results,
        )
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                _ensure_item_stub(
                    cur,
                    row["source_item_id"],
                    batch_id=batch_id,
                    source_url=row["source_url"],
                    source_platform=row["source_platform"],
                    source_site_name=row.get("source_site_name"),
                )
                upsert_rows(cur, "auction_items", [row], mysql_column_types(cur, "auction_items"))
                item_id = int(_get_item_id(cur, row["source_item_id"], source_platform=row["source_platform"]))
                self._upsert_dedup_index_cur(cur, item_id=item_id, row=row, common_values=full_values)
                self._upsert_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="common",
                    asset_group=asset_group,
                    fields=COMMON_FIELDS,
                    values=full_values,
                    field_results=field_results,
                )
            conn.commit()

    def upsert_special_item(
        self,
        *,
        paimai_id: str,
        asset_group: str,
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
        source_platform: str = "jd",
    ) -> None:
        fields = SPECIAL_FIELDS[asset_group]
        full_values = {field.key: compact_text(values.get(field.key)) for field in fields}
        table = V2_SPECIAL_TABLES[asset_group]
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                row = build_v2_special_row(item_id=item_id, asset_group=asset_group, values=full_values)
                upsert_rows(cur, table, [row], mysql_column_types(cur, table))
                if asset_group == "debt":
                    self._refresh_debt_main_from_details_cur(cur, item_id)
                self._upsert_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="special",
                    asset_group=asset_group,
                    fields=fields,
                    values=full_values,
                    field_results=field_results,
                )
            conn.commit()

    def upsert_debt_details(self, *, paimai_id: str, details: list[dict[str, Any]], source_platform: str = "jd") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute("DELETE FROM asset_debt_details WHERE item_id=%s", (item_id,))
                rows = []
                for index, detail in enumerate(details, start=1):
                    rows.append(
                        {
                            "item_id": item_id,
                            "detail_index": index,
                            "sequence_no": _blank_to_none(detail.get("sequence_no")),
                            "debtor_name": _blank_to_none(detail.get("debtor_name") or detail.get("debtor_or_asset")),
                            "principal_balance_amount": _money_decimal(detail.get("principal_balance")),
                            "interest_balance_amount": _money_decimal(detail.get("interest_balance")),
                            "claim_total_amount": _money_decimal(detail.get("claim_total")),
                            "benchmark_date": date_to_db(detail.get("benchmark_date")),
                            "collateral": _blank_to_none(detail.get("collateral")),
                            "guarantor": _blank_to_none(detail.get("guarantor") or detail.get("guarantor_or_related_party")),
                            "litigation_status": _blank_to_none(detail.get("litigation_status")),
                            "source_excerpt": (_blank_to_none(detail.get("source_excerpt")) or "")[:1000],
                        }
                    )
                upsert_rows(cur, "asset_debt_details", rows, mysql_column_types(cur, "asset_debt_details"))
                self._refresh_debt_main_from_details_cur(cur, item_id)
            conn.commit()

    def upsert_ip_details(self, *, paimai_id: str, details: list[dict[str, Any]], source_platform: str = "jd") -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute("DELETE FROM asset_ip_details WHERE item_id=%s", (item_id,))
                rows = []
                for index, detail in enumerate(details, start=1):
                    rows.append(
                        {
                            "item_id": item_id,
                            "detail_index": index,
                            "sequence_no": _blank_to_none(detail.get("sequence_no")) or str(index),
                            "ip_name": _blank_to_none(detail.get("ip_name")),
                            "certificate_no": _blank_to_none(detail.get("certificate_no")),
                            "registration_no": _blank_to_none(detail.get("registration_no")),
                            "acquire_method": _blank_to_none(detail.get("acquire_method")),
                            "application_date": date_to_db(detail.get("application_date")),
                            "approval_date": date_to_db(detail.get("approval_date") or detail.get("application_date")),
                            "ip_type": _blank_to_none(detail.get("ip_type")),
                            "patent_type": _blank_to_none(detail.get("patent_type")),
                            "right_holder": _blank_to_none(detail.get("right_holder")),
                            "right_status": _blank_to_none(detail.get("right_status") or detail.get("status")),
                            "source_excerpt": (_blank_to_none(detail.get("source_excerpt")) or "")[:1000],
                        }
                    )
                upsert_rows(cur, "asset_ip_details", rows, mysql_column_types(cur, "asset_ip_details"))
                if rows:
                    cur.execute(
                        "UPDATE asset_ip SET ip_count=%s WHERE item_id=%s AND (ip_count IS NULL OR ip_count=0)",
                        (len(rows), item_id),
                    )
            conn.commit()

    def enqueue_ocr_retry_task(self, *, paimai_id: str, task: dict[str, Any], source_platform: str = "jd") -> None:
        image_urls = task.get("image_urls") or task.get("resource_urls") or []
        if not image_urls:
            return
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                row = {
                    "item_id": item_id,
                    "source_platform": source_platform,
                    "source_item_id": str(paimai_id),
                    "task_type": compact_text(task.get("task_type")) or "ip_image_details",
                    "resource_urls_json": safe_json_dumps(image_urls),
                    "queue_status": "pending",
                    "priority": int(task.get("priority") or 100),
                    "retry_count": 0,
                    "max_retries": int(task.get("max_retries") or 3),
                    "last_error": _blank_to_none(task.get("reason") or task.get("last_error")),
                    "result_json": None,
                }
                upsert_rows(cur, "ocr_retry_queue", [row], mysql_column_types(cur, "ocr_retry_queue"))
            conn.commit()

    def enqueue_ai_enrichment_task(
        self,
        *,
        paimai_id: str,
        source_platform: str = "jd",
        source_item_id: str | None = None,
        asset_group: str,
        context: dict[str, Any],
        task_type: str = "field_enrichment",
        field_keys: list[str] | None = None,
        priority: int = 100,
        max_retries: int = 3,
        reason: str = "",
    ) -> None:
        item_ref = compact_text(source_item_id) or compact_text(paimai_id)
        if not item_ref:
            raise ValueError("source_item_id is required for AI enrichment queue")
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, item_ref, source_platform=source_platform))
                row = {
                    "item_id": item_id,
                    "source_platform": source_platform,
                    "source_item_id": item_ref,
                    "asset_group": asset_group,
                    "task_type": task_type,
                    "context_json": context,
                    "field_keys_json": field_keys or None,
                    "queue_status": "pending",
                    "priority": priority,
                    "retry_count": 0,
                    "max_retries": max_retries,
                    "last_error": _blank_to_none(reason),
                    "result_json": None,
                }
                upsert_rows(cur, "ai_enrichment_queue", [row], mysql_column_types(cur, "ai_enrichment_queue"))
            conn.commit()

    def fetch_ai_enrichment_tasks(
        self,
        *,
        limit: int = 20,
        worker_id: str = "ai-worker",
        stale_minutes: int = 30,
    ) -> list[dict[str, Any]]:
        limit = max(1, int(limit or 1))
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM ai_enrichment_queue
                    WHERE (
                        queue_status='pending'
                        OR (queue_status='failed' AND retry_count < max_retries)
                        OR (
                            queue_status='running'
                            AND locked_at IS NOT NULL
                            AND locked_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
                        )
                    )
                    ORDER BY priority ASC, ai_task_id ASC
                    LIMIT %s
                    """,
                    (stale_minutes, limit),
                )
                rows = list(cur.fetchall())
                if rows:
                    ids = [int(row["ai_task_id"]) for row in rows]
                    cur.execute(
                        f"""
                        UPDATE ai_enrichment_queue
                        SET queue_status='running', locked_by=%s, locked_at=NOW(), updated_at=NOW()
                        WHERE ai_task_id IN ({qmarks(len(ids))})
                        """,
                        [worker_id, *ids],
                    )
            conn.commit()
        return rows

    def mark_ai_enrichment_task_success(self, ai_task_id: int, result_json: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai_enrichment_queue
                    SET queue_status='success', result_json=%s, last_error=NULL, updated_at=NOW()
                    WHERE ai_task_id=%s
                    """,
                    (safe_json_dumps(result_json), ai_task_id),
                )
            conn.commit()

    def mark_ai_enrichment_task_failed(self, ai_task_id: int, error: Any) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai_enrichment_queue
                    SET queue_status=IF(retry_count + 1 >= max_retries, 'failed', 'pending'),
                        retry_count=retry_count + 1,
                        locked_by=NULL,
                        locked_at=NULL,
                        last_error=%s,
                        updated_at=NOW()
                    WHERE ai_task_id=%s
                    """,
                    (compact_text(error)[:4000], ai_task_id),
                )
            conn.commit()

    def apply_ai_enrichment_results(
        self,
        *,
        paimai_id: str,
        source_platform: str,
        asset_group: str,
        common_values: dict[str, Any],
        common_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any],
        special_results: dict[str, dict[str, Any]],
        debt_details: list[dict[str, Any]] | None = None,
        ip_details: list[dict[str, Any]] | None = None,
    ) -> None:
        with mysql_connection(self.config) as conn:
            with conn.cursor() as cur:
                item_id = int(_get_item_id(cur, paimai_id, source_platform=source_platform))
                cur.execute(
                    """
                    SELECT asset_type, project_name, data_source, start_price_display, final_price_display
                    FROM auction_items
                    WHERE item_id=%s
                    """,
                    (item_id,),
                )
                existing_item = cur.fetchone() or {}
                common_update = self._ai_common_update_row(
                    common_values,
                    common_results,
                    special_values,
                    asset_group,
                    existing_item=existing_item,
                )
                update_row_columns(
                    cur,
                    "auction_items",
                    "item_id",
                    item_id,
                    common_update,
                    mysql_column_types(cur, "auction_items"),
                )
                self._upsert_selected_field_extractions_cur(
                    cur,
                    item_id=item_id,
                    namespace="common",
                    asset_group=asset_group,
                    fields=tuple(
                        field
                        for field in COMMON_FIELDS
                        if field.key in common_values and field.key not in {"attachments_json"}
                    ),
                    values=common_values,
                    field_results=common_results,
                )
                if asset_group in V2_SPECIAL_TABLES and special_values:
                    table = V2_SPECIAL_TABLES[asset_group]
                    special_row = build_v2_special_row(item_id=item_id, asset_group=asset_group, values=special_values)
                    special_row = {key: value for key, value in special_row.items() if key == "item_id" or value is not None}
                    if len(special_row) > 1:
                        upsert_rows(cur, table, [special_row], mysql_column_types(cur, table))
                    self._upsert_selected_field_extractions_cur(
                        cur,
                        item_id=item_id,
                        namespace="special",
                        asset_group=asset_group,
                        fields=tuple(field for field in SPECIAL_FIELDS.get(asset_group, ()) if field.key in special_values),
                        values=special_values,
                        field_results=special_results,
                    )
            conn.commit()
        if debt_details:
            self.upsert_debt_details(paimai_id=paimai_id, source_platform=source_platform, details=debt_details)
        if ip_details:
            self.upsert_ip_details(paimai_id=paimai_id, source_platform=source_platform, details=ip_details)

    def export_csvs(self, output_dir: Path) -> dict[str, Path]:
        return {}

    def _ai_common_update_row(
        self,
        common_values: dict[str, Any],
        common_results: dict[str, dict[str, Any]],
        special_values: dict[str, Any],
        asset_group: str,
        existing_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        existing_item = existing_item or {}
        direct_columns = {
            "asset_type": "asset_type",
            "asset_location": "asset_location",
            "project_status": "project_status",
            "project_status_basis": "project_status_basis",
            "auction_stage": "auction_stage",
            "data_source": "data_source",
            "project_name": "project_name",
            "signup_start_time": "signup_start_time",
            "signup_end_time": "signup_end_time",
            "disposal_party": "disposal_party",
            "disposal_agency": "disposal_agency",
            "contact_info": "contact_info",
            "special_notice": "special_notice",
        }
        for field_key, column in direct_columns.items():
            value = common_values.get(field_key)
            if not compact_text(value):
                continue
            if field_key in {"asset_type", "project_name", "data_source"} and compact_text(existing_item.get(column)):
                continue
            if field_key in {"signup_start_time", "signup_end_time"}:
                row[column] = datetime_to_db(value)
            else:
                row[column] = _blank_to_none(value)
        if compact_text(common_values.get("bid_records_json")):
            row["bid_records_json"] = _json_or_none(_parse_json_maybe(common_values.get("bid_records_json")) or [])
            row["bid_records_count"] = _bid_count(common_values.get("bid_records_json"))
        start_display = _blank_to_none(common_values.get("start_price_raw"))
        if start_display:
            row["start_price_display"] = start_display
            row["start_price_amount"] = _money_decimal(start_display)
            if not _blank_to_none(common_values.get("final_price_raw")) and not _blank_to_none(
                existing_item.get("final_price_display")
            ):
                row["final_price_display"] = start_display
                row["final_price_amount"] = _money_decimal(start_display)
                row["price_basis"] = "start_price_fallback_after_ai"
        final_display = _blank_to_none(common_values.get("final_price_raw"))
        if final_display:
            row["final_price_display"] = final_display
            row["final_price_amount"] = _money_decimal(final_display)
            row["price_basis"] = "ai_extracted_effective_price"
        defects = _blank_to_none(special_values.get("disclosed_defects"))
        if defects:
            row["disclosed_defects"] = defects
        if asset_group != "debt":
            right_holder = _blank_to_none(
                special_values.get("right_holder")
                or special_values.get("original_right_holder")
                or common_values.get("right_holder")
            )
            if right_holder:
                row["right_holder"] = right_holder
        assessment_text = _blank_to_none(common_values.get("assessment_price_time"))
        if assessment_text:
            result = common_results.get("assessment_price_time", {})
            if _valid_assessment_text(
                assessment_text,
                source_excerpt=result.get("source_excerpt"),
                source_payload_type=compact_text(result.get("source_payload_type")) or "",
                source_path=compact_text(result.get("source_path")) or "",
            ):
                row["assessment_price_amount"] = _money_decimal(assessment_text)
                row["assessment_price_display"] = assessment_text
                row["assessment_price_basis"] = _assessment_basis(assessment_text)
                row["assessment_date"] = (
                    date_to_db(assessment_text) if has_assessment_date_text(assessment_text) else None
                )
        return row

    def _upsert_selected_field_extractions_cur(
        self,
        cur,
        *,
        item_id: int,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        rows: list[dict[str, Any]] = []
        for field in fields:
            value = values.get(field.key)
            value_text = compact_text(value)
            if not value_text:
                continue
            result = field_results.get(field.key, {})
            typed_values = typed_field_extraction_values(field.key, value)
            rows.append(
                {
                    "item_id": item_id,
                    "field_namespace": namespace,
                    "asset_group": asset_group,
                    "field_key": field.key,
                    "field_label": field.label,
                    "display_value": value_text,
                    "normalized_text": value_text,
                    "numeric_value": typed_values.get("numeric_value"),
                    "date_value": typed_values.get("date_value"),
                    "datetime_value": typed_values.get("datetime_value"),
                    "value_unit": "元" if typed_values.get("value_type") == "money" else None,
                    "method": result.get("method") or "ai",
                    "source_payload_type": result.get("source_payload_type", "ai_extraction"),
                    "source_tab": _payload_tab(result.get("source_payload_type", "ai_extraction")),
                    "source_path": result.get("source_path", "llm_batch"),
                    "source_excerpt": compact_text(result.get("source_excerpt")),
                    "confidence": float(result.get("confidence", 0.75)),
                    "status": "extracted",
                    "is_selected": 1,
                    "missing_reason": "",
                }
            )
        if not rows:
            return
        keys = [row["field_key"] for row in rows]
        cur.execute(
            f"""
            UPDATE field_extractions
            SET is_selected=0
            WHERE item_id=%s AND field_namespace=%s AND asset_group=%s
              AND field_key IN ({qmarks(len(keys))})
            """,
            [item_id, namespace, asset_group, *keys],
        )
        cur.execute(
            f"""
            DELETE FROM field_extractions
            WHERE item_id=%s AND field_namespace=%s AND asset_group=%s
              AND method IN ('ai', 'ai_derived', 'ai_async')
              AND field_key IN ({qmarks(len(keys))})
            """,
            [item_id, namespace, asset_group, *keys],
        )
        upsert_rows(cur, "field_extractions", rows, mysql_column_types(cur, "field_extractions"))

    def _upsert_dedup_index_cur(
        self,
        cur,
        *,
        item_id: int,
        row: dict[str, Any],
        common_values: dict[str, Any],
    ) -> None:
        dedup_hash = compact_text(row.get("dedup_hash"))
        if not dedup_hash:
            return
        identity_basis = {
            "project_name": common_values.get("project_name"),
            "asset_location": common_values.get("asset_location"),
            "source_item_id": row.get("source_item_id"),
        }
        upsert_rows(
            cur,
            "asset_dedup_index",
            [
                {
                    "item_id": item_id,
                    "source_platform": row.get("source_platform") or "jd",
                    "source_item_id": row.get("source_item_id"),
                    "dedup_hash": dedup_hash,
                    "asset_group": row.get("asset_group"),
                    "project_name": row.get("project_name"),
                    "asset_location": row.get("asset_location"),
                    "identity_basis_json": safe_json_dumps(identity_basis),
                    "duplicate_status": "unique",
                }
            ],
            mysql_column_types(cur, "asset_dedup_index"),
        )

    def _refresh_debt_main_from_details_cur(self, cur, item_id: int) -> None:
        cur.execute(
            """
            SELECT
              GROUP_CONCAT(DISTINCT debtor_name ORDER BY detail_index SEPARATOR '、') AS debtor_names,
              SUM(principal_balance_amount) AS principal_balance_amount,
              SUM(interest_balance_amount) AS interest_balance_amount,
              SUM(claim_total_amount) AS claim_total_amount,
              COUNT(*) AS household_count
            FROM asset_debt_details
            WHERE item_id=%s
            """,
            (item_id,),
        )
        summary = cur.fetchone() or {}
        if not summary.get("household_count"):
            return
        updates = {
            "item_id": item_id,
            "debtor_names": summary.get("debtor_names"),
            "main_debtor_name": (summary.get("debtor_names") or "").split("、", 1)[0] or None,
            "principal_balance_amount": summary.get("principal_balance_amount"),
            "interest_balance_amount": summary.get("interest_balance_amount"),
            "claim_total_amount": summary.get("claim_total_amount"),
            "household_count": summary.get("household_count"),
        }
        upsert_rows(cur, "asset_debt", [updates], mysql_column_types(cur, "asset_debt"))

    def _upsert_field_extractions_cur(
        self,
        cur,
        *,
        item_id: int,
        namespace: str,
        asset_group: str,
        fields: tuple[FieldDef, ...],
        values: dict[str, Any],
        field_results: dict[str, dict[str, Any]],
    ) -> None:
        cur.execute(
            "DELETE FROM field_extractions WHERE item_id=%s AND field_namespace=%s AND asset_group=%s",
            (item_id, namespace, asset_group),
        )
        rows: list[dict[str, Any]] = []
        for field in fields:
            value = values.get(field.key)
            result = field_results.get(field.key, {})
            value_text = compact_text(value)
            status = result.get("status") or ("extracted" if value_text else "missing")
            if status == "missing_on_page":
                status = "missing"
            typed_values = typed_field_extraction_values(field.key, value)
            rows.append(
                {
                    "item_id": item_id,
                    "field_namespace": namespace,
                    "asset_group": asset_group,
                    "field_key": field.key,
                    "field_label": field.label,
                    "display_value": value_text,
                    "normalized_text": value_text,
                    "numeric_value": typed_values.get("numeric_value"),
                    "date_value": typed_values.get("date_value"),
                    "datetime_value": typed_values.get("datetime_value"),
                    "value_unit": "元" if typed_values.get("value_type") == "money" else None,
                    "method": result.get("method") or ("not_found" if not value_text else "api_or_html"),
                    "source_payload_type": result.get("source_payload_type", ""),
                    "source_tab": _payload_tab(result.get("source_payload_type", "")),
                    "source_path": result.get("source_path", ""),
                    "source_excerpt": compact_text(result.get("source_excerpt")),
                    "confidence": float(result.get("confidence", 0.95 if value_text else 0.0)),
                    "status": status,
                    "is_selected": 1 if value_text else 0,
                    "missing_reason": "" if value_text else result.get("missing_reason", "页面或接口未提供该字段"),
                }
            )
        upsert_rows(cur, "field_extractions", rows, mysql_column_types(cur, "field_extractions"))


def table_comments_mysql(cur, table: str) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT COLUMN_NAME AS column_name, COLUMN_COMMENT AS comment
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table,),
    )
    return {
        row["column_name"]: {
            "column_name": row["column_name"],
            "comment": row.get("comment") or "",
        }
        for row in cur.fetchall()
    }


def _raw_payload_map(cur, item_id: int) -> dict[str, Any]:
    cur.execute(
        "SELECT * FROM raw_payloads WHERE item_id=%s ORDER BY payload_id",
        (item_id,),
    )
    raw: dict[str, Any] = {}
    for row in cur.fetchall():
        payload_type = row["payload_type"]
        if payload_type.endswith("_json"):
            raw[payload_type] = row.get("payload_json")
        else:
            raw[payload_type] = row.get("payload_text")
    return raw


def parse_source_item_ref(value: Any) -> tuple[str | None, str]:
    text = str(value or "").strip()
    if ":" in text:
        platform, source_item_id = text.split(":", 1)
        platform = platform.strip()
        source_item_id = source_item_id.strip()
        if platform and source_item_id:
            return platform, source_item_id
    return None, text


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
        like = f"%{filters['q']}%"
        clauses.append(
            """
            (
              c.project_name LIKE %s
              OR c.asset_location LIKE %s
              OR c.disposal_party LIKE %s
              OR EXISTS (
                SELECT 1 FROM field_extractions fx
                WHERE fx.item_id = c.item_id
                  AND (fx.display_value LIKE %s OR fx.source_excerpt LIKE %s)
              )
            )
            """
        )
        params.extend([like, like, like, like, like])
    if filters.get("issue") == "missing":
        clauses.append("EXISTS (SELECT 1 FROM field_extractions fm WHERE fm.item_id=c.item_id AND fm.status!='extracted')")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  CONCAT(c.source_platform, ':', c.source_item_id) AS paimai_id,
                  c.source_platform,
                  c.source_item_id,
                  c.asset_group,
                  c.asset_group_label,
                  c.source_category_id AS jd_category_id,
                  c.source_category_name AS jd_category_name,
                  c.project_name,
                  c.asset_location,
                  c.project_status,
                  c.start_price_display AS start_price_raw,
                  COALESCE(NULLIF(c.final_price_display, ''), NULLIF(c.start_price_display, '')) AS final_price_raw,
                  c.disposal_party,
                  COUNT(fe.field_key) AS total_fields,
                  SUM(CASE WHEN fe.status='extracted' THEN 1 ELSE 0 END) AS extracted_fields,
                  SUM(CASE WHEN fe.status!='extracted' THEN 1 ELSE 0 END) AS issue_fields
                FROM auction_items c
                LEFT JOIN field_extractions fe ON fe.item_id = c.item_id
                {where}
                GROUP BY c.item_id
                ORDER BY c.source_platform, CAST(c.source_category_id AS UNSIGNED), c.source_item_id
                """,
                params,
            )
            items = cur.fetchall()
            cur.execute(
                "SELECT asset_group, asset_group_label, COUNT(*) AS count "
                "FROM auction_items GROUP BY asset_group, asset_group_label ORDER BY asset_group"
            )
            asset_groups = cur.fetchall()
            cur.execute(
                "SELECT DISTINCT project_status FROM auction_items "
                "WHERE project_status IS NOT NULL AND project_status!='' ORDER BY project_status"
            )
            statuses = [row["project_status"] for row in cur.fetchall()]
    return {"items": items, "asset_groups": asset_groups, "statuses": statuses}


def get_item_detail_mysql(config: MySQLConfig, paimai_id: str) -> dict[str, Any]:
    with mysql_connection(config) as conn:
        with conn.cursor() as cur:
            platform_hint, source_item_id = parse_source_item_ref(paimai_id)
            if platform_hint:
                item_where = "c.source_platform=%s AND c.source_item_id=%s"
                item_params: tuple[Any, ...] = (platform_hint, source_item_id)
                item_order = ""
            else:
                item_where = "c.source_item_id=%s"
                item_params = (source_item_id,)
                if source_item_id.isdigit():
                    item_order = "ORDER BY CASE WHEN c.source_platform='jd' THEN 0 ELSE 1 END, c.item_id DESC"
                else:
                    item_order = "ORDER BY c.item_id DESC"
            cur.execute(
                f"""
                SELECT
                  c.*,
                  CONCAT(c.source_platform, ':', c.source_item_id) AS paimai_id,
                  c.source_category_id AS jd_category_id,
                  c.source_category_name AS jd_category_name,
                  c.start_price_display AS start_price_raw,
                  COALESCE(NULLIF(c.final_price_display, ''), NULLIF(c.start_price_display, '')) AS final_price_raw
                FROM auction_items c
                WHERE {item_where}
                {item_order}
                LIMIT 1
                """,
                item_params,
            )
            item = cur.fetchone()
            if item is None:
                raise KeyError(f"找不到标的：{paimai_id}")
            item_id = int(item["item_id"])
            group = item["asset_group"]
            special_table = V2_SPECIAL_TABLES[group]
            cur.execute(f"SELECT * FROM `{special_table}` WHERE item_id=%s", (item_id,))
            special_row = cur.fetchone() or {}
            cur.execute(
                """
                SELECT *
                FROM item_resources
                WHERE item_id=%s
                ORDER BY resource_type, resource_role, resource_id
                """,
                (item_id,),
            )
            resources = cur.fetchall()
            debt_details: list[dict[str, Any]] = []
            if group == "debt":
                cur.execute(
                    """
                    SELECT
                      *,
                      CAST(principal_balance_amount AS CHAR) AS principal_balance,
                      CAST(interest_balance_amount AS CHAR) AS interest_balance,
                      CAST(claim_total_amount AS CHAR) AS claim_total,
                      NULL AS recovery_fee
                    FROM asset_debt_details
                    WHERE item_id=%s
                    ORDER BY detail_index
                    """,
                    (item_id,),
                )
                debt_details = cur.fetchall()
            ip_details: list[dict[str, Any]] = []
            if group == "ip":
                cur.execute(
                    """
                    SELECT *, right_status AS status
                    FROM asset_ip_details
                    WHERE item_id=%s
                    ORDER BY detail_index
                    """,
                    (item_id,),
                )
                ip_details = cur.fetchall()
            raw = _raw_payload_map(cur, item_id)
            duplicates: list[dict[str, Any]] = []
            if item.get("dedup_hash"):
                cur.execute(
                    """
                    SELECT d.source_platform, d.source_item_id,
                           CONCAT(d.source_platform, ':', d.source_item_id) AS paimai_id,
                           d.asset_group, d.project_name, d.asset_location, d.updated_at
                    FROM asset_dedup_index d
                    JOIN auction_items i ON i.item_id = d.item_id
                    WHERE d.dedup_hash = %s
                      AND d.item_id != %s
                    ORDER BY d.updated_at DESC
                    """,
                    (item["dedup_hash"], item_id),
                )
                duplicates = cur.fetchall()
            common_comments = table_comments_mysql(cur, "auction_items")
            special_comments = table_comments_mysql(cur, special_table)

            def load_fields(namespace: str, asset_group_filter: str, comments: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
                cur.execute(
                    """
                    SELECT fe.*, fc.export_order
                    FROM (
                        SELECT *,
                            ROW_NUMBER() OVER (
                                PARTITION BY field_key
                                ORDER BY is_selected DESC, extraction_id DESC
                            ) AS rn
                        FROM field_extractions
                        WHERE item_id = %s AND field_namespace = %s AND asset_group = %s
                    ) fe
                    LEFT JOIN field_catalog fc
                      ON fc.field_namespace = fe.field_namespace
                     AND fc.asset_group = CASE WHEN fe.field_namespace='common' THEN 'ALL' ELSE fe.asset_group END
                     AND fc.field_key = fe.field_key
                    WHERE fe.rn = 1
                    ORDER BY COALESCE(fc.export_order, 999), fe.field_key
                    """,
                    (item_id, namespace, asset_group_filter),
                )
                fields = []
                for row in cur.fetchall():
                    comment = comments.get(row["field_key"])
                    fields.append(
                        {
                            "key": row["field_key"],
                            "label": row["field_label"],
                            "comment": comment["comment"] if comment and comment.get("comment") else "字段说明未配置。",
                            "value": row["display_value"],
                            "raw_value": row["display_value"],
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

            common_fields = load_fields("common", group, common_comments)
            special_fields = load_fields("special", group, special_comments)

    return {
        "item": item,
        "special_row": special_row,
        "raw": raw,
        "common_fields": common_fields,
        "special_fields": special_fields,
        "resources": resources,
        "debt_details": debt_details,
        "ip_details": ip_details,
        "duplicates": duplicates,
        "asset_group_label": ASSET_GROUP_LABELS.get(group, group),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步京东拍卖 SQLite 数据到 MySQL")
    parser.add_argument("--sqlite", type=Path, default=Path("outputs") / "v2_multi_preview" / "jd_auction.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default="auction_data")
    parser.add_argument("--no-clean-assessment", action="store_true", help="不清理明显误提取的评估价")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = MySQLConfig(args.host, args.port, args.user, args.password, args.database)
    imported = import_sqlite_to_mysql(args.sqlite, config, clean_assessment=not args.no_clean_assessment)
    print(json.dumps(imported, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
