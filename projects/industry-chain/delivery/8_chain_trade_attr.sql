--输入：AM_chain_final_unique_enterprise_2024_ly；tmp_raw_trade_month_AM_2024_ly
--输出：AM_chain_trade_trend_2024_ly


DROP TABLE IF EXISTS temp_enterprise_core;
CREATE TABLE temp_enterprise_core (
  `tax_id` varchar(50) NOT NULL COMMENT '目标企业税号（唯一）',
  `node_type` varchar(50) NOT NULL COMMENT '上/中/下游',
  `map_code` varchar(50) NOT NULL COMMENT '产业链编码',
  `all_chain_codes` varchar(1000) NOT NULL COMMENT '全产业链去重map_code（逗号分隔）'
) ENGINE=OLAP 
UNIQUE KEY(`tax_id`)
DISTRIBUTED BY HASH(`tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "true",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_enterprise_core
WITH all_chain_map_codes AS (
  -- 提取全产业链去重map_code
  SELECT GROUP_CONCAT(DISTINCT map_code SEPARATOR ',') AS all_codes
  FROM AM_chain_final_unique_enterprise_2024_ly
  WHERE map_code IS NOT NULL
)
SELECT DISTINCT
  t.tax_id,
  t.node_type,
  t.map_code,
  a.all_codes AS all_chain_codes
FROM AM_chain_final_unique_enterprise_2024_ly t
CROSS JOIN all_chain_map_codes a
WHERE t.tax_id IS NOT NULL
  AND t.node_type IN ('上游', '中游', '下游')
  AND t.map_code IS NOT NULL;
	
DROP TABLE IF EXISTS temp_trade_amount_dedup;
CREATE TABLE temp_trade_amount_dedup (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `year_mon` varchar(20) NOT NULL,
  `goods_code` varchar(7) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `item_total_amount` decimal(20, 2) NULL,
  `total_amount` decimal(20, 2) NULL COMMENT '去重后的企业对+月份总金额'
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `year_mon`, `goods_code`, `invoice_type`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

INSERT INTO temp_trade_amount_dedup
WITH core_tax AS (
  SELECT DISTINCT tax_id FROM temp_enterprise_core
),
src AS (
  SELECT
    t.from_tax_id, t.to_tax_id, t.year_mon, t.goods_code, t.invoice_type,
    t.item_total_amount, t.total_amount
  FROM tmp_raw_trade_month_AM_2024_ly t
  WHERE t.year_mon IS NOT NULL
    AND (t.item_total_amount > 0 OR t.total_amount > 0)
    AND (
      t.from_tax_id IN (SELECT tax_id FROM core_tax)
      OR t.to_tax_id IN (SELECT tax_id FROM core_tax)
    )
),
pair_max AS (
  SELECT
    from_tax_id, to_tax_id, year_mon, invoice_type,
    MAX(total_amount) AS total_amount
  FROM src
  GROUP BY from_tax_id, to_tax_id, year_mon, invoice_type
)
SELECT
  g.from_tax_id, g.to_tax_id, g.year_mon, g.goods_code, g.invoice_type,
  g.item_total_amount, p.total_amount
FROM src g
JOIN pair_max p
  ON g.from_tax_id = p.from_tax_id
 AND g.to_tax_id = p.to_tax_id
 AND g.year_mon = p.year_mon
 AND g.invoice_type = p.invoice_type;

SET @all_chain_codes = (SELECT GROUP_CONCAT(DISTINCT map_code) FROM temp_enterprise_core);
DROP TABLE IF EXISTS temp_trade_base;
-- 先删除旧表（避免重复）
DROP TABLE IF EXISTS temp_direction_base;
CREATE TABLE temp_direction_base (
  year_mon varchar(20) NOT NULL,
  own_tax_id varchar(50) NOT NULL,
  node_type varchar(50) NOT NULL,
  trade_direction varchar(20) NOT NULL,
  partner_tax_id varchar(100) NOT NULL,
  invoice_type varchar(50) NOT NULL,
  chain_trade_amt decimal(20,2) NOT NULL,
  total_trade_amt decimal(20,2) NOT NULL
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `own_tax_id`, `node_type`, `trade_direction`, `partner_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);
INSERT INTO temp_direction_base
WITH all_chain_code AS (
  SELECT DISTINCT map_code
  FROM temp_enterprise_core
  WHERE map_code IS NOT NULL
),
core_side AS (
  -- 卖出侧：核心企业在 from
  SELECT
    t.year_mon,
    ec.tax_id AS own_tax_id,
    ec.node_type,
    '卖出' AS trade_direction,
    t.to_tax_id AS partner_tax_id,
    t.invoice_type,
    t.goods_code,
    t.item_total_amount,
    t.total_amount,
    ec.map_code AS own_map_code
  FROM temp_trade_amount_dedup t
  JOIN temp_enterprise_core ec
    ON t.from_tax_id = ec.tax_id

  UNION ALL

  -- 买入侧：核心企业在 to
  SELECT
    t.year_mon,
    ec.tax_id AS own_tax_id,
    ec.node_type,
    '买入' AS trade_direction,
    t.from_tax_id AS partner_tax_id,
    t.invoice_type,
    t.goods_code,
    t.item_total_amount,
    t.total_amount,
    ec.map_code AS own_map_code
  FROM temp_trade_amount_dedup t
  JOIN temp_enterprise_core ec
    ON t.to_tax_id = ec.tax_id
)
SELECT
  c.year_mon,
  c.own_tax_id,
  c.node_type,
  c.trade_direction,
  c.partner_tax_id,
  c.invoice_type,
  SUM(
    CASE
      WHEN c.node_type IN ('上游','中游') AND c.goods_code = c.own_map_code THEN c.item_total_amount
      WHEN c.node_type = '下游' AND a.map_code IS NOT NULL THEN c.item_total_amount
      ELSE 0
    END
  ) AS chain_trade_amt,
  SUM(c.total_amount) AS total_trade_amt
