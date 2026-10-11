"""
产业链分析综合重构版 (Consolidated Chain Analysis)
功能：
1. 产业链图谱关系强度计算 (Graph Relation Strength)
2. 节点内部活跃度计算 (Internal Activity)
3. 节点参与度计算 (Involvement)

特性：
- 整合三个独立脚本，统一数据库连接与配置
- 自动清理中间表，减少数据库冗余
- 统一 Min-Max 归一化计算逻辑
- 简化流程：归并数据准备步骤，减少中间表跳转
"""
import os


import pymysql
from typing import Dict, List, Tuple, Any
import logging
import pandas as pd
from datetime import datetime

# ====================================================================
# 全局配置
# ====================================================================

class Config:
    DB_HOST = os.environ['SR_DB_HOST']
    DB_PORT = 9030
    DB_USER = os.environ['SR_DB_USER']
    DB_PASSWORD = os.environ['SR_DB_PASSWORD']
    DB_NAME = 'fintech_stat'

    # 当前处理的产业链
    TABLE_SUFFIX = '_ly'

    CHAIN_NAME = '精密仪器设备产业链'
    YEAR = 2024
    CHAIN = 'PI'

    # 时间范围配置
    START_DATE = f'{YEAR}-01'
    END_DATE = f'{YEAR}-12'

    # 原始表
    CHAIN_MAP_TABLE = f'industry_chain_code_enterprise_map_dev_{CHAIN}_{YEAR}_ly'
    CHAIN_CLS_TABLE = f'industry_chain_cls_{CHAIN}_ly'
    NODE_RELATION_TABLE = f'industry_node_relation_{CHAIN}_ly'
    
    # 源表
    SALES_SI_TABLE = 'jwy_ods__jwy_tax__t_invoice_sales__si'
    DECRYPTION_TABLE = 'dwd_jwy_tax__t_invoice_sales_buyer_mapping_mvm'
    SALES_ITEM_TABLE = 'jwy_ods__jwy_tax__t_invoice_sales_item__goods_codes_fill_202408_202509'
    INCOME_SI_TABLE = 'jwy_ods__jwy_tax__t_invoice_income__si'
    INCOME_ITEM_TABLE = 'jwy_ods__jwy_tax__t_invoice_income_item__goods_codes_fill_202408_202509'

    # 中间表 (用完即删)
    # 归并后的统一路径表，包含 L0-L3 祖先节点
    UNIFIED_PREP_TABLE = f'tmp_unified_chain_prep{TABLE_SUFFIX}'
    RAW_TRADE_MONTH_TABLE = f'tmp_raw_trade_month_{CHAIN}_{YEAR}{TABLE_SUFFIX}'
    INTERMEDIATE_TABLES = [UNIFIED_PREP_TABLE]

    # 结果输出表
    GRAPH_STRENGTH_TABLE = f'graph_relation_strength{TABLE_SUFFIX}'
    INTERNAL_ACTIVITY_TABLE = f'node_internal_activity_all_levels_{CHAIN}_{YEAR}{TABLE_SUFFIX}'
    INVOLVEMENT_TABLE = f'node_involvement{TABLE_SUFFIX}'

    # Strength 计算权重
    STRENGTH_WEIGHTS = {
        'amount_sum': 0.30,
        'edge_count': 0.30,
        'invoice_sum': 0.20,
        'density': 0.20
    }

# ====================================================================
# 日志配置
# ====================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ====================================================================
# 数据库工具类
# ====================================================================

class DatabaseConnector:
    def __init__(self, config: Config):
        self.config = config
        self.connection = None

    def connect(self):
        self.connection = pymysql.connect(
            host=self.config.DB_HOST,
            port=self.config.DB_PORT,
            user=self.config.DB_USER,
            password=self.config.DB_PASSWORD,
            database=self.config.DB_NAME,
            charset='utf8mb4'
        )

    def execute_dml(self, sql: str, params: Tuple = None) -> int:
        try:
            with self.connection.cursor() as cursor:
                affected = cursor.execute(sql, params)
                self.connection.commit()
                return affected
        except Exception as e:
            self.connection.rollback()
            logger.error(f"SQL Error: {e}")
            raise

    def execute_query(self, sql: str, params: Tuple = None) -> List[Dict]:
        with self.connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def execute_batch(self, sql: str, params_list: List[Tuple]) -> int:
        try:
            with self.connection.cursor() as cursor:
                affected = cursor.executemany(sql, params_list)
                self.connection.commit()
                return affected
        except Exception as e:
            self.connection.rollback()
            logger.error(f"Batch Error: {e}")
            raise

    def close(self):
        if self.connection:
            self.connection.close()

