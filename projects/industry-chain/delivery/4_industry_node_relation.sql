-- 目标：构建增材制造产业链节点关系表 industry_node_relation_PI_ly
-- 说明：
-- 1) 该脚本面向 Doris/StarRocks 方言（ENGINE=OLAP）
-- 2) node_id / parent_id 使用统一哈希口径，确保可复算
-- 3) 默认按 chain_name = '精密仪器设备产业链' 过滤，如需全量可去掉对应 WHERE 条件
-- 要修改：industry_node_relation_PI_ly；industry_chain_cls_PrecisionInstrument_ly2；WHERE chain_name = '精密仪器设备产业链'

CREATE TABLE IF NOT EXISTS `industry_node_relation_PI_ly` (
  `node_id` bigint(20) NOT NULL COMMENT '主键，基于完整路径的 Hash 值',
  `node_name` varchar(255) NULL COMMENT '本级名称（不含父级，如：减速器）',
  `node_path` varchar(1000) NULL COMMENT '截至到本级的路径（如：核心零部件>减速器）',
  `node_type` varchar(50) NULL COMMENT '节点类型（上/中/下游）',
  `parent_id` bigint(20) NULL COMMENT '父节点的 ID',
  `node_level` int(11) NULL COMMENT '层级 (1, 2, 3)',
  `is_leaf` tinyint(4) NULL COMMENT '是否叶子节点（1=是, 0=否）',
  `chain_name` varchar(50) NULL COMMENT '产业链名称',
  `update_time` datetime NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=OLAP
UNIQUE KEY(`node_id`)
DISTRIBUTED BY HASH(`node_id`) BUCKETS 10
PROPERTIES (
  'replication_num' = '1',
  'in_memory' = 'false',
  'enable_persistent_index' = 'false',
  'replicated_storage' = 'true',
  'compression' = 'LZ4'
);

-- 幂等重跑建议：每次装载前先清空目标表
TRUNCATE TABLE industry_node_relation_PI_ly;

INSERT INTO industry_node_relation_PI_ly
(
  node_id,
  node_name,
  node_path,
  node_type,
  parent_id,
  node_level,
  is_leaf,
  chain_name,
  update_time
)
WITH src AS (
    SELECT DISTINCT
        chain_name,
        NULLIF(TRIM(node_type), '') AS node_type,
        case when node_path like '%>%' then NULLIF(TRIM(split_part(TRIM(node_path), '>', 1)), '') else node_path end AS p1,
        NULLIF(TRIM(split_part(TRIM(node_path), '>', 2)), '') AS p2,
        NULLIF(TRIM(split_part(TRIM(node_path), '>', 3)), '') AS p3
    FROM industry_chain_cls_PrecisionInstrument_ly2
    WHERE chain_name = '精密仪器设备产业链'
      AND node_path IS NOT NULL
      AND TRIM(node_path) <> ''
      AND node_type IS NOT NULL
      AND TRIM(node_type) <> ''
),
node_l1 AS (
    SELECT DISTINCT
        chain_name,
        node_type,
        p1 AS node_name,
        p1 AS node_path,
        1 AS node_level
    FROM src
    WHERE p1 IS NOT NULL
),
node_l2 AS (
    SELECT DISTINCT
        chain_name,
        node_type,
        p2 AS node_name,
        CONCAT(p1, '>', p2) AS node_path,
        2 AS node_level
    FROM src
    WHERE p1 IS NOT NULL
      AND p2 IS NOT NULL
),
node_l3 AS (
    SELECT DISTINCT
        chain_name,
        node_type,
        p3 AS node_name,
        CONCAT(p1, '>', p2, '>', p3) AS node_path,
        3 AS node_level
    FROM src
    WHERE p1 IS NOT NULL
      AND p2 IS NOT NULL
      AND p3 IS NOT NULL
),
all_nodes AS (
    SELECT * FROM node_l1
    UNION ALL
    SELECT * FROM node_l2
    UNION ALL
    SELECT * FROM node_l3
),
nodes_with_parent_path AS (
    SELECT
        chain_name,
        node_type,
        node_name,
        node_path,
        node_level,
        CASE
            WHEN node_level = 1 THEN NULL
            WHEN node_level = 2 THEN NULLIF(TRIM(split_part(node_path, '>', 1)), '')
            WHEN node_level = 3 THEN CONCAT(
                NULLIF(TRIM(split_part(node_path, '>', 1)), ''),
                '>',
                NULLIF(TRIM(split_part(node_path, '>', 2)), '')
            )
            ELSE NULL
        END AS parent_path
    FROM all_nodes
),
leaf_flag AS (
    SELECT
        n.chain_name,
        n.node_type,
        n.node_path,
        CASE
            WHEN cp.node_path IS NULL THEN 1
            ELSE 0
        END AS is_leaf
    FROM all_nodes n
    LEFT JOIN (
        SELECT DISTINCT
            chain_name,
            node_type,
            parent_path AS node_path
        FROM nodes_with_parent_path
        WHERE parent_path IS NOT NULL
    ) cp
      ON n.chain_name = cp.chain_name
     AND n.node_type = cp.node_type
     AND n.node_path = cp.node_path
)
SELECT
    CAST(CONV(SUBSTRING(MD5(CONCAT(nwp.chain_name, '|', nwp.node_type, '|', nwp.node_path)), 1, 15), 16, 10) AS BIGINT) AS node_id,
    nwp.node_name,
    nwp.node_path,
    nwp.node_type,
    CASE
        WHEN nwp.parent_path IS NULL THEN NULL
        ELSE CAST(CONV(SUBSTRING(MD5(CONCAT(nwp.chain_name, '|', nwp.node_type, '|', nwp.parent_path)), 1, 15), 16, 10) AS BIGINT)
    END AS parent_id,
    nwp.node_level,
    lf.is_leaf,
    nwp.chain_name,
    NOW() AS update_time
FROM nodes_with_parent_path nwp
JOIN leaf_flag lf
  ON nwp.chain_name = lf.chain_name
 AND nwp.node_type = lf.node_type
 AND nwp.node_path = lf.node_path;
