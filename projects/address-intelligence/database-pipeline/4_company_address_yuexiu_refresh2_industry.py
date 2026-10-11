import os

from sqlalchemy import create_engine, text
import logging
import time


# ======================== 参数定义 ========================
DB_TARGET = os.environ['SR_TARGET_DSN']
DB_LAKE = os.environ['SR_SOURCE_DSN']

INPUT_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly"
BASIC_INFO_TABLE = "fintech_data_lake.jwy_ods__jwy_dwh__company_basic_dimension_info__v"
OUTPUT_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly_refresh2"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASIC_INFO_FIELDS = [
    {
        "name": "industry_l1_name",
        "ddl": "varchar(65533) NULL COMMENT \"一级行业名称\"",
        "value_type": "string",
    },
    {
        "name": "industry_l2_name",
        "ddl": "varchar(65533) NULL COMMENT \"二级行业名称\"",
        "value_type": "string",
    },
    {
        "name": "industry_l3_name",
        "ddl": "varchar(65533) NULL COMMENT \"三级行业名称\"",
        "value_type": "string",
    },
    {
        "name": "industry_l4_name",
        "ddl": "varchar(65533) NULL COMMENT \"四级行业名称\"",
        "value_type": "string",
    },
    {
        "name": "abnormal_stay_if_current",
        "ddl": "int(11) NULL COMMENT \"当前经营异常条数\"",
        "value_type": "non_null",
    },
    {
        "name": "law_violated_cnt_current",
        "ddl": "int(11) NULL COMMENT \"当前严重违法条数\"",
        "value_type": "non_null",
    },
    {
        "name": "capital_freeze_cnt_current",
        "ddl": "int(11) NULL COMMENT \"当前股权冻结条数\"",
        "value_type": "non_null",
    },
    {
        "name": "is_sme",
        "ddl": "tinyint(4) NULL COMMENT \"是否小微企业，0否，1是（确认）\"",
        "value_type": "non_null",
    },
    {
        "name": "is_valid",
        "ddl": "tinyint(4) NULL COMMENT \"有效状态,0无效，1有效\"",
        "value_type": "non_null",
    },
    {
        "name": "annual_report_insured_number",
        "ddl": "varchar(65533) NULL COMMENT \"最近一年年报参保人数(确认)\"",
        "value_type": "string",
    },
    {
        "name": "sale_valid_last_date",
        "ddl": "varchar(65533) NULL COMMENT \"最近一张销项有效票开票日期\"",
        "value_type": "string",
    },
    {
        "name": "income_valid_last_date",
        "ddl": "varchar(65533) NULL COMMENT \"最近一张进项有效票开票日期\"",
        "value_type": "string",
    },
    {
        "name": "capital_num",
        "ddl": "decimal(20,2) NULL COMMENT \"注册资本（万）\"",
        "value_type": "non_null",
    },
    {
        "name": "establish_date",
        "ddl": "varchar(65533) NULL COMMENT \"注册日期\"",
        "value_type": "string",
    },
    {
        "name": "oper_change_last_date",
        "ddl": "varchar(65533) NULL COMMENT \"最近一次法人变更日期\"",
        "value_type": "string",
    },
]


# ======================== 工具函数 ========================
def format_elapsed(seconds):
    """把秒数格式化成更适合终端阅读的耗时。"""
    seconds = int(seconds)
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{remaining_minutes}分{remaining_seconds}秒"
    if remaining_minutes:
        return f"{remaining_minutes}分{remaining_seconds}秒"
    return f"{remaining_seconds}秒"


def log_step(step_no, total_steps, message):
    """统一打印步骤进度。"""
    logger.info(f"[{step_no}/{total_steps}] {message}")


def get_engines():
    """创建目标库和数据湖引擎并测试连接。"""
    engine_target = create_engine(DB_TARGET)
    engine_lake = create_engine(DB_LAKE)
    try:
        with engine_target.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ 目标库连接成功")

        with engine_lake.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ 数据湖连接成功")
    except Exception as e:
        logger.error(f"❌ 数据库连接失败：{e}")
        raise
    return engine_target, engine_lake


