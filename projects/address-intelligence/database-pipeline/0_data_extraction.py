import os

import sqlalchemy as sa
from sqlalchemy import create_engine, text
import logging

# ======================== 参数定义 ========================
DB_TARGET = os.environ['SR_TARGET_DSN']
DB_LAKE   = os.environ['SR_SOURCE_DSN']

SOURCE_TABLE = "fintech_data_lake.jwy_ods__mongo_pazxdb__company_base__si"
TARGET_TABLE = "fintech_stat.company_address_yuexiu_ly"
PROGRESS_TABLE = "fintech_stat.etl_progress_yuexiu_ly"

# 源表主键列名（用于断点续跑）
SOURCE_ID_COL = "id"

# 每批处理行数
BATCH_SIZE = 10000

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================== 工具函数 ========================
def get_engines():
    """创建数据库引擎并测试连接"""
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

def create_tables_manually(engine_target):
    """手动创建目标表和进度表（适配 StarRocks）"""
    
    # 目标表：包含 credit_no, company_name, company_address 三列
    create_target_sql = f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        credit_no VARCHAR(255) COMMENT '统一社会信用代码',
        company_name VARCHAR(255) COMMENT '公司名称',
        company_address VARCHAR(500) COMMENT '公司地址'
    ) ENGINE=OLAP
    DUPLICATE KEY(credit_no)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """
    
    # 进度表
    create_progress_sql = f"""
    CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
        table_name VARCHAR(255) NOT NULL,
        last_id BIGINT NOT NULL
    ) ENGINE=OLAP
    PRIMARY KEY(table_name)
    DISTRIBUTED BY HASH(table_name) BUCKETS 1
    PROPERTIES ("replication_num" = "1");
    """
    
    with engine_target.connect() as conn:
        try:
            conn.execute(text(create_target_sql))
            conn.commit()
            logger.info(f"✅ 目标表 {TARGET_TABLE} 已创建/存在")
        except Exception as e:
            logger.warning(f"创建目标表时出现问题：{e}")
            
        try:
            conn.execute(text(create_progress_sql))
            conn.commit()
            logger.info(f"✅ 进度表 {PROGRESS_TABLE} 已创建/存在")
        except Exception as e:
            logger.warning(f"创建进度表时出现问题：{e}")

def get_last_id(engine_target):
    """从进度表读取已处理的最大 id"""
    with engine_target.connect() as conn:
        try:
            result = conn.execute(
                text(f"SELECT last_id FROM {PROGRESS_TABLE} WHERE table_name = :tname"),
                {"tname": TARGET_TABLE}
            )
            row = result.fetchone()
            if row and row[0] is not None:
                return row[0]
            else:
                # 初始化记录
                conn.execute(
                    text(f"INSERT INTO {PROGRESS_TABLE} (table_name, last_id) VALUES (:tname, 0)"),
                    {"tname": TARGET_TABLE}
                )
                conn.commit()
                return 0
        except Exception as e:
            logger.error(f"读取进度表失败：{e}")
            create_tables_manually(engine_target)
            return 0

def update_last_id(engine_target, last_id):
    """更新进度表中的 last_id"""
    with engine_target.begin() as conn:
        conn.execute(
            text(f"UPDATE {PROGRESS_TABLE} SET last_id = :last_id WHERE table_name = :tname"),
            {"last_id": last_id, "tname": TARGET_TABLE}
        )

def deduplicate_rows(rows):
    """
    对数据进行去重：
    1. credit_no 不为空的行，按 credit_no 去重，取第一条
    2. credit_no 为空但 company_name 不为空的行，按 company_name 去重，取第一条
    3. credit_no 为空且 company_name 为空的行，保留所有
    """
    credit_no_dict = {}      # 用于存储 credit_no 不为空的记录
    company_name_dict = {}   # 用于存储 credit_no 为空但 company_name 不为空的记录
    null_rows = []           # 用于存储两个都为空的记录
    
    for row in rows:
        credit_no, company_name, company_address = row
        
        # 判断 credit_no 是否有效
        if credit_no is not None and str(credit_no).strip() != '':
            # 按 credit_no 去重
            if credit_no not in credit_no_dict:
                credit_no_dict[credit_no] = row
        # credit_no 无效，但 company_name 有效
        elif company_name is not None and str(company_name).strip() != '':
            # 按 company_name 去重
            if company_name not in company_name_dict:
                company_name_dict[company_name] = row
        else:
            # 两个都无效，保留所有
            null_rows.append(row)
    
    # 合并结果
    result = list(credit_no_dict.values()) + list(company_name_dict.values()) + null_rows
    
    dedup_count = len(rows) - len(result)
    if dedup_count > 0:
        logger.info(f"去重：原始 {len(rows)} 行，去重后 {len(result)} 行，移除 {dedup_count} 条重复记录")
    
    return result

def batch_insert(engine_target, rows):
    """批量插入数据到目标表（先过滤无效记录，再去重）"""
    if not rows:
        return 0
    
    # 第一步：过滤掉所有字段都为空的记录
    filtered_rows = []
    for credit_no, company_name, company_address in rows:
        # 如果三个字段都为空，则跳过
        if (credit_no is None or str(credit_no).strip() == '') and \
           (company_name is None or str(company_name).strip() == '') and \
           (company_address is None or str(company_address).strip() == ''):
            continue
        filtered_rows.append((credit_no, company_name, company_address))
    
    if not filtered_rows:
        logger.warning(f"本批次 {len(rows)} 条记录全部被过滤（所有字段均为空）")
        return 0
    
    # 第二步：去重
    deduplicated_rows = deduplicate_rows(filtered_rows)
    
    # 第三步：插入数据
    insert_sql = text(f"""
        INSERT INTO {TARGET_TABLE} (credit_no, company_name, company_address)
        VALUES (:credit_no, :company_name, :company_address)
    """)
    
    with engine_target.begin() as conn:
        conn.execute(insert_sql, [
            {"credit_no": row[0], "company_name": row[1], "company_address": row[2]} 
            for row in deduplicated_rows
        ])
    
    return len(deduplicated_rows)

# ======================== 核心逻辑 ========================
def etl_with_resume(engine_target, engine_lake):
    """断点续跑 ETL（基于主键 id > last_id）"""
    last_id = get_last_id(engine_target)
    logger.info(f"📌 从源表 {SOURCE_ID_COL} > {last_id} 开始处理")

    total_inserted = 0
    current_max_id = last_id
    batch_count = 0

    while True:
        batch_count += 1
        # 查询一批数据（包含主键列和三列业务数据）
        select_sql = text(f"""
            SELECT {SOURCE_ID_COL}, credit_no, company_name, company_address
            FROM {SOURCE_TABLE}
            WHERE company_address LIKE '%越秀区%'
              AND {SOURCE_ID_COL} > :last_id
            ORDER BY {SOURCE_ID_COL}
            LIMIT :limit
        """)
        
        with engine_lake.connect() as lake_conn:
            result = lake_conn.execute(select_sql, {"last_id": last_id, "limit": BATCH_SIZE})
            rows = result.fetchall()   # 每条 (id, credit_no, company_name, company_address)

        if not rows:
            logger.info("🏁 没有更多数据，ETL 完成")
            break

        # 提取业务数据（credit_no, company_name, company_address）
        business_data = [(row[1], row[2], row[3]) for row in rows]
        inserted = batch_insert(engine_target, business_data)
        total_inserted += inserted

        # 更新断点：取本批次最大主键
        batch_max_id = rows[-1][0]
        if batch_max_id > current_max_id:
            current_max_id = batch_max_id
            update_last_id(engine_target, current_max_id)

        # 统计本批记录数
        filtered_out = len(rows) - inserted
        logger.info(f"✅ 批次 {batch_count}: 读取 {len(rows)} 行，"
                    f"插入 {inserted} 行，过滤+去重 {filtered_out} 行，"
                    f"累计 {total_inserted} 行，当前 last_id = {batch_max_id}")

        # 准备下一轮
        last_id = batch_max_id

        # 如果本批数量不足 BATCH_SIZE，说明已取完
        if len(rows) < BATCH_SIZE:
            logger.info("最后一批数据处理完成")
            break

    logger.info(f"🎉 ETL 完成！共插入 {total_inserted} 行到 {TARGET_TABLE}")

# ======================== 主函数 ========================
def main():
    try:
        engine_target, engine_lake = get_engines()
        create_tables_manually(engine_target)
        etl_with_resume(engine_target, engine_lake)
    except Exception as e:
        logger.error(f"程序异常退出：{e}")
        raise

if __name__ == "__main__":
    main()