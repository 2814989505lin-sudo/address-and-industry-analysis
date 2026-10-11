from __future__ import annotations
import os


from collections import Counter, OrderedDict

from sqlalchemy import create_engine, text


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

DB_TARGET = os.environ['SR_TARGET_DSN']
INPUT_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly_refresh2"
OUTPUT_TABLE = "fintech_stat.company_address_yuexiu_clustered_stat_ly_refresh2"

TOP_ENTITY_COUNT = 15
TOP_INDUSTRY_COUNT = 10
INSERT_BATCH_SIZE = 500
PROGRESS_EVERY_ROWS = 10000


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def clean_text(value):
    """把数据库值清洗成稳定字符串。"""
    if value is None:
        return ""
    text_value = str(value).strip()
    if text_value.lower() == "nan":
        return ""
    return text_value


def get_engine():
    """统一创建数据库引擎。"""
    return create_engine(DB_TARGET)


def log_progress(message):
    """统一打印进程日志，并立即刷新到终端。"""
    print(message, flush=True)


def chunk_rows(rows, chunk_size):
    """把待写入数据切成固定大小的小批次。"""
    for index in range(0, len(rows), chunk_size):
        yield rows[index:index + chunk_size]


def iter_input_rows():
    """流式读取输入表，避免一次性把整表装入内存。"""
    sql = text(
        f"""
        SELECT
            location_label,
            road_name,
            entity_name,
            town_street,
            industry_l2_name,
            industry_l3_name
        FROM {INPUT_TABLE}
        """
    )

    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(sql)
        for row in result.mappings():
            yield row


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------

def recreate_output_table():
    """重建统计结果表，确保旧字段被移除。"""
    log_progress(f"重建统计结果表: {OUTPUT_TABLE}")
    create_sql = f"""
    CREATE TABLE {OUTPUT_TABLE} (
        cluster_name VARCHAR(255) COMMENT '位置标签',
        company_count BIGINT COMMENT '企业数量',
        road_names VARCHAR(5000) COMMENT '去重路名列表',
        top_entity_names VARCHAR(5000) COMMENT '高频实体名称',
        town_streets VARCHAR(5000) COMMENT '去重街道列表',
        top_company_industry_l2_names VARCHAR(5000) COMMENT '基础维表二级行业统计',
        top_company_industry_l3_names VARCHAR(5000) COMMENT '基础维表三级行业统计'
    ) ENGINE=OLAP
    DUPLICATE KEY(cluster_name)
    DISTRIBUTED BY HASH(cluster_name) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {OUTPUT_TABLE}"))
        connection.execute(text(create_sql))


def insert_rows(rows):
    """把统计结果分批写入输出表。"""
    if not rows:
        log_progress("统计结果为空，跳过写入")
        return

    insert_sql = text(
        f"""
        INSERT INTO {OUTPUT_TABLE} (
            cluster_name,
            company_count,
            road_names,
            top_entity_names,
            town_streets,
            top_company_industry_l2_names,
            top_company_industry_l3_names
        ) VALUES (
            :cluster_name,
            :company_count,
            :road_names,
            :top_entity_names,
            :town_streets,
            :top_company_industry_l2_names,
            :top_company_industry_l3_names
        )
        """
    )

    engine = get_engine()
    total_inserted = 0
    with engine.begin() as connection:
        for batch in chunk_rows(rows, INSERT_BATCH_SIZE):
            connection.execute(insert_sql, batch)
            total_inserted += len(batch)
            log_progress(f"统计结果写入中: {total_inserted}/{len(rows)} 行")
    log_progress(f"统计结果写入完成: {total_inserted} 行")


def build_cluster_summary():
    """按 `location_label` 聚合统计。"""
    cluster_summary = OrderedDict()
    total_rows = 0
    skipped_cluster_rows = 0

    log_progress(f"开始读取输入表: {INPUT_TABLE}")

    for row in iter_input_rows():
        total_rows += 1
        cluster_name = clean_text(row.get("location_label"))
        road_name = clean_text(row.get("road_name"))
        entity_name = clean_text(row.get("entity_name"))
        town_street = clean_text(row.get("town_street"))
        industry_l2_name = clean_text(row.get("industry_l2_name"))
        industry_l3_name = clean_text(row.get("industry_l3_name"))

        if not cluster_name:
            skipped_cluster_rows += 1
            if total_rows % PROGRESS_EVERY_ROWS == 0:
                log_progress(
                    f"读取进度: {total_rows} 行，label 数 {len(cluster_summary)}，"
                    f"无 label 跳过 {skipped_cluster_rows} 行"
                )
            continue

        summary = cluster_summary.setdefault(
            cluster_name,
            {
                "company_count": 0,
                "road_names": [],
                "entity_counter": Counter(),
                "town_streets": [],
                "industry_l2_counter": Counter(),
                "industry_l3_counter": Counter(),
            },
        )
        summary["company_count"] += 1

        if road_name and road_name not in summary["road_names"]:
            summary["road_names"].append(road_name)

        if entity_name:
            summary["entity_counter"][entity_name] += 1

        if town_street and town_street not in summary["town_streets"]:
            summary["town_streets"].append(town_street)

        if industry_l2_name:
            summary["industry_l2_counter"][industry_l2_name] += 1

        if industry_l3_name:
            summary["industry_l3_counter"][industry_l3_name] += 1

        if total_rows % PROGRESS_EVERY_ROWS == 0:
            log_progress(
                f"读取进度: {total_rows} 行，label 数 {len(cluster_summary)}，"
                f"无 label 跳过 {skipped_cluster_rows} 行"
            )

    log_progress(
        f"读取完成: {total_rows} 行，label 数 {len(cluster_summary)}，"
        f"无 label 跳过 {skipped_cluster_rows} 行"
    )
    return cluster_summary


def build_output_rows(cluster_summary):
    """把聚合后的中间结构转换成最终写库行。"""
    log_progress("开始生成统计结果行")
    output_rows = []
    for cluster_name, summary in cluster_summary.items():
        top_entities = summary["entity_counter"].most_common(TOP_ENTITY_COUNT)
        top_industry_l2_names = summary["industry_l2_counter"].most_common(TOP_INDUSTRY_COUNT)
        top_industry_l3_names = summary["industry_l3_counter"].most_common(TOP_INDUSTRY_COUNT)

        output_rows.append({
            "cluster_name": cluster_name,
            "company_count": summary["company_count"],
            "road_names": ",".join(summary["road_names"]),
            "top_entity_names": ",".join(f"{name}({count})" for name, count in top_entities),
            "town_streets": ",".join(summary["town_streets"]),
            "top_company_industry_l2_names": ",".join(
                f"{industry_name}({count})" for industry_name, count in top_industry_l2_names
            ),
            "top_company_industry_l3_names": ",".join(
                f"{industry_name}({count})" for industry_name, count in top_industry_l3_names
            ),
        })

    log_progress(f"统计结果行生成完成: {len(output_rows)} 行")
    return output_rows


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    """执行 location_label 维度的聚类统计，并写出结果表。"""
    log_progress("脚本 4.1 开始执行")
    cluster_summary = build_cluster_summary()
    output_rows = build_output_rows(cluster_summary)

    log_progress(f"开始写出结果表: {OUTPUT_TABLE}")
    recreate_output_table()
    insert_rows(output_rows)

    log_progress("处理完成")
    log_progress(f"共输出 {len(output_rows)} 个 location_label")


if __name__ == "__main__":
    main()
