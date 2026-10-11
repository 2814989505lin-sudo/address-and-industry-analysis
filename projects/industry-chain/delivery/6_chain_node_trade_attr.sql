-- 要改的输入“增材制造产业链”；industry_chain_cls_AM_ly;AM_chain_final_unique_enterprise_2024_ly;node_internal_activity_all_levels_AM_2024_ly
SET @target_chain_name = '增材制造产业链';

WITH mapping_raw AS (
    -- 保持不变
    SELECT DISTINCT
        chain_name,
        node_type,
        node_path,
        map_code
    FROM industry_chain_cls_AM_ly
    WHERE chain_name = @target_chain_name
      AND map_code IS NOT NULL
      AND map_code <> ''
),
path_split AS (
    -- 保持不变
    SELECT
        chain_name,
        node_type,
        node_path,
        map_code,
        case when node_path like '%>%' then NULLIF(TRIM(split_part(node_path, '>', 1)), '') else node_path end AS p1,
        NULLIF(TRIM(split_part(node_path, '>', 2)), '') AS p2,
        NULLIF(TRIM(split_part(node_path, '>', 3)), '') AS p3
    FROM mapping_raw
),
node_map_union AS (
    -- 保持不变
    SELECT
        chain_name,
        '产业链整体' AS 属性,
        chain_name AS 节点,
        '根节点' AS 位置,
        '整体' AS 路径,
        map_code
    FROM path_split

    UNION ALL

    SELECT
        chain_name,
        'node0' AS 属性,
        node_type AS 节点,
        '分类节点' AS 位置,
        node_type AS 路径,
        map_code
    FROM path_split
    WHERE node_type IS NOT NULL
      AND node_type <> ''

    UNION ALL

    SELECT
        chain_name,
        'node1' AS 属性,
        p1 AS 节点,
        node_type AS 位置,
        p1 AS 路径,
        map_code
    FROM path_split
    WHERE p1 IS NOT NULL
      AND p1 <> ''
      AND node_type IS NOT NULL
      AND node_type <> ''

    UNION ALL

    SELECT
        chain_name,
        'node2' AS 属性,
        p2 AS 节点,
        node_type AS 位置,
        CONCAT(p1, '>', p2) AS 路径,
        map_code
    FROM path_split
    WHERE p1 IS NOT NULL
      AND p1 <> ''
      AND p2 IS NOT NULL
      AND p2 <> ''
      AND node_type IS NOT NULL
      AND node_type <> ''

    UNION ALL

    SELECT
        chain_name,
        'node3' AS 属性,
        p3 AS 节点,
        node_type AS 位置,
        CONCAT(p1, '>', p2, '>', p3) AS 路径,
        map_code
    FROM path_split
    WHERE p1 IS NOT NULL
      AND p1 <> ''
      AND p2 IS NOT NULL
      AND p2 <> ''
      AND p3 IS NOT NULL
      AND p3 <> ''
      AND node_type IS NOT NULL
      AND node_type <> ''
),
node_map AS (
    -- 保持不变
    SELECT DISTINCT
        chain_name,
        属性,
        节点,
        位置,
        路径,
        map_code
    FROM node_map_union
),
enterprise_scope AS (
    -- 保持不变
    SELECT
        chain_name,
        map_code,
        province,
        map_name,
        company_name,
        establish_year,
        total_code4_rate,
        final_score,
        tax_id
    FROM AM_chain_final_unique_enterprise_2024_ly
    WHERE chain_name = @target_chain_name
),
node_agg AS (
    -- 保持不变
    SELECT
        nm.chain_name,
        nm.属性,
        nm.节点,
        nm.位置,
        nm.路径,
        COUNT(DISTINCT nm.map_code) AS node_count,
        ROUND(AVG(CASE WHEN e.establish_year != -9999 THEN 2026 - e.establish_year END), 2) AS avg_establish_year,
        COUNT(e.tax_id) AS tax_count,
        ROUND(AVG(e.total_code4_rate), 4) AS avg_total_code4_rate,
        CONCAT(
            ROUND(
                SUM(CASE WHEN e.final_score >= 60 THEN 1 ELSE 0 END) * 100.0
                / NULLIF(COUNT(e.map_code), 0),
                2
            ),
            '%'
        ) AS great_company_rate
    FROM node_map nm
    LEFT JOIN enterprise_scope e
        ON nm.map_code = e.map_code
    GROUP BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径
),
rate_distinct AS (
    -- 保持不变
    SELECT DISTINCT
        avg_total_code4_rate
    FROM node_agg
    WHERE avg_total_code4_rate IS NOT NULL
),
rate_quantile AS (
    -- 保持不变
    SELECT
        avg_total_code4_rate,
        NTILE(3) OVER (ORDER BY avg_total_code4_rate) AS quantile_group
    FROM rate_distinct
),
node_with_level AS (
    -- 保持不变
    SELECT
        n.chain_name,
        n.属性,
        n.节点,
        n.位置,
        n.路径,
        n.node_count,
        n.avg_establish_year,
        n.tax_count,
        n.avg_total_code4_rate,
        CASE
            WHEN rq.quantile_group = 3 THEN 'high'
            WHEN rq.quantile_group = 2 THEN 'medium'
            WHEN rq.quantile_group = 1 THEN 'low'
            ELSE NULL
        END AS industry_relation_level,
        n.great_company_rate
    FROM node_agg n
    LEFT JOIN rate_quantile rq
      ON n.avg_total_code4_rate = rq.avg_total_code4_rate
),
region_stats AS (
    -- 保持不变
    SELECT
        nm.chain_name,
        nm.属性,
        nm.节点,
        nm.位置,
        nm.路径,
        e.province,
        COUNT(*) AS region_count,
        ROUND(
            COUNT(*) * 100.0
            / NULLIF(SUM(COUNT(*)) OVER (PARTITION BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径), 0),
            4
        ) AS region_pct
    FROM node_map nm
    JOIN enterprise_scope e
        ON nm.map_code = e.map_code
    WHERE e.province IS NOT NULL
      AND e.province <> ''
    GROUP BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径, e.province
),
max_region_count AS (
    -- 保持不变
    SELECT
        chain_name,
        属性,
        节点,
        位置,
        路径,
        MAX(region_count) AS max_count
    FROM region_stats
    GROUP BY chain_name, 属性, 节点, 位置, 路径
),
top_region AS (
    -- 保持不变
    SELECT
        rs.chain_name,
        rs.属性,
        rs.节点,
        rs.位置,
        rs.路径,
        GROUP_CONCAT(
            CONCAT(rs.province, ' (', rs.region_pct, '%)')
            ORDER BY rs.province
        ) AS main_region
    FROM region_stats rs
    JOIN max_region_count mrc
      ON rs.chain_name = mrc.chain_name
     AND rs.属性 = mrc.属性
     AND rs.节点 = mrc.节点
     AND rs.位置 = mrc.位置
     AND rs.路径 = mrc.路径
     AND rs.region_count = mrc.max_count
    GROUP BY rs.chain_name, rs.属性, rs.节点, rs.位置, rs.路径
),
product_stats AS (
    -- 保持不变
    SELECT
        nm.chain_name,
        nm.属性,
        nm.节点,
        nm.位置,
        nm.路径,
        e.map_name,
        COUNT(*) AS product_count,
        ROUND(
            COUNT(*) * 100.0
            / NULLIF(SUM(COUNT(*)) OVER (PARTITION BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径), 0),
            4
        ) AS product_pct
    FROM node_map nm
    JOIN enterprise_scope e
        ON nm.map_code = e.map_code
    WHERE e.map_name IS NOT NULL
      AND e.map_name <> ''
    GROUP BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径, e.map_name
),
max_product_count AS (
    -- 保持不变
    SELECT
        chain_name,
        属性,
        节点,
        位置,
        路径,
        MAX(product_count) AS max_count
    FROM product_stats
    GROUP BY chain_name, 属性, 节点, 位置, 路径
),
top_product AS (
    -- 保持不变
    SELECT
        ps.chain_name,
        ps.属性,
        ps.节点,
        ps.位置,
        ps.路径,
        GROUP_CONCAT(
            CONCAT(ps.map_name, ' (', ps.product_pct, '%)')
            ORDER BY ps.map_name
        ) AS main_product
    FROM product_stats ps
    JOIN max_product_count mpc
      ON ps.chain_name = mpc.chain_name
     AND ps.属性 = mpc.属性
     AND ps.节点 = mpc.节点
     AND ps.位置 = mpc.位置
     AND ps.路径 = mpc.路径
     AND ps.product_count = mpc.max_count
    GROUP BY ps.chain_name, ps.属性, ps.节点, ps.位置, ps.路径
),
ranked_enterprises AS (
    -- 保持不变
    SELECT
        nm.chain_name,
        nm.属性,
        nm.节点,
        nm.位置,
        nm.路径,
        e.company_name,
        ROW_NUMBER() OVER (
            PARTITION BY nm.chain_name, nm.属性, nm.节点, nm.位置, nm.路径
            ORDER BY e.total_code4_rate DESC, e.establish_year ASC
        ) AS rank_num
    FROM node_map nm
    JOIN enterprise_scope e
        ON nm.map_code = e.map_code
    WHERE e.establish_year != -9999
      AND e.company_name IS NOT NULL
      AND e.company_name <> ''
),
top_enterprises AS (
    -- 保持不变
    SELECT
        chain_name,
        属性,
        节点,
        位置,
        路径,
        GROUP_CONCAT(
            CONCAT(rank_num, '. ', company_name)
            ORDER BY rank_num
        ) AS top3_enterprises
    FROM ranked_enterprises
    WHERE rank_num <= 3
    GROUP BY chain_name, 属性, 节点, 位置, 路径
),
activity_raw AS (
    -- 保持不变
    SELECT
        chain_name,
        NULLIF(TRIM(node_name), '') AS node_name,
        NULLIF(TRIM(node_path), '') AS node_path,
        node_level,
        NULLIF(TRIM(node_type), '') AS node_type,
        CAST(strength AS DECIMAL(18, 4)) AS strength_value,
        COALESCE(
            NULLIF(TRIM(node_name), ''),
            NULLIF(TRIM(regexp_replace(TRIM(node_path), '^.*>', '')), ''),
            NULLIF(TRIM(node_path), '')
        ) AS node_name_fill,
        CAST(node_level AS VARCHAR) AS node_level_str
    FROM node_internal_activity_all_levels_AM_2024_ly
    WHERE chain_name = @target_chain_name
),
activity_norm AS (
    -- 保持不变
    SELECT
        chain_name,
        node_type,
        node_path,
        MAX(node_name_fill) AS node_name_fill,
        MAX(node_level_str) AS node_level_str,
        MAX(strength_value) AS strength_value
    FROM activity_raw
    WHERE node_path IS NOT NULL
      AND node_path <> ''
      AND node_type IS NOT NULL
      AND node_type <> ''
    GROUP BY chain_name, node_type, node_path
),
result_base AS (
    -- 保持不变
    SELECT
        n.chain_name,
        n.属性,
        n.节点,
        n.位置,
        n.路径,
        n.node_count,
        n.avg_establish_year,
        n.tax_count,
        n.avg_total_code4_rate,
        n.industry_relation_level,
        n.great_company_rate,
        tr.main_region,
        tp.main_product,
        te.top3_enterprises
    FROM node_with_level n
    LEFT JOIN top_region tr
      ON n.chain_name = tr.chain_name
     AND n.属性 = tr.属性
     AND n.节点 = tr.节点
     AND n.位置 = tr.位置
     AND n.路径 = tr.路径
    LEFT JOIN top_product tp
      ON n.chain_name = tp.chain_name
     AND n.属性 = tp.属性
     AND n.节点 = tp.节点
     AND n.位置 = tp.位置
     AND n.路径 = tp.路径
    LEFT JOIN top_enterprises te
      ON n.chain_name = te.chain_name
     AND n.属性 = te.属性
     AND n.节点 = te.节点
     AND n.位置 = te.位置
     AND n.路径 = te.路径
),
-- 新增：计算除 产业链 外其他节点的平均 strength_value
avg_strength_excluding_root AS (
    SELECT AVG(a.strength_value) AS avg_value
    FROM activity_norm a
    WHERE a.node_name_fill != @target_chain_name
      AND a.strength_value IS NOT NULL
),
node_enriched AS (
    SELECT
        rb.chain_name,
        CASE
            WHEN rb.节点 = '增材制造产业链' THEN rb.属性
            WHEN rb.节点 IN ('上游', '中游', '下游') THEN 'node0'
            ELSE COALESCE(
                CASE
                    WHEN a.node_level_str IN ('0', '1', '2', '3') THEN CONCAT('node', a.node_level_str)
                    WHEN a.node_level_str IN ('node0', 'node1', 'node2', 'node3') THEN a.node_level_str
                    ELSE NULL
                END,
                CASE
                    WHEN rb.属性 IN ('0', '1', '2', '3') THEN CONCAT('node', rb.属性)
                    WHEN rb.属性 IN ('node0', 'node1', 'node2', 'node3') THEN rb.属性
                    ELSE rb.属性
                END
            )
        END AS 属性_out,
        CASE
            WHEN rb.节点 IN ('增材制造产业链', '上游', '中游', '下游') THEN rb.节点
            ELSE COALESCE(a.node_name_fill, rb.节点)
        END AS 节点_out,
        CASE
            WHEN rb.节点 IN ('增材制造产业链', '上游', '中游', '下游') THEN rb.位置
            ELSE COALESCE(a.node_type, rb.位置)
        END AS 位置_out,
        CASE
            WHEN rb.节点 IN ('增材制造产业链', '上游', '中游', '下游') THEN rb.路径
            ELSE COALESCE(a.node_path, rb.路径)
        END AS 路径_out,
        rb.node_count,
        rb.avg_establish_year,
        rb.tax_count,
        rb.avg_total_code4_rate,
        rb.industry_relation_level,
        -- 修改这里：如果节点是 'xxx产业链'，则使用平均值，否则使用原始值
        CASE
            WHEN rb.节点 IN ('增材制造产业链') THEN (SELECT avg_value FROM avg_strength_excluding_root)
            ELSE a.strength_value
        END AS strength_out,
        rb.great_company_rate,
        rb.main_region,
        rb.main_product,
        rb.top3_enterprises
    FROM result_base rb
    LEFT JOIN activity_norm a
      ON rb.chain_name = a.chain_name
     AND rb.节点 = a.node_name_fill
),
strength_distinct AS (
    -- 保持不变
    SELECT DISTINCT
        strength_out
    FROM node_enriched
    WHERE strength_out IS NOT NULL
),
strength_quantile AS (
    -- 保持不变
    SELECT
        strength_out,
        NTILE(3) OVER (ORDER BY strength_out) AS quantile_group
    FROM strength_distinct
),
node_with_strength_level AS (
    -- 保持不变
    SELECT
        ne.*,
        CASE
            WHEN sq.quantile_group = 3 THEN 'high'
            WHEN sq.quantile_group = 2 THEN 'medium'
            WHEN sq.quantile_group = 1 THEN 'low'
            ELSE NULL
        END AS strength_level
    FROM node_enriched ne
    LEFT JOIN strength_quantile sq
      ON ne.strength_out = sq.strength_out
)
SELECT
    n.节点_out AS `节点`,
    n.属性_out AS `属性`,
    n.位置_out AS `位置`,
    n.路径_out AS `路径`,
    n.node_count AS `产业节点`,
    n.avg_establish_year AS `企业平均年限`,
    n.tax_count AS `关联企业数`,
    n.avg_total_code4_rate AS `产业活动集中度`,
    n.industry_relation_level AS `产业关联度等级`,
    n.strength_out AS `节点交易活跃度`,
    n.strength_level AS `节点交易活跃度等级`,
    n.great_company_rate AS `优质企业占比`,
    n.main_region AS `主要区域`,
    n.main_product AS `主要商品`,
    n.top3_enterprises AS `头部企业TOP3`
FROM node_with_strength_level n
ORDER BY
    CASE
        WHEN n.属性_out = '产业链整体' THEN 1
        WHEN n.属性_out = 'node0' THEN 2
        WHEN n.属性_out = 'node1' THEN 3
        WHEN n.属性_out = 'node2' THEN 4
        WHEN n.属性_out = 'node3' THEN 5
        ELSE 99
    END,
    CASE
        WHEN n.位置_out = '根节点' THEN 1
        WHEN n.位置_out = '分类节点' THEN 2
        WHEN n.位置_out = '上游' THEN 3
        WHEN n.位置_out = '中游' THEN 4
        WHEN n.位置_out = '下游' THEN 5
        ELSE 99
    END,
    n.路径_out,
    n.节点_out;