def fetch_scalar(engine, sql):
    """执行单值查询。"""
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        row = result.fetchone()
    return row[0] if row else None


def log_table_stats(engine, table_name, prefix):
    """打印总行数、空 credit_no 行数、重复 credit_no 组数。"""
    total_rows = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(*)
        FROM {table_name}
        """,
    )
    empty_credit_no_rows = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(*)
        FROM {table_name}
        WHERE credit_no IS NULL OR TRIM(credit_no) = ''
        """,
    )
    duplicated_credit_no_groups = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT credit_no
            FROM {table_name}
            WHERE credit_no IS NOT NULL AND TRIM(credit_no) <> ''
            GROUP BY credit_no
            HAVING COUNT(*) > 1
        ) t
        """,
    )

    logger.info(f"{prefix}总行数: {total_rows}")
    logger.info(f"{prefix}credit_no 为空行数: {empty_credit_no_rows}")
    logger.info(f"{prefix}重复 credit_no 组数: {duplicated_credit_no_groups}")


def log_basic_info_stats(engine):
    """打印基础维表中可用于关联的补充字段情况。"""
    valid_credit_no_rows = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(*)
        FROM {BASIC_INFO_TABLE}
        WHERE credit_no IS NOT NULL
          AND TRIM(credit_no) <> ''
        """,
    )
    distinct_credit_no_rows = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(DISTINCT credit_no)
        FROM {BASIC_INFO_TABLE}
        WHERE credit_no IS NOT NULL
          AND TRIM(credit_no) <> ''
        """,
    )

    logger.info(f"基础维表可关联 credit_no 行数: {valid_credit_no_rows}")
    logger.info(f"基础维表可关联 credit_no 去重数: {distinct_credit_no_rows}")
    log_basic_info_field_stats(engine, BASIC_INFO_TABLE, "基础维表")


def table_column_exists(engine, table_name, column_name):
    """检查表字段是否存在，兼容 CREATE TABLE IF NOT EXISTS 的旧表结构。"""
    schema_name, pure_table_name = table_name.split(".", 1)
    sql = text(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {
            "schema_name": schema_name,
            "table_name": pure_table_name,
            "column_name": column_name,
        }).fetchone()
    return bool(row and row[0])


def fetch_table_column_type(engine, table_name, column_name):
    """查询表字段类型，用于识别旧表结构和当前脚本定义是否一致。"""
    schema_name, pure_table_name = table_name.split(".", 1)
    sql = text(
        """
        SELECT COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = :schema_name
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {
            "schema_name": schema_name,
            "table_name": pure_table_name,
            "column_name": column_name,
        }).fetchone()
    return row[0] if row else None


def normalize_column_type(column_type):
    """统一字段类型格式，避免 decimal(20, 2) 和 decimal(20,2) 这类空格差异。"""
    return column_type.lower().replace(" ", "") if column_type else None


def validate_basic_info_columns(engine):
    """提前校验基础维表是否包含全部待补充字段。"""
    missing_columns = [
        field["name"]
        for field in BASIC_INFO_FIELDS
        if not table_column_exists(engine, BASIC_INFO_TABLE, field["name"])
    ]
    if missing_columns:
        raise ValueError(
            f"{BASIC_INFO_TABLE} 缺少字段: {', '.join(missing_columns)}"
        )


def extract_expected_column_type(column_definition):
    """从 ADD COLUMN 字段定义里取出期望字段类型。"""
    return column_definition.split(" NULL ", 1)[0]


def build_non_empty_condition(column_name):
    """生成字段非空统计条件。"""
    field = next((field for field in BASIC_INFO_FIELDS if field["name"] == column_name), None)
    if field and field["value_type"] == "string":
        return f"{column_name} IS NOT NULL AND TRIM({column_name}) <> ''"
    return f"{column_name} IS NOT NULL"