FROM core_side c
LEFT JOIN all_chain_code a
  ON c.goods_code = a.map_code
GROUP BY
  c.year_mon, c.own_tax_id, c.node_type,
  c.trade_direction, c.partner_tax_id, c.invoice_type
HAVING
  SUM(
    CASE
      WHEN c.node_type IN ('上游','中游') AND c.goods_code = c.own_map_code THEN c.item_total_amount
      WHEN c.node_type = '下游' AND a.map_code IS NOT NULL THEN c.item_total_amount
      ELSE 0
    END
  ) > 0
  OR SUM(c.total_amount) > 0;

-- 先删除旧表（如果存在）
DROP TABLE IF EXISTS temp_direction_total;

-- 创建temp_direction_total表
CREATE TABLE temp_direction_total (
  `year_mon` varchar(20) NOT NULL,
  `node_type` varchar(50) NOT NULL,
  `trade_direction` varchar(20) NOT NULL,
  `node_chain_total` decimal(20, 2) NOT NULL COMMENT '节点产业链交易总金额',
  `node_total` decimal(20, 2) NOT NULL COMMENT '节点总交易金额'
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `node_type`, `trade_direction`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

-- 插入节点汇总数据（替代窗口函数）
INSERT INTO temp_direction_total
SELECT
  year_mon,
  node_type,
  trade_direction,
  SUM(chain_trade_amt) AS node_chain_total,
  SUM(total_trade_amt) AS node_total
FROM temp_direction_base
GROUP BY year_mon, node_type, trade_direction;

CREATE TABLE temp_trade_base (
  year_mon varchar(20) NOT NULL,
  own_tax_id varchar(50) NOT NULL,
  node_type varchar(50) NOT NULL,
  trade_direction varchar(20) NOT NULL,
  partner_tax_id varchar(100) NOT NULL,
  invoice_type varchar(50) NOT NULL,
  chain_trade_amt decimal(20,2) NOT NULL,
  total_trade_amt decimal(20,2) NOT NULL,
  node_chain_total decimal(20,2) NOT NULL,
  node_total decimal(20,2) NOT NULL
) ENGINE=OLAP
UNIQUE KEY(`year_mon`, `own_tax_id`, `node_type`, `trade_direction`, `partner_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
  "replication_num" = "3",
  "in_memory" = "false",
  "enable_persistent_index" = "false",
  "replicated_storage" = "true",
  "compression" = "LZ4"
);

INSERT INTO temp_trade_base
SELECT
  a.year_mon,
  a.own_tax_id,
  a.node_type,
  a.trade_direction,
  a.partner_tax_id,
  a.invoice_type,
  a.chain_trade_amt,
  a.total_trade_amt,
  b.node_chain_total,
  b.node_total
FROM temp_direction_base a
JOIN temp_direction_total b
  ON a.year_mon = b.year_mon 
  AND a.node_type = b.node_type 
  AND a.trade_direction = b.trade_direction;
	
-- 先删除旧表
DROP TABLE IF EXISTS temp_total_base;
-- 创建普通OLAP表
CREATE TABLE temp_total_base (
  year_mon varchar(20) NOT NULL,
  own_tax_id varchar(50) NOT NULL,
  node_type varchar(50) NOT NULL,
  partner_tax_id varchar(100) NOT NULL,
  invoice_type varchar(50) NOT NULL,
  chain_trade_amt decimal(20,2) NOT NULL,
  total_trade_amt decimal(20,2) NOT NULL
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `own_tax_id`, `node_type`, `partner_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

-- 插入整体维度基础数据（合并买入+卖出）
INSERT INTO temp_total_base
SELECT
  year_mon,
  own_tax_id,
  node_type,
  partner_tax_id,
  invoice_type,
  SUM(chain_trade_amt) AS chain_trade_amt,
  SUM(total_trade_amt) AS total_trade_amt
FROM temp_direction_base
GROUP BY year_mon, own_tax_id, node_type, partner_tax_id, invoice_type;

-- 先删除旧表
DROP TABLE IF EXISTS temp_total_total;
-- 创建普通OLAP表
CREATE TABLE temp_total_total (
  year_mon varchar(20) NOT NULL,
  node_type varchar(50) NOT NULL,
  node_chain_total decimal(20,2) NOT NULL,
  node_total decimal(20,2) NOT NULL
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `node_type`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

-- 插入整体维度节点汇总数据
INSERT INTO temp_total_total
SELECT
  year_mon,
  node_type,
  SUM(chain_trade_amt) AS node_chain_total,
  SUM(total_trade_amt) AS node_total
FROM temp_total_base
GROUP BY year_mon, node_type;

INSERT INTO temp_trade_base
SELECT
  a.year_mon,
  a.own_tax_id,
  a.node_type,
  '整体' AS trade_direction,
  a.partner_tax_id,
  a.invoice_type,
  a.chain_trade_amt,
  a.total_trade_amt,
  b.node_chain_total,
  b.node_total
FROM temp_total_base a
JOIN temp_total_total b
  ON a.year_mon = b.year_mon 
  AND a.node_type = b.node_type;
	
DROP TABLE IF EXISTS temp_direction_base;
DROP TABLE IF EXISTS temp_direction_total;
DROP TABLE IF EXISTS temp_total_base;
DROP TABLE IF EXISTS temp_total_total;

DROP TABLE IF EXISTS temp_trade_concentration;
CREATE TABLE temp_trade_concentration (
  `year_mon` varchar(20) NOT NULL,
  `node_type` varchar(50) NOT NULL,
  `trade_direction` varchar(20) NOT NULL,
  `hhi` decimal(20, 4) NOT NULL COMMENT 'HHI指数（归一化：0~10000）',
  `concentration_level` varchar(20) NOT NULL COMMENT '集中度等级',
  `top5_hhi` decimal(20, 4) NOT NULL COMMENT '前5大对手HHI指数',
  `partner_count` int(11) NOT NULL COMMENT '交易对手总数'
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `node_type`, `trade_direction`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_concentration
WITH partner_amt AS (
  SELECT
    COALESCE(year_mon, 'unknown') AS year_mon, --判断是否为空值
    COALESCE(node_type, 'unknown') AS node_type,
    COALESCE(trade_direction, 'unknown') AS trade_direction,
    COALESCE(partner_tax_id, 'unknown') AS partner_tax_id,
    COALESCE(SUM(CASE WHEN chain_trade_amt < 0 THEN 0 ELSE chain_trade_amt END), 0) AS partner_chain_amt,
    COALESCE(NULLIF(MAX(node_chain_total), 0), 1) AS node_chain_total,
    ROW_NUMBER() OVER(PARTITION BY year_mon, node_type, trade_direction 
                      ORDER BY COALESCE(SUM(chain_trade_amt), 0) DESC) AS rn
  FROM temp_trade_base
  GROUP BY year_mon, node_type, trade_direction, partner_tax_id
),
hhi_all AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    COALESCE(
      SUM(POWER(COALESCE(partner_chain_amt / node_chain_total, 0), 2)) * 10000, 
      0
    ) AS hhi,
    COALESCE(COUNT(DISTINCT partner_tax_id), 0) AS partner_count
  FROM partner_amt
  GROUP BY year_mon, node_type, trade_direction
),
hhi_top5 AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    COALESCE(
      SUM(POWER(COALESCE(partner_chain_amt / node_chain_total, 0), 2)) * 10000, 
      0
    ) AS top5_hhi
  FROM partner_amt
  WHERE rn <= 5
  GROUP BY year_mon, node_type, trade_direction
)
SELECT
  a.year_mon,
  a.node_type,
  a.trade_direction,
  COALESCE(a.hhi, 0) AS hhi,
  CASE
    WHEN COALESCE(a.hhi, 0) < 1000 THEN '低集中度'
    WHEN COALESCE(a.hhi, 0) BETWEEN 1000 AND 2500 THEN '中集中度'
    WHEN COALESCE(a.hhi, 0) > 2500 THEN '高集中度'
    ELSE '低集中度'
  END AS concentration_level,
  COALESCE(b.top5_hhi, 0) AS top5_hhi,
  a.partner_count
FROM hhi_all a
LEFT JOIN hhi_top5 b
  ON a.year_mon = b.year_mon
  AND a.node_type = b.node_type
  AND a.trade_direction = b.trade_direction
ORDER BY a.year_mon, a.node_type, a.trade_direction;

DROP TABLE IF EXISTS temp_trade_pair_month;
CREATE TABLE temp_trade_pair_month (
  `year_mon` varchar(20) NOT NULL,
  `pair_key` varchar(300) NOT NULL COMMENT '唯一键：own_tax_id+node_type+trade_direction+partner_tax_id',
  `own_tax_id` varchar(50) NOT NULL COMMENT '己方目标企业税号',
  `node_type` varchar(50) NOT NULL,
  `trade_direction` varchar(20) NOT NULL,
  `partner_tax_id` varchar(100) NOT NULL,
  `chain_trade_amt` decimal(20, 2) NOT NULL COMMENT '该对手的产业链交易金额'
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `pair_key`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_pair_month
SELECT
  COALESCE(year_mon, 'unknown') AS year_mon,
  COALESCE(CONCAT(own_tax_id, '|', node_type, '|', trade_direction, '|', partner_tax_id), 'unknown|unknown|unknown|unknown') AS pair_key,
  COALESCE(own_tax_id, 'unknown') AS own_tax_id,
  COALESCE(node_type, 'unknown') AS node_type,
  COALESCE(trade_direction, 'unknown') AS trade_direction,
  COALESCE(partner_tax_id, 'unknown') AS partner_tax_id,
  COALESCE(SUM(chain_trade_amt), 0.00) AS chain_trade_amt
FROM temp_trade_base
GROUP BY year_mon, own_tax_id, node_type, trade_direction, partner_tax_id;

DROP TABLE IF EXISTS temp_trade_stability;
CREATE TABLE temp_trade_stability (
  `year_mon` varchar(20) NOT NULL,
  `node_type` varchar(50) NOT NULL,
  `trade_direction` varchar(20) NOT NULL,
  `stable_pair_ratio` decimal(20, 4) NOT NULL COMMENT '过去3个月重复交易对占比（%）',
  `current_month_pair_num` int(11) NOT NULL COMMENT '本月交易对总数',
  `repeat_pair_num` int(11) NOT NULL COMMENT '过去3个月重复的交易对数量',
  `amt_cv` decimal(20, 4) NOT NULL COMMENT '近3个月金额离散系数（下游用全产业链编码）',
  `stable_amt_ratio` decimal(20, 4) NOT NULL COMMENT '重复交易对金额占比（%）'
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `node_type`, `trade_direction`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_stability
-- 步骤1：基础交易对数据（用status2的转换方式）
WITH pair_full AS (
  SELECT
    pair_key,
    node_type,
    trade_direction,
    year_mon,
    chain_trade_amt,
    -- 核心修改：补01日再转换
    STR_TO_DATE(CONCAT(year_mon, '-01'), '%Y-%m-%d') AS month_dt
  FROM temp_trade_pair_month
),
-- 步骤2：自关联获取每个交易对近3个月的所有数据
pair_3month AS (
  SELECT
    curr.pair_key,
    curr.node_type,
    curr.trade_direction,
    curr.year_mon AS curr_year_mon,
    curr.month_dt AS curr_month_dt,
    curr.chain_trade_amt,
    last.year_mon AS last_year_mon
  FROM pair_full curr
  LEFT JOIN pair_full last
    ON curr.pair_key = last.pair_key
    AND curr.node_type = last.node_type
    AND curr.trade_direction = last.trade_direction
    AND last.month_dt >= DATE_SUB(curr.month_dt, INTERVAL 2 MONTH)
    AND last.month_dt <= curr.month_dt
),
-- 步骤3：统计每个交易对+当前月份的近3个月唯一月份数
pair_month_count AS (
  SELECT
    pair_key,
    node_type,
    trade_direction,
    curr_year_mon AS year_mon,
    curr_month_dt AS month_dt,
    chain_trade_amt,
    COUNT(DISTINCT last_year_mon) AS month_count
  FROM pair_3month
  GROUP BY pair_key, node_type, trade_direction, curr_year_mon, curr_month_dt, chain_trade_amt
),
-- 步骤4：判断是否为重复交易对
pair_repeat AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    pair_key,
    chain_trade_amt,
    CASE WHEN month_count >= 2 THEN 1 ELSE 0 END AS is_repeat
  FROM pair_month_count
),
-- 步骤5：月度基础统计
monthly_stats AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    COUNT(DISTINCT pair_key) AS current_month_pair_num,
    SUM(is_repeat) AS repeat_pair_num,
    SUM(CASE WHEN is_repeat=1 THEN chain_trade_amt ELSE 0 END) AS repeat_amt,
    SUM(chain_trade_amt) AS total_amt
  FROM pair_repeat
  GROUP BY year_mon, node_type, trade_direction
),
-- 步骤6：计算金额离散系数（同步修改日期排序）
amt_cv AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    CASE 
      WHEN AVG(chain_trade_amt) = 0 THEN 0 
      ELSE STDDEV(chain_trade_amt) / AVG(chain_trade_amt) 
    END AS amt_cv
  FROM (
    SELECT
      year_mon,
      node_type,
      trade_direction,
      SUM(chain_trade_amt) OVER(
        PARTITION BY node_type, trade_direction
        -- 核心修改：排序的日期转换也用status2的方式
        ORDER BY STR_TO_DATE(CONCAT(year_mon, '-01'), '%Y-%m-%d')
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
      ) AS chain_trade_amt
    FROM temp_trade_pair_month
  ) t
  GROUP BY year_mon, node_type, trade_direction
)
-- 最终汇总
SELECT
  ms.year_mon,
  ms.node_type,
  ms.trade_direction,
  ROUND(IF(ms.current_month_pair_num=0, 0, ms.repeat_pair_num/ms.current_month_pair_num*100),4) AS stable_pair_ratio,
  ms.current_month_pair_num,
  ms.repeat_pair_num,
  COALESCE(ac.amt_cv, 0) AS amt_cv,
  ROUND(IF(ms.total_amt=0, 0, ms.repeat_amt/ms.total_amt*100),4) AS stable_amt_ratio
FROM monthly_stats ms
LEFT JOIN amt_cv ac
  ON ms.year_mon = ac.year_mon
  AND ms.node_type = ac.node_type
  AND ms.trade_direction = ac.trade_direction
ORDER BY ms.year_mon, ms.node_type, ms.trade_direction;

-- 修改最终表创建语句，新增内循环率列
DROP TABLE IF EXISTS AM_chain_trade_trend_2024_ly;
CREATE TABLE AM_chain_trade_trend_2024_ly (
  `year_mon` varchar(20) NOT NULL COMMENT '月份（YYYY-MM）',
  `node_type` varchar(50) NOT NULL COMMENT '上/中/下游',
  `trade_direction` varchar(20) NOT NULL COMMENT '卖出/买入/整体',
  -- 交易金额指标
  `chain_trade_amt` decimal(20, 2) NOT NULL COMMENT '产业链交易总金额',
  `total_trade_amt` decimal(20, 2) NOT NULL COMMENT '总交易金额（产业链+非产业链）',
  `chain_trade_ratio` decimal(20, 4) NOT NULL COMMENT '产业链交易占比（%）',
  -- 交易集中度指标
  `hhi` decimal(20, 4) NOT NULL COMMENT 'HHI指数（归一化：0~10000）',
  `concentration_level` varchar(20) NOT NULL COMMENT '集中度等级',
  `top5_hhi` decimal(20, 4) NOT NULL COMMENT '前5大对手HHI指数',
  `partner_count` int(11) NOT NULL COMMENT '交易对手总数',
  `partner_diversity` decimal(20, 4) NOT NULL COMMENT '伙伴多元化指数（10000/HHI，0兜底）',
  -- 交易稳定度指标
  `stable_pair_ratio` decimal(20, 4) NOT NULL COMMENT '过去3个月重复交易对占比（%）',
  `current_month_pair_num` int(11) NOT NULL COMMENT '本月交易对总数',
  `repeat_pair_num` int(11) NOT NULL COMMENT '过去3个月重复的交易对数量',
  `amt_cv` decimal(20, 4) NOT NULL COMMENT '产业链金额离散系数（近3个月）',
  `stable_amt_ratio` decimal(20, 4) NOT NULL COMMENT '重复交易对金额占比（%）',
  -- 新增：贸易内循环率
  `internal_circulation_ratio` decimal(20, 4) NOT NULL COMMENT '贸易内循环率（%）：产业链内部交易金额/总产业链金额'
) ENGINE=OLAP 
UNIQUE KEY(`year_mon`, `node_type`, `trade_direction`)
DISTRIBUTED BY HASH(`year_mon`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

-- 最终表插入SQL修改（新增内循环率）
INSERT OVERWRITE AM_chain_trade_trend_2024_ly
WITH trade_amt AS (
  SELECT
    year_mon,
    node_type,
    trade_direction,
    SUM(chain_trade_amt) AS chain_trade_amt,
    SUM(total_trade_amt) AS total_trade_amt,
    ROUND(CASE WHEN SUM(total_trade_amt) = 0 THEN 0 ELSE SUM(chain_trade_amt) / SUM(total_trade_amt) * 100 END, 4) AS chain_trade_ratio,
    -- 新增：产业链内部交易金额
    SUM(CASE WHEN partner_tax_id IN (SELECT tax_id FROM temp_enterprise_core) THEN chain_trade_amt ELSE 0 END) AS internal_circulation_amt,
    -- 新增：总产业链金额（复用chain_trade_amt的SUM结果）
    SUM(chain_trade_amt) AS total_chain_amt
  FROM temp_trade_base
  GROUP BY year_mon, node_type, trade_direction
)
SELECT
  a.year_mon,
  a.node_type,
  a.trade_direction,
  a.chain_trade_amt,
  a.total_trade_amt,
  a.chain_trade_ratio,
  COALESCE(c.hhi, 0) AS hhi,
  COALESCE(c.concentration_level, '低集中度') AS concentration_level,
  COALESCE(c.top5_hhi, 0) AS top5_hhi,
  COALESCE(c.partner_count, 0) AS partner_count,
  CASE WHEN COALESCE(c.hhi, 0) = 0 THEN 0 ELSE ROUND(10000 / c.hhi, 4) END AS partner_diversity,
  COALESCE(s.stable_pair_ratio, 0) AS stable_pair_ratio,
  COALESCE(s.current_month_pair_num, 0) AS current_month_pair_num,
  COALESCE(s.repeat_pair_num, 0) AS repeat_pair_num,
  COALESCE(s.amt_cv, 0) AS amt_cv,
  COALESCE(s.stable_amt_ratio, 0) AS stable_amt_ratio,
  -- 新增：贸易内循环率
  ROUND(CASE WHEN a.total_chain_amt = 0 THEN 0 ELSE a.internal_circulation_amt / a.total_chain_amt * 100 END, 4) AS internal_circulation_ratio
FROM trade_amt a
LEFT JOIN temp_trade_concentration c
  ON a.year_mon = c.year_mon
  AND a.node_type = c.node_type
  AND a.trade_direction = c.trade_direction
LEFT JOIN temp_trade_stability s
  ON a.year_mon = s.year_mon
  AND a.node_type = s.node_type
  AND a.trade_direction = s.trade_direction
ORDER BY a.year_mon, a.node_type, a.trade_direction;


DROP TABLE IF EXISTS temp_enterprise_core;
DROP TABLE IF EXISTS temp_trade_amount_dedup;
DROP TABLE IF EXISTS temp_trade_base;
DROP TABLE IF EXISTS temp_trade_pair_month;
DROP TABLE IF EXISTS temp_trade_concentration;
DROP TABLE IF EXISTS temp_trade_stability;