# ====================================================================
# 核心分析类
# ====================================================================

class ChainAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.db = DatabaseConnector(config)

    def run(self):
        try:
            self.db.connect()
            logger.info(f"🚀 开始产业链综合分析: {self.config.CHAIN_NAME}")

            # --- Step 1: 归并化简的数据准备 ---
            # 将原先 01 脚本的 3 个中间表和 02 脚本的扩展表归并为一个
            self._prepare_unified_data()

            # --- Step 2: 核心计算 ---
            #self._calculate_graph_strength()
            self._calculate_internal_activity()
            #self._calculate_involvement()

            # --- Step 3: 清理 ---
            self._cleanup()

            logger.info("✅ 产业链分析全部完成")

        finally:
            self.db.close()

    def _cleanup(self):
        """删除所有中间表"""
        logger.info("🧹 清理中间表...")
        for table in self.config.INTERMEDIATE_TABLES:
            self.db.execute_dml(f"DROP TABLE IF EXISTS {table}")

    def _ensure_table(self, table_name: str, create_sql: str):
        """确保表存在，如果结构不符则重建"""
        check_sql = f"SHOW TABLES LIKE '{table_name}'"
        if not self.db.execute_query(check_sql):
            self.db.execute_dml(create_sql)
            return

    # ----------------------------------------------------------------
    # 归并化简的数据准备逻辑
    # ----------------------------------------------------------------

    def _iter_months(self):
        start_year, start_month = map(int, self.config.START_DATE.split('-'))
        end_year, end_month = map(int, self.config.END_DATE.split('-'))

        months = []
        y, m = start_year, start_month
        while (y < end_year) or (y == end_year and m <= end_month):
            months.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
        return months

    def _prepare_unified_data(self):
        logger.info("Step 1: 直接从源表准备统一路径表（按月聚合，避免 OOM）...")

        self.db.execute_dml(f"DROP TABLE IF EXISTS {self.config.UNIFIED_PREP_TABLE}")
        self.db.execute_dml(f"DROP TABLE IF EXISTS {self.config.RAW_TRADE_MONTH_TABLE}")

        create_month_table_sql = f"""
        CREATE TABLE {self.config.RAW_TRADE_MONTH_TABLE} (
            from_tax_id VARCHAR(64),
            to_tax_id VARCHAR(64),
            year_mon VARCHAR(7),
            goods_code VARCHAR(16),
            invoice_type VARCHAR(50),       
            total_amount DECIMAL(38, 2),
            item_total_amount DECIMAL(38, 2),
            invoice_num BIGINT,
            days BIGINT
        )
        ENGINE=OLAP
        DUPLICATE KEY(from_tax_id, to_tax_id, year_mon, goods_code, invoice_type)
        DISTRIBUTED BY HASH(from_tax_id) BUCKETS 16
        PROPERTIES ("replication_num" = "3")
        """
        self.db.execute_dml(create_month_table_sql)

        months = self._iter_months()

        for ym in months:
            logger.info(f"  处理月份: {ym}")

            # -------------------------
            # 1) 销项：按月插入
            # -------------------------
            
            sales_sql = f"""
            INSERT INTO {self.config.RAW_TRADE_MONTH_TABLE}
            WITH
            chain_tax_sales AS (
                SELECT DISTINCT tax_id
                FROM {self.config.CHAIN_MAP_TABLE}
                WHERE chain_name = '{self.config.CHAIN_NAME}'
                AND invoice_type IN ('sales', 'industry_sales', 'industry_income_expand')
            ),
            chain_goods AS (
                SELECT DISTINCT CAST(map_code AS STRING) AS goods_code
                FROM {self.config.CHAIN_CLS_TABLE}
                WHERE chain_name = '{self.config.CHAIN_NAME}'
                AND data_source = '商品分类'
            ),
            sales_si AS (
                SELECT
                    s.id_md5,
                    s.tax_id,
                    s.saler_tax_no,
                    s.buyer_tax_no,
                    s.year_mon,
                    s.invoice_date,
                    s.invoice_amount          -- 添加：发票总金额
                FROM fintech_data_lake.{self.config.SALES_SI_TABLE} s
                INNER JOIN chain_tax_sales c
                    ON s.tax_id = c.tax_id
                WHERE s.year_mon = '{ym}'
                AND s.invoice_status IN ('0', '3')
                AND s.invoice_amount > 0
            ),
            sales_si_dec AS (
                SELECT
                    s.id_md5,
                    s.tax_id,
                    s.saler_tax_no AS from_tax_id,
                    d.buyer_tax_no_decrypt AS to_tax_id,
                    s.year_mon,
                    s.invoice_date,
                    s.invoice_amount          -- 传递发票总金额
                FROM sales_si s
                INNER JOIN fintech_stat.{self.config.DECRYPTION_TABLE} d
                    ON s.buyer_tax_no = d.buyer_tax_no
                WHERE d.buyer_tax_no_decrypt IS NOT NULL
                AND TRIM(d.buyer_tax_no_decrypt) != ''
            ),
            sales_item AS (
                SELECT
                    id_md5,
                    tax_id,
                    SUBSTRING(CAST(goods_code AS STRING), 1, 7) AS goods_code,
                    COALESCE(detail_amount, 0) + COALESCE(tax_amount, 0) AS item_amount
                FROM fintech_data_lake.{self.config.SALES_ITEM_TABLE}
                WHERE year_mon = '{ym}'
                AND goods_name IS NOT NULL
                AND TRIM(goods_name) != ''
                AND goods_name NOT IN (
                        '原价合计', '折扣额合计', '(详见销货清单)', '（详见销货清单）',
                        '(样见销货清单)', '（样见销货清单）', '详见对应正数发票及清单', '（详见对应正数发票及清单）'
                )
                AND detail_amount IS NOT NULL
            ),
            sales_detail AS (
                SELECT
                    s.from_tax_id,
                    s.to_tax_id,
                    s.year_mon,
                    i.goods_code,
                    s.id_md5,
                    s.invoice_date,
                    i.item_amount,
                    s.invoice_amount          -- 添加：发票总金额
                FROM sales_si_dec s
                INNER JOIN sales_item i
                    ON s.id_md5 = i.id_md5
                AND s.tax_id = i.tax_id
                INNER JOIN chain_goods g
                    ON i.goods_code = g.goods_code
                WHERE s.from_tax_id IS NOT NULL AND TRIM(s.from_tax_id) != ''
                AND s.to_tax_id IS NOT NULL AND TRIM(s.to_tax_id) != ''
                AND i.goods_code IS NOT NULL AND TRIM(i.goods_code) != ''
            ),
            -- 按交易对和发票类型汇总（新增：计算该交易对在该月份该类型下的总金额）
            sales_pair_amount AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    invoice_type,           -- 添加 invoice_type 到分组
                    SUM(item_amount) AS total_amount
                FROM (
                    SELECT 
                        from_tax_id,
                        to_tax_id,
                        year_mon,
                        'sales' AS invoice_type,  -- 在子查询中定义 invoice_type
                        item_amount
                    FROM sales_detail
                ) t
                GROUP BY from_tax_id, to_tax_id, year_mon, invoice_type
            ),
            sales_goods AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    goods_code,
                    SUM(item_amount) AS item_total_amount
                FROM sales_detail
                GROUP BY from_tax_id, to_tax_id, year_mon, goods_code
            ),
            sales_pair AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    COUNT(DISTINCT id_md5) AS invoice_num,
                    COUNT(DISTINCT invoice_date) AS days
                FROM sales_detail
                GROUP BY from_tax_id, to_tax_id, year_mon
            )
            SELECT
                g.from_tax_id,
                g.to_tax_id,
                g.year_mon,
                g.goods_code,
                'sales' AS invoice_type,          -- 设置invoice_type为'sales'
                pa.total_amount,                   -- 添加该交易对的总金额
                g.item_total_amount,
                p.invoice_num,
                p.days
            FROM sales_goods g
            INNER JOIN sales_pair p
                ON g.from_tax_id = p.from_tax_id
                AND g.to_tax_id = p.to_tax_id
                AND g.year_mon = p.year_mon
            INNER JOIN sales_pair_amount pa        -- 关联交易对总金额
                ON g.from_tax_id = pa.from_tax_id
                AND g.to_tax_id = pa.to_tax_id
                AND g.year_mon = pa.year_mon
                AND pa.invoice_type = 'sales' 
            """
            self.db.execute_dml(sales_sql)

            # -------------------------
            # 2) 进项：按月插入
            # -------------------------
           
            income_sql = f"""
            INSERT INTO {self.config.RAW_TRADE_MONTH_TABLE}
            WITH
            chain_tax_income AS (
                SELECT DISTINCT tax_id
                FROM {self.config.CHAIN_MAP_TABLE}
                WHERE chain_name = '{self.config.CHAIN_NAME}'
                AND invoice_type IN ('income', 'industry_income', 'industry_sales_expand')
            ),
            chain_goods AS (
                SELECT DISTINCT CAST(map_code AS STRING) AS goods_code
                FROM {self.config.CHAIN_CLS_TABLE}
                WHERE chain_name = '{self.config.CHAIN_NAME}'
                AND data_source = '商品分类'
            ),
            income_si AS (
                SELECT
                    s.id_md5,
                    s.tax_id,
                    s.saler_tax_no,
                    s.buyer_tax_no,
                    s.year_mon,
                    s.invoice_date,
                    s.invoice_amount          -- 添加：发票总金额
                FROM fintech_data_lake.{self.config.INCOME_SI_TABLE} s
                INNER JOIN chain_tax_income c
                    ON s.tax_id = c.tax_id
                WHERE s.year_mon = '{ym}'
                AND s.invoice_status IN ('0', '3')
                AND s.invoice_amount > 0
                AND s.saler_tax_no IS NOT NULL
                AND TRIM(s.saler_tax_no) != ''
                AND s.buyer_tax_no IS NOT NULL
                AND TRIM(s.buyer_tax_no) != ''
            ),
            income_item AS (
                SELECT
                    id_md5,
                    tax_id,
                    SUBSTRING(CAST(goods_code AS STRING), 1, 7) AS goods_code,
                    COALESCE(detail_amount, 0) + COALESCE(tax_amount, 0) AS item_amount
                FROM fintech_data_lake.{self.config.INCOME_ITEM_TABLE}
                WHERE year_mon = '{ym}'
                AND goods_name IS NOT NULL
                AND TRIM(goods_name) != ''
                AND goods_name NOT IN (
                        '原价合计', '折扣额合计', '(详见销货清单)', '（详见销货清单）',
                        '(样见销货清单)', '（样见销货清单）', '详见对应正数发票及清单', '（详见对应正数发票及清单）'
                )
                AND detail_amount IS NOT NULL
            ),
            income_detail AS (
                SELECT
                    s.saler_tax_no AS from_tax_id,
                    s.buyer_tax_no AS to_tax_id,
                    s.year_mon,
                    i.goods_code,
                    s.id_md5,
                    s.invoice_date,
                    i.item_amount,
                    s.invoice_amount          -- 添加：发票总金额
                FROM income_si s
                INNER JOIN income_item i
                    ON s.id_md5 = i.id_md5
                AND s.tax_id = i.tax_id
                INNER JOIN chain_goods g
                    ON i.goods_code = g.goods_code
                WHERE i.goods_code IS NOT NULL AND TRIM(i.goods_code) != ''
            ),
            -- 按交易对和发票类型汇总
            income_pair_amount AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    invoice_type,
                    SUM(item_amount) AS total_amount
                FROM (
                    SELECT 
                        from_tax_id,
                        to_tax_id,
                        year_mon,
                        'income' AS invoice_type,
                        item_amount
                    FROM income_detail
                ) t
                GROUP BY from_tax_id, to_tax_id, year_mon, invoice_type
            ),
            income_goods AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    goods_code,
                    SUM(item_amount) AS item_total_amount
                FROM income_detail
                GROUP BY from_tax_id, to_tax_id, year_mon, goods_code
            ),
            income_pair AS (
                SELECT
                    from_tax_id,
                    to_tax_id,
                    year_mon,
                    COUNT(DISTINCT id_md5) AS invoice_num,
                    COUNT(DISTINCT invoice_date) AS days
                FROM income_detail
                GROUP BY from_tax_id, to_tax_id, year_mon
            )
            SELECT
                g.from_tax_id,
                g.to_tax_id,
                g.year_mon,
                g.goods_code,
                'income' AS invoice_type,          -- 设置invoice_type为'income'
                pa.total_amount,                    -- 添加该交易对的总金额
                g.item_total_amount,
                p.invoice_num,
                p.days
            FROM income_goods g
            INNER JOIN income_pair p
                ON g.from_tax_id = p.from_tax_id
                AND g.to_tax_id = p.to_tax_id
                AND g.year_mon = p.year_mon
            INNER JOIN income_pair_amount pa        -- 关联交易对总金额
                ON g.from_tax_id = pa.from_tax_id
                AND g.to_tax_id = pa.to_tax_id
                AND g.year_mon = pa.year_mon
                AND pa.invoice_type = 'income'
            """
            self.db.execute_dml(income_sql)

        logger.info("  月度中间表构建完成，开始汇总并映射节点路径...")

        final_sql = f"""
        CREATE TABLE {self.config.UNIFIED_PREP_TABLE} AS
        WITH
        agg_trade AS (
            SELECT
                from_tax_id,
                to_tax_id,
                goods_code,
                MAX(invoice_type) AS invoice_type,  -- 添加，实际上同一交易对可能有混合类型
                SUM(total_amount) AS total_amount,   -- 汇总total_amount
                SUM(item_total_amount) AS total_item_amount,
                SUM(invoice_num) AS total_invoice_num,
                SUM(days) AS total_days
            FROM {self.config.RAW_TRADE_MONTH_TABLE}
            GROUP BY from_tax_id, to_tax_id, goods_code
        ),
        map_enterprise AS (
            SELECT DISTINCT tax_id, map_code
            FROM {self.config.CHAIN_MAP_TABLE}
            WHERE chain_name = '{self.config.CHAIN_NAME}'
        ),
        cls AS (
            SELECT
                map_code,
                node_path,
                node_type,
                (LENGTH(node_path) - LENGTH(REPLACE(node_path, '>', '')) + 1) AS node_level
            FROM {self.config.CHAIN_CLS_TABLE}
            WHERE chain_name = '{self.config.CHAIN_NAME}'
        ),
        chain_filtered AS (
            SELECT
                agg.*,
                cls_from.node_path AS from_node_path,
                cls_from.node_type AS from_node_type,
                cls_from.node_level AS from_node_level,
                cls_to.node_path AS to_node_path,
                cls_to.node_type AS to_node_type,
                cls_to.node_level AS to_node_level
            FROM agg_trade agg
            INNER JOIN map_enterprise map_from
                ON agg.from_tax_id = map_from.tax_id
            INNER JOIN map_enterprise map_to
                ON agg.to_tax_id = map_to.tax_id
            INNER JOIN cls cls_from
                ON map_from.map_code = cls_from.map_code
            INNER JOIN cls cls_to
                ON map_to.map_code = cls_to.map_code
        )
        SELECT
            *,
            from_node_type AS from_L0_node,
            CASE
                WHEN from_node_level >= 1 THEN COALESCE(NULLIF(SPLIT_PART(from_node_path, '>', 1), ''), from_node_path)
                ELSE NULL
            END AS from_L1_node,
            CASE
                WHEN from_node_level >= 2 THEN CONCAT(SPLIT_PART(from_node_path, '>', 1), '>', SPLIT_PART(from_node_path, '>', 2))
                ELSE NULL
            END AS from_L2_node,
            CASE
                WHEN from_node_level >= 3 THEN from_node_path
                ELSE NULL
            END AS from_L3_node,
            to_node_type AS to_L0_node,
            CASE
                WHEN to_node_level >= 1 THEN COALESCE(NULLIF(SPLIT_PART(to_node_path, '>', 1), ''), to_node_path)
                ELSE NULL
            END AS to_L1_node,
            CASE
                WHEN to_node_level >= 2 THEN CONCAT(SPLIT_PART(to_node_path, '>', 1), '>', SPLIT_PART(to_node_path, '>', 2))
                ELSE NULL
            END AS to_L2_node,
            CASE
                WHEN to_node_level >= 3 THEN to_node_path
                ELSE NULL
            END AS to_L3_node
        FROM chain_filtered
        """
        self.db.execute_dml(final_sql)

        cnt = self.db.execute_query(f"SELECT COUNT(*) AS cnt FROM {self.config.UNIFIED_PREP_TABLE}")
        logger.info(f"✅ 统一路径表准备完成，记录数: {cnt[0]['cnt']}")



    # ----------------------------------------------------------------
    # 核心计算逻辑
    # ----------------------------------------------------------------

    def _calculate_graph_strength(self):
        """计算图谱关系强度 (原 01 逻辑)"""
        logger.info("Step 2: 计算图谱关系强度...")

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.config.GRAPH_STRENGTH_TABLE} (
            chain_name VARCHAR(50), from_node_id BIGINT, to_node_id BIGINT,
            from_node_path VARCHAR(200), to_node_path VARCHAR(200),
            edge_count BIGINT, record_count BIGINT, 
            amount_avg DECIMAL(38,2), amount_sum DECIMAL(38,2),
            days_avg DECIMAL(38,2), days_sum BIGINT,
            invoice_num_avg DECIMAL(38,2), invoice_num_sum BIGINT, 
            density DECIMAL(10,4), strength DECIMAL(10,4)
        ) ENGINE=OLAP DUPLICATE KEY(chain_name, from_node_id) DISTRIBUTED BY HASH(from_node_id) BUCKETS 10 PROPERTIES ("replication_num" = "3");
        """
        self._ensure_table(self.config.GRAPH_STRENGTH_TABLE, create_sql)
        self.db.execute_dml(f"DELETE FROM {self.config.GRAPH_STRENGTH_TABLE} WHERE chain_name = %s", (self.config.CHAIN_NAME,))

        agg_sql = f"""
        SELECT 
            from_node_path, to_node_path,
            rel_from.node_id AS from_node_id, rel_to.node_id AS to_node_id,
            COUNT(DISTINCT CONCAT(from_tax_id, '-', to_tax_id)) AS edge_count,
            COUNT(*) AS record_count,
            AVG(total_item_amount) AS amount_avg, SUM(total_item_amount) AS amount_sum,
            AVG(total_days) AS days_avg, SUM(total_days) AS days_sum,
            AVG(total_invoice_num) AS invoice_num_avg, SUM(total_invoice_num) AS invoice_num_sum,
            COUNT(DISTINCT CONCAT(from_tax_id, '-', to_tax_id)) * 1.0 / NULLIF(COUNT(DISTINCT from_tax_id) + COUNT(DISTINCT to_tax_id), 0) AS density
        FROM {self.config.UNIFIED_PREP_TABLE} ep
        LEFT JOIN {self.config.NODE_RELATION_TABLE} rel_from ON rel_from.node_path = ep.from_node_path AND rel_from.chain_name = '{self.config.CHAIN_NAME}'
        LEFT JOIN {self.config.NODE_RELATION_TABLE} rel_to ON rel_to.node_path = ep.to_node_path AND rel_to.chain_name = '{self.config.CHAIN_NAME}'
        GROUP BY from_node_path, to_node_path, rel_from.node_id, rel_to.node_id
        """
        rows = self.db.execute_query(agg_sql)
        if not rows: return

        scored_rows = self._apply_strength_scoring(rows, 'amount_sum', 'edge_count', 'invoice_num_sum', 'density')

        insert_sql = f"""
        INSERT INTO {self.config.GRAPH_STRENGTH_TABLE} (
            chain_name, from_node_id, to_node_id, from_node_path, to_node_path,
            edge_count, record_count, amount_avg, amount_sum, days_avg, days_sum,
            invoice_num_avg, invoice_num_sum, density, strength
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self.db.execute_batch(insert_sql, [
            (self.config.CHAIN_NAME, r['from_node_id'], r['to_node_id'], r['from_node_path'], r['to_node_path'],
             r['edge_count'], r['record_count'], r['amount_avg'], r['amount_sum'], r['days_avg'], r['days_sum'],
             r['invoice_num_avg'], r['invoice_num_sum'], r['density'], r['strength']) for r in scored_rows
        ])

    def _calculate_internal_activity(self):
        """计算节点内部活跃度 (原 02 逻辑)"""
        logger.info("Step 3: 计算节点内部活跃度...")

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.config.INTERNAL_ACTIVITY_TABLE} (
            chain_name VARCHAR(50), node_id BIGINT, node_name VARCHAR(100),
            node_path VARCHAR(200), node_level TINYINT, node_type VARCHAR(50),
            internal_edge_count BIGINT, 
            internal_from_enterprise_count BIGINT, internal_to_enterprise_count BIGINT,
            internal_amount_sum DECIMAL(38, 2), internal_days_sum BIGINT,
            internal_invoice_sum BIGINT, internal_amount_avg DECIMAL(38, 2),
            internal_density DECIMAL(10, 4), strength DECIMAL(10, 4)
        ) ENGINE=OLAP DUPLICATE KEY(chain_name, node_id) DISTRIBUTED BY HASH(node_id) BUCKETS 10 PROPERTIES ("replication_num" = "3");
        """
        self._ensure_table(self.config.INTERNAL_ACTIVITY_TABLE, create_sql)
        self.db.execute_dml(f"DELETE FROM {self.config.INTERNAL_ACTIVITY_TABLE} WHERE chain_name = %s", (self.config.CHAIN_NAME,))

        for level in range(0, 4):
            col = f"from_L{level}_node"
            to_col = f"to_L{level}_node"
            sql = f"""
            INSERT INTO {self.config.INTERNAL_ACTIVITY_TABLE} (
                chain_name, node_id, node_name, node_path, node_level, node_type,
                internal_edge_count, internal_from_enterprise_count, internal_to_enterprise_count,
                internal_amount_sum, internal_days_sum, internal_invoice_sum, internal_amount_avg, internal_density
            )
            SELECT 
                '{self.config.CHAIN_NAME}', rel.node_id, rel.node_name, T.node_path, {level}, T.node_type,
                T.edge_count, T.from_ent_count, T.to_ent_count,
                T.amount_sum, T.days_sum, T.invoice_sum, T.amount_avg, T.density
            FROM (
                SELECT 
                    {col} AS node_path, from_node_type AS node_type,
                    COUNT(DISTINCT CONCAT(from_tax_id, '->', to_tax_id)) AS edge_count,
                    COUNT(DISTINCT from_tax_id) AS from_ent_count,
                    COUNT(DISTINCT to_tax_id) AS to_ent_count,
                    SUM(total_item_amount) AS amount_sum, 
                    SUM(total_days) AS days_sum,
                    SUM(total_invoice_num) AS invoice_sum,
                    AVG(total_item_amount) AS amount_avg,
                    COUNT(DISTINCT CONCAT(from_tax_id, '->', to_tax_id)) * 1.0 / NULLIF(COUNT(DISTINCT from_tax_id) + COUNT(DISTINCT to_tax_id), 0) AS density
                FROM {self.config.UNIFIED_PREP_TABLE}
                WHERE {col} = {to_col} AND {col} IS NOT NULL
                GROUP BY {col}, from_node_type
            ) T
            LEFT JOIN {self.config.NODE_RELATION_TABLE} rel ON rel.node_path = T.node_path AND rel.chain_name = '{self.config.CHAIN_NAME}'
            """
            self.db.execute_dml(sql)

        self._update_table_strength(self.config.INTERNAL_ACTIVITY_TABLE, 'internal_amount_sum', 'internal_edge_count', 'internal_invoice_sum', 'internal_density')

    def _calculate_involvement(self):
        """计算节点参与度 (原 03 逻辑)"""
        logger.info("Step 4: 计算节点参与度...")

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.config.INVOLVEMENT_TABLE} (
            chain_name VARCHAR(50), from_node_id BIGINT, to_node_id BIGINT,
            node_path VARCHAR(200), node_level TINYINT, node_type VARCHAR(50),
            involve_edge_count BIGINT, involve_amount_sum DECIMAL(38, 2),
            involve_days_sum BIGINT, involve_invoice_sum BIGINT, 
            involve_amount_avg DECIMAL(38, 2), involve_density DECIMAL(10, 4), 
            involve_strength DECIMAL(10, 4)
        ) ENGINE=OLAP DUPLICATE KEY(chain_name, from_node_id) DISTRIBUTED BY HASH(to_node_id) BUCKETS 10 PROPERTIES ("replication_num" = "3");
        """
        self._ensure_table(self.config.INVOLVEMENT_TABLE, create_sql)
        self.db.execute_dml(f"DELETE FROM {self.config.INVOLVEMENT_TABLE} WHERE chain_name = %s", (self.config.CHAIN_NAME,))

        for level in range(1, 4):
            p_col, c_col = f"from_L{level-1}_node", f"from_L{level}_node"
            p_to_col, c_to_col = f"to_L{level-1}_node", f"to_L{level}_node"

            sql = f"""
            INSERT INTO {self.config.INVOLVEMENT_TABLE} (
                chain_name, from_node_id, to_node_id, node_path, node_level, node_type,
                involve_edge_count, involve_amount_sum, involve_days_sum, involve_invoice_sum, 
                involve_amount_avg, involve_density
            )
            SELECT
                '{self.config.CHAIN_NAME}', rel_from.node_id, rel_to.node_id, T.target_node, {level}, T.node_type,
                T.edge_count, T.amount_sum, T.days_sum, T.invoice_sum, T.amount_avg, T.density
            FROM (
                SELECT parent_node, target_node, node_type, 
                       COUNT(DISTINCT edge_key) as edge_count, 
                       SUM(amount) as amount_sum, 
                       SUM(days) as days_sum,
                       SUM(invoice_num) as invoice_sum,
                       AVG(amount) as amount_avg,
                       COUNT(DISTINCT edge_key) * 1.0 / NULLIF(COUNT(DISTINCT from_tax_id) + COUNT(DISTINCT to_tax_id), 0) as density
                FROM (
                    SELECT {p_col} as parent_node, {c_col} as target_node, from_node_type as node_type, from_tax_id, to_tax_id, CONCAT(from_tax_id, '->', to_tax_id) as edge_key, total_item_amount as amount, total_days as days, total_invoice_num as invoice_num
                    FROM {self.config.UNIFIED_PREP_TABLE} WHERE {p_col} = {p_to_col} AND {c_col} IS NOT NULL
                    UNION ALL
                    SELECT {p_col} as parent_node, {c_to_col} as target_node, to_node_type as node_type, from_tax_id, to_tax_id, CONCAT(from_tax_id, '->', to_tax_id) as edge_key, total_item_amount as amount, total_days as days, total_invoice_num as invoice_num
                    FROM {self.config.UNIFIED_PREP_TABLE} WHERE {p_col} = {p_to_col} AND {c_to_col} IS NOT NULL AND ({c_col} IS NULL OR {c_col} != {c_to_col})
                ) combined GROUP BY parent_node, target_node, node_type
            ) T
            LEFT JOIN {self.config.NODE_RELATION_TABLE} rel_from ON rel_from.node_path = T.parent_node AND rel_from.chain_name = '{self.config.CHAIN_NAME}'
            LEFT JOIN {self.config.NODE_RELATION_TABLE} rel_to ON rel_to.node_path = T.target_node AND rel_to.chain_name = '{self.config.CHAIN_NAME}'
            """
            self.db.execute_dml(sql)

        self._update_table_strength(self.config.INVOLVEMENT_TABLE, 'involve_amount_sum', 'involve_edge_count', 'involve_invoice_sum', 'involve_density', 'involve_strength')

    # ----------------------------------------------------------------
    # 通用计算工具 (归一化与评分)
    # ---------------------------------------------------------------


    def _apply_strength_scoring(self, rows: List[Dict], col_amt: str, col_edge: str, col_inv: str, col_den: str) -> List[Dict]:
        if not rows: return []

        def get_min_max(key):
            vals = [float(r[key]) for r in rows if r[key] is not None]
            return (min(vals), max(vals)) if vals else (0, 0)

        stats = {k: get_min_max(k) for k in [col_amt, col_edge, col_inv, col_den]}
        weights = self.config.STRENGTH_WEIGHTS

        def norm(val, key):
            mi, ma = stats[key]
            return (float(val) - mi) / (ma - mi) if ma > mi else 0

        for r in rows:
            score = (weights['amount_sum'] * norm(r[col_amt], col_amt) +
                     weights['edge_count'] * norm(r[col_edge], col_edge) +
                     weights['invoice_sum'] * norm(r[col_inv], col_inv) +
                     weights['density'] * norm(r[col_den], col_den)) * 100
            r['strength'] = round(score, 4)
        return rows

    def _update_table_strength(self, table_name: str, col_amt: str, col_edge: str, col_inv: str, col_den: str, target_col: str = 'strength'):
        """从数据库读取数据，计算 Strength 后写回 (按层级归一化)"""
        rows = self.db.execute_query(f"SELECT * FROM {table_name} WHERE chain_name = %s", (self.config.CHAIN_NAME,))
        if not rows: return

        levels = set(r['node_level'] for r in rows)
        final_rows = []
        for lvl in levels:
            lvl_rows = [r for r in rows if r['node_level'] == lvl]
            scored = self._apply_strength_scoring(lvl_rows, col_amt, col_edge, col_inv, col_den)
            for r in scored:
                r[target_col] = r.pop('strength')
            final_rows.extend(scored)

        self.db.execute_dml(f"DELETE FROM {table_name} WHERE chain_name = %s", (self.config.CHAIN_NAME,))
        cols = list(final_rows[0].keys())
        placeholders = ", ".join(["%s"] * len(cols))
        insert_sql = f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({placeholders})"
        data = [tuple(r[c] for c in cols) for r in final_rows]
        self.db.execute_batch(insert_sql, data)

if __name__ == '__main__':
    analyzer = ChainAnalyzer(Config())
    analyzer.run()