def build_basic_info_agg_expr(column_name):
    """生成基础维表同一 credit_no 聚合字段表达式。"""
    field = next((field for field in BASIC_INFO_FIELDS if field["name"] == column_name), None)
    if field and field["value_type"] == "string":
        return f"""MAX(
                    CASE
                        WHEN {column_name} IS NOT NULL
                         AND TRIM({column_name}) <> '' THEN {column_name}
                        ELSE NULL
                    END
                ) AS {column_name}"""
    return f"MAX({column_name}) AS {column_name}"


def log_basic_info_field_stats(engine, table_name, prefix):
    """打印基础维表补充字段在指定表中的非空情况。"""
    for field in BASIC_INFO_FIELDS:
        column_name = field["name"]
        rows = fetch_scalar(
            engine,
            f"""
            SELECT COUNT(*)
            FROM {table_name}
            WHERE {build_non_empty_condition(column_name)}
            """,
        )
        logger.info(f"{prefix}{column_name} 非空行数: {rows}")


# ======================== 核心逻辑 ========================
def create_output_table(engine):
    """创建补充基础维表字段后的支线表。"""
    logger.info(f"开始创建/检查输出表: {OUTPUT_TABLE}")
    basic_info_column_ddl = ",\n        ".join(
        f"`{field['name']}` {field['ddl']}" for field in BASIC_INFO_FIELDS
    )
    create_sql = text(f"""
        CREATE TABLE IF NOT EXISTS {OUTPUT_TABLE} (
        `credit_no` varchar(255) NULL COMMENT "统一社会信用代码",
        `company_name` varchar(255) NULL COMMENT "公司名称",
        `company_address` varchar(500) NULL COMMENT "公司地址",
        `province` varchar(255) NULL COMMENT "省",
        `city` varchar(255) NULL COMMENT "市",
        `district` varchar(255) NULL COMMENT "区县",
        `town_street` varchar(255) NULL COMMENT "镇街",
        `road_name` varchar(255) NULL COMMENT "道路名",
        `road_no` varchar(255) NULL COMMENT "门牌号",
        `entity_name` varchar(255) NULL COMMENT "实体名",
        `additional_information` varchar(500) NULL COMMENT "剩余信息",
        `location_label` varchar(255) NULL COMMENT "位置标签",
        {basic_info_column_ddl}
        ) ENGINE=OLAP
        DUPLICATE KEY(`credit_no`, `company_name`, `company_address`)
        DISTRIBUTED BY HASH(`credit_no`) BUCKETS 10
        PROPERTIES (
        "compression" = "LZ4",
        "fast_schema_evolution" = "true",
        "replicated_storage" = "true",
        "replication_num" = "1"
        );
    """)

    with engine.begin() as conn:
        conn.execute(create_sql)
    logger.info(f"✅ 输出表已创建/存在: {OUTPUT_TABLE}")


def ensure_output_table_columns(engine):
    """确保输出表包含新增的基础维表字段。"""
    logger.info("开始检查输出表字段")
    columns = [(field["name"], field["ddl"]) for field in BASIC_INFO_FIELDS]

    with engine.begin() as conn:
        for column_name, column_definition in columns:
            if table_column_exists(engine, OUTPUT_TABLE, column_name):
                actual_type = fetch_table_column_type(engine, OUTPUT_TABLE, column_name)
                expected_type = extract_expected_column_type(column_definition)
                if normalize_column_type(actual_type) != normalize_column_type(expected_type):
                    logger.warning(
                        f"字段已存在但类型不一致: {column_name}, "
                        f"当前类型={actual_type}, 脚本期望类型={expected_type}"
                    )
                logger.info(f"字段已存在: {column_name}")
                continue
            conn.execute(text(
                f"ALTER TABLE {OUTPUT_TABLE} ADD COLUMN `{column_name}` {column_definition}"
            ))
            logger.info(f"✅ 已新增字段: {column_name}")


