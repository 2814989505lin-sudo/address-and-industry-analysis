from __future__ import annotations
import os


from sqlalchemy import create_engine, text


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

DB_TARGET = os.environ['SR_TARGET_DSN']

CLUSTER_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly"
NAMES_STAGE_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly_scope_names_stage"
LOOKUP_STAGE_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly_scope_lookup_stage"
RESULT_STAGE_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly_scope_stage"
LOOKUP_TABLE = "fintech_data_lake.jwy_ods__mongo_pazxdb__company_base__si"

SCOPE_COLUMN = "business_scope"
LOOKUP_SCOPE_COLUMN = "business_scope"


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def clean_text(value):
    """把数据库值统一成干净字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def get_engine():
    """统一创建数据库连接。"""
    return create_engine(DB_TARGET)


def has_business_scope_column(table_name):
    """判断指定表是否已经存在 business_scope 列。"""
    sql = text(f"SHOW COLUMNS FROM {table_name}")

    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(sql)
        for row in result:
            if clean_text(row[0]) == SCOPE_COLUMN:
                return True
    return False


def ensure_business_scope_column():
    """确保 clustered 表存在 business_scope 列。"""
    if has_business_scope_column(CLUSTER_TABLE):
        print(f"列 {SCOPE_COLUMN} 已存在，跳过建列。")
        return

    alter_sql = text(
        f"""
        ALTER TABLE {CLUSTER_TABLE}
        ADD COLUMN {SCOPE_COLUMN} VARCHAR(5000) COMMENT '经营范围'
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(alter_sql)

    print(f"已新增列: {CLUSTER_TABLE}.{SCOPE_COLUMN}")


def create_stage_tables():
    """创建三个中间表，分别承载 company_name 集合、scope lookup 结果和最终 join 结果。"""
    names_stage_sql = f"""
    CREATE TABLE IF NOT EXISTS {NAMES_STAGE_TABLE} (
        company_name VARCHAR(255) COMMENT '公司名称'
    ) ENGINE=OLAP
    DUPLICATE KEY(company_name)
    DISTRIBUTED BY HASH(company_name) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    lookup_stage_sql = f"""
    CREATE TABLE IF NOT EXISTS {LOOKUP_STAGE_TABLE} (
        company_name VARCHAR(255) COMMENT '公司名称',
        business_scope VARCHAR(5000) COMMENT '经营范围'
    ) ENGINE=OLAP
    DUPLICATE KEY(company_name)
    DISTRIBUTED BY HASH(company_name) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    result_stage_sql = f"""
    CREATE TABLE IF NOT EXISTS {RESULT_STAGE_TABLE} (
        credit_no VARCHAR(255) COMMENT '统一社会信用代码',
        company_name VARCHAR(255) COMMENT '公司名称',
        company_address VARCHAR(500) COMMENT '公司地址',
        province VARCHAR(255) COMMENT '省',
        city VARCHAR(255) COMMENT '市',
        district VARCHAR(255) COMMENT '区县',
        town_street VARCHAR(255) COMMENT '镇街',
        road_name VARCHAR(255) COMMENT '道路名',
        road_no VARCHAR(255) COMMENT '门牌号',
        entity_name VARCHAR(255) COMMENT '实体名',
        additional_information VARCHAR(500) COMMENT '剩余信息',
        location_label VARCHAR(255) COMMENT '位置标签',
        business_scope VARCHAR(5000) COMMENT '经营范围'
    ) ENGINE=OLAP
    DUPLICATE KEY(credit_no, company_name, company_address)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(names_stage_sql))
        connection.execute(text(lookup_stage_sql))
        connection.execute(text(result_stage_sql))

    print("已确保中间表存在")


def truncate_table(table_name):
    """清空指定表。"""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_name}"))


def drop_table_if_exists(table_name):
    """删除指定表。"""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


def cleanup_stage_tables():
    """删除所有中间表。"""
    for table_name in (RESULT_STAGE_TABLE, LOOKUP_STAGE_TABLE, NAMES_STAGE_TABLE):
        drop_table_if_exists(table_name)
        print(f"已删除中间表: {table_name}")


def count_rows(table_name, require_scope=False):
    """统计指定表的记录数。"""
    where_clause = ""
    if require_scope:
        where_clause = f"WHERE {SCOPE_COLUMN} IS NOT NULL AND TRIM({SCOPE_COLUMN}) <> ''"

    sql = text(
        f"""
        SELECT COUNT(*) AS row_count
        FROM {table_name}
        {where_clause}
        """
    )

    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(sql).mappings().first()
        return int(row["row_count"]) if row else 0


def populate_names_stage():
    """从 clustered 表抽取去重后的 company_name 集合。"""
    insert_sql = text(
        f"""
        INSERT INTO {NAMES_STAGE_TABLE} (
            company_name
        )
        SELECT
            DISTINCT company_name
        FROM {CLUSTER_TABLE}
        WHERE company_name IS NOT NULL AND TRIM(company_name) <> ''
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(insert_sql)