def truncate_output_table(engine):
    """清空输出表，保证脚本可重复运行。"""
    logger.info(f"开始清空输出表: {OUTPUT_TABLE}")
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {OUTPUT_TABLE}"))
    logger.info(f"✅ 输出表已清空: {OUTPUT_TABLE}")


def write_output_table(engine):
    """把 refresh 全量结果左连基础维表，补充基础维表字段后写入 refresh2。"""
    input_rows = fetch_scalar(
        engine,
        f"""
        SELECT COUNT(*)
        FROM {INPUT_TABLE}
        """,
    )
    logger.info(f"预计写入输出表行数: {input_rows}")
    basic_info_insert_columns = ",\n            ".join(
        field["name"] for field in BASIC_INFO_FIELDS
    )
    basic_info_select_columns = ",\n            ".join(
        f"b.{field['name']}" for field in BASIC_INFO_FIELDS
    )
    basic_info_agg_columns = ",\n                ".join(
        build_basic_info_agg_expr(field["name"]) for field in BASIC_INFO_FIELDS
    )

    insert_sql = text(f"""
        INSERT INTO {OUTPUT_TABLE} (
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information,
            location_label,
            {basic_info_insert_columns}
        )
        SELECT
            r.credit_no,
            r.company_name,
            r.company_address,
            r.province,
            r.city,
            r.district,
            r.town_street,
            r.road_name,
            r.road_no,
            r.entity_name,
            r.additional_information,
            r.location_label,
            {basic_info_select_columns}
        FROM {INPUT_TABLE} r
        LEFT JOIN (
            SELECT
                credit_no,
                {basic_info_agg_columns}
            FROM {BASIC_INFO_TABLE}
            WHERE credit_no IS NOT NULL
              AND TRIM(credit_no) <> ''
            GROUP BY credit_no
        ) b
            ON r.credit_no = b.credit_no
    """)

    logger.info(f"开始写入输出表: {OUTPUT_TABLE}")
    logger.info(f"规则: {INPUT_TABLE} LEFT JOIN {BASIC_INFO_TABLE}，按 credit_no 补充基础维表字段")
    with engine.begin() as conn:
        conn.execute(insert_sql)
    logger.info(f"✅ 输出表写入完成: {OUTPUT_TABLE}")


# ======================== 主函数 ========================
def main():
    total_steps = 8
    started_at = time.time()
    try:
        logger.info("=" * 80)
        logger.info("开始执行 refresh2 基础维表字段补充脚本")
        log_step(1, total_steps, "连接目标库和数据湖")
        engine_target, engine_lake = get_engines()

        logger.info(f"输入表: {INPUT_TABLE}")
        logger.info(f"基础维表: {BASIC_INFO_TABLE}")
        logger.info(f"输出表: {OUTPUT_TABLE}")
        logger.info("规则: 保留输入表全部结果，通过 credit_no 补充基础维表字段")

        validate_basic_info_columns(engine_lake)

        log_step(2, total_steps, "统计输入表")
        log_table_stats(engine_target, INPUT_TABLE, "输入表")

        log_step(3, total_steps, "统计基础维表")
        log_basic_info_stats(engine_lake)

        log_step(4, total_steps, "创建/检查输出表")
        create_output_table(engine_target)

        log_step(5, total_steps, "检查输出表字段")
        ensure_output_table_columns(engine_target)

        log_step(6, total_steps, "清空输出表")
        truncate_output_table(engine_target)

        log_step(7, total_steps, "写入输出表")
        write_output_table(engine_target)

        log_step(8, total_steps, "统计输出表")
        log_table_stats(engine_target, OUTPUT_TABLE, "输出表")
        log_basic_info_field_stats(engine_target, OUTPUT_TABLE, "输出表")
        logger.info(f"✅ 全部完成，总耗时: {format_elapsed(time.time() - started_at)}")
        logger.info("=" * 80)
    except Exception as e:
        logger.error(f"❌ 已失败，运行耗时: {format_elapsed(time.time() - started_at)}")
        logger.error(f"程序异常退出：{e}")
        raise


if __name__ == "__main__":
    main()