def populate_lookup_stage():
    """只针对 clustered 中实际出现的 company_name 去 lookup 大表聚合 business_scope。"""
    insert_sql = text(
        f"""
        INSERT INTO {LOOKUP_STAGE_TABLE} (
            company_name,
            business_scope
        )
        SELECT
            lookup_data.company_name,
            MAX(lookup_data.{LOOKUP_SCOPE_COLUMN}) AS business_scope
        FROM {LOOKUP_TABLE} AS lookup_data
        INNER JOIN {NAMES_STAGE_TABLE} AS target_names
            ON lookup_data.company_name = target_names.company_name
        WHERE lookup_data.{LOOKUP_SCOPE_COLUMN} IS NOT NULL
          AND TRIM(lookup_data.{LOOKUP_SCOPE_COLUMN}) <> ''
        GROUP BY lookup_data.company_name
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(insert_sql)


def rebuild_result_stage():
    """用 filtered lookup 结果回拼 clustered 表，生成最终结果 stage。"""
    insert_sql = text(
        f"""
        INSERT INTO {RESULT_STAGE_TABLE} (
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
            business_scope
        )
        SELECT
            clustered.credit_no,
            clustered.company_name,
            clustered.company_address,
            clustered.province,
            clustered.city,
            clustered.district,
            clustered.town_street,
            clustered.road_name,
            clustered.road_no,
            clustered.entity_name,
            clustered.additional_information,
            clustered.location_label,
            lookup_scope.business_scope
        FROM {CLUSTER_TABLE} AS clustered
        LEFT JOIN {LOOKUP_STAGE_TABLE} AS lookup_scope
            ON clustered.company_name = lookup_scope.company_name
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(insert_sql)


def replace_cluster_with_stage():
    """用 stage 表结果整表回填 clustered 表。"""
    insert_sql = text(
        f"""
        INSERT INTO {CLUSTER_TABLE} (
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
            business_scope
        )
        SELECT
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
            business_scope
        FROM {RESULT_STAGE_TABLE}
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {CLUSTER_TABLE}"))
        connection.execute(insert_sql)


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    print(f"开始补充 business_scope: {CLUSTER_TABLE}")

    ensure_business_scope_column()

    source_rows = count_rows(CLUSTER_TABLE)
    print(f"clustered 当前记录数: {source_rows}")

    try:
        create_stage_tables()

        print(f"清空中间表: {NAMES_STAGE_TABLE}")
        truncate_table(NAMES_STAGE_TABLE)
        print(f"清空中间表: {LOOKUP_STAGE_TABLE}")
        truncate_table(LOOKUP_STAGE_TABLE)
        print(f"清空中间表: {RESULT_STAGE_TABLE}")
        truncate_table(RESULT_STAGE_TABLE)

        print("开始抽取 clustered 中的去重 company_name ...")
        populate_names_stage()
        names_rows = count_rows(NAMES_STAGE_TABLE)
        print(f"去重 company_name 数: {names_rows}")

        print("开始对 lookup 大表做受限 join 聚合 ...")
        populate_lookup_stage()
        lookup_rows = count_rows(LOOKUP_STAGE_TABLE, require_scope=True)
        print(f"lookup stage 中命中的非空 business_scope 数: {lookup_rows}")

        print("开始回拼完整 clustered stage ...")
        rebuild_result_stage()
        result_rows = count_rows(RESULT_STAGE_TABLE)
        result_scope_rows = count_rows(RESULT_STAGE_TABLE, require_scope=True)
        print(f"结果 stage 记录数: {result_rows}")
        print(f"结果 stage 中已补 scope 的记录数: {result_scope_rows}")

        print("开始用结果 stage 整表回填 clustered 表 ...")
        replace_cluster_with_stage()

        final_rows = count_rows(CLUSTER_TABLE)
        final_scope_rows = count_rows(CLUSTER_TABLE, require_scope=True)

        print("处理完成")
        print(f"clustered 回填前记录数: {source_rows}")
        print(f"clustered 回填后记录数: {final_rows}")
        print(f"clustered 当前已补 business_scope 的记录数: {final_scope_rows}")
    finally:
        cleanup_stage_tables()


if __name__ == "__main__":
    main()
