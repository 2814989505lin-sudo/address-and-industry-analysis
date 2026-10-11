-- 会留下AM_chain_final_unique_enterprise_2025_ly
-- 输入AM_chain_enter_attr_2025_ly
-- TRUNCATE table temp_enter_attr_with_flags_ly;
-- 一辅助指标
-- 临时表：存储原始列 + 核心辅助标记（无复杂计算，解析快）
CREATE TABLE IF NOT EXISTS temp_enter_attr_with_flags_ly AS
SELECT 
  t.*,
  -- 1.1 全局双向参与
  MAX(CASE WHEN t.invoice_type IN ('sales', 'industry_sales') THEN 1 ELSE 0 END) OVER (PARTITION BY t.tax_id)
  * MAX(CASE WHEN t.invoice_type IN ('income', 'industry_income') THEN 1 ELSE 0 END) OVER (PARTITION BY t.tax_id)
  AS has_both_sales_income,
  -- 1.2 子链内双向参与
  MAX(CASE WHEN t.invoice_type IN ('sales', 'industry_sales') THEN 1 ELSE 0 END) OVER (PARTITION BY t.tax_id, t.map_code)
  * MAX(CASE WHEN t.invoice_type IN ('income', 'industry_income') THEN 1 ELSE 0 END) OVER (PARTITION BY t.tax_id, t.map_code)
  AS sub_chain_has_both,
  -- 1.3 税务评级系数
  CASE 
    WHEN t.tax_rating_level = 'A' THEN 1.0
    WHEN t.tax_rating_level = 'B' THEN 0.8
    WHEN t.tax_rating_level = 'C' THEN 0.5
    WHEN t.tax_rating_level IN ('D', 'M') THEN 0.3
    ELSE 0.4
  END AS tax_rating_coeff,
  -- 1.4 发票规模系数
  CASE 
    WHEN t.invoice_size = '大型' THEN 1.0
    WHEN t.invoice_size = '中型' THEN 0.8
    WHEN t.invoice_size = '小型' THEN 0.6
    WHEN t.invoice_size = '微型' THEN 0.4
    ELSE 0.5
  END AS invoice_size_coeff
FROM AM_chain_enter_attr_2025_ly t;


-- 二打分机制
-- TRUNCATE TABLE chain_final_with_all_cols_ly;

-- 最终整合表：临时表 + 4个得分列（SQL短，无重复计算）
CREATE TABLE IF NOT EXISTS chain_final_with_all_cols_ly AS
SELECT 
  t.*,
  
  -- #################### 1. 子链相关度得分（按data_source分场景）####################
  ROUND(
    CASE 
      -- 场景1：data_source=国民经济行业分类（原下游企业）
      WHEN t.data_source = '国民经济行业分类' THEN
        (1.0 * 50)  -- code4_rate=1.0
        + t.sub_chain_has_both * 30  -- 直接用临时表的子链双向标记
        + t.invoice_size_coeff * 20  -- 直接用临时表的规模系数
      
      -- 场景2：data_source=商品分类（原非下游企业）
      WHEN t.data_source = '商品分类' THEN
        (t.code4_rate / NULLIF(MAX(t.code4_rate) OVER (PARTITION BY t.chain_name), 0)) * 40
        + (t.code4_amount / NULLIF(MAX(t.code4_amount) OVER (PARTITION BY t.chain_name, t.map_code), 0)) * 30
        + t.sub_chain_has_both * 20
        + t.invoice_size_coeff * 10
      
      -- 其他data_source默认按商品分类逻辑
      ELSE
        (t.code4_rate / NULLIF(MAX(t.code4_rate) OVER (PARTITION BY t.chain_name), 0)) * 40
        + (t.code4_amount / NULLIF(MAX(t.code4_amount) OVER (PARTITION BY t.chain_name, t.map_code), 0)) * 30
        + t.sub_chain_has_both * 20
        + t.invoice_size_coeff * 10
    END,
    2
  ) AS sub_chain_score,
  
  -- #################### 2. 全产业链相关度得分 ####################
  ROUND(
    (t.total_code4_rate / NULLIF(MAX(t.total_code4_rate) OVER (PARTITION BY t.chain_name), 0)) * 40
    + (t.total_code4_amount / NULLIF(MAX(t.total_code4_amount) OVER (PARTITION BY t.chain_name), 0)) * 30
    + (t.node_num / NULLIF(MAX(t.node_num) OVER (PARTITION BY t.chain_name), 0)) * 15
    + t.has_both_sales_income * 15,  -- 直接用临时表的全局双向标记
    2
  ) AS total_chain_score,
  
  -- #################### 3. 发票经营健康度得分 ####################
  ROUND(
    (1 - COALESCE(t.sales_abnormal_amt_rate, 0)) * 40 * t.tax_rating_coeff  -- 直接用临时表的税务系数
    + (1 - COALESCE(t.income_abnormal_amt_rate, 0)) * 40 * t.tax_rating_coeff
    + t.tax_rating_coeff * 20,
    2
  ) AS invoice_health_score,
  
  -- #################### 4. 最终综合得分 ####################
  ROUND(
    -- 基础综合分（子链30% + 全链40% + 健康度30%）
    (
      CASE 
        WHEN t.data_source = '国民经济行业分类' THEN
          (1.0 * 50) + t.sub_chain_has_both * 30 + t.invoice_size_coeff * 20
        ELSE
          (t.code4_rate / NULLIF(MAX(t.code4_rate) OVER (PARTITION BY t.chain_name), 0)) * 40
          + (t.code4_amount / NULLIF(MAX(t.code4_amount) OVER (PARTITION BY t.chain_name, t.map_code), 0)) * 30
          + t.sub_chain_has_both * 20
          + t.invoice_size_coeff * 10
      END
    ) * 0.3
    + (
      (t.total_code4_rate / NULLIF(MAX(t.total_code4_rate) OVER (PARTITION BY t.chain_name), 0)) * 40
      + (t.total_code4_amount / NULLIF(MAX(t.total_code4_amount) OVER (PARTITION BY t.chain_name), 0)) * 30
      + (t.node_num / NULLIF(MAX(t.node_num) OVER (PARTITION BY t.chain_name), 0)) * 15
      + t.has_both_sales_income * 15
    ) * 0.4
    + (
      (1 - COALESCE(t.sales_abnormal_amt_rate, 0)) * 40 * t.tax_rating_coeff
      + (1 - COALESCE(t.income_abnormal_amt_rate, 0)) * 40 * t.tax_rating_coeff
      + t.tax_rating_coeff * 20
    ) * 0.3
    -- 异常率梯度扣分
    * CASE 
        WHEN COALESCE(t.sales_abnormal_amt_rate, 0) > 0.16 OR COALESCE(t.income_abnormal_amt_rate, 0) > 0.16 THEN 0.8
        WHEN COALESCE(t.sales_abnormal_amt_rate, 0) > 0.1 OR COALESCE(t.income_abnormal_amt_rate, 0) > 0.1 THEN 0.9
        WHEN COALESCE(t.sales_abnormal_amt_rate, 0) > 0.05 OR COALESCE(t.income_abnormal_amt_rate, 0) > 0.05 THEN 0.95
        ELSE 1.0
      END,
    2
  ) AS final_score

FROM temp_enter_attr_with_flags_ly t;

-- （可选）删除临时表（节省空间，执行成功后再删）
-- DROP TABLE temp_enter_attr_with_flags_ly;


-- 三筛选机制

-- #######################################
-- 步骤1：划分子链类型（大众/小众）- 不变
-- #######################################
CREATE TABLE IF NOT EXISTS map_code_enterprise_count_ly AS
SELECT
  map_code,
  map_name,
  COUNT(DISTINCT tax_id) AS enterprise_count
FROM chain_final_with_all_cols_ly
WHERE map_code IS NOT NULL
GROUP BY map_code, map_name;

CREATE TABLE IF NOT EXISTS map_code_type_ly AS
SELECT
  m.*,
  CASE
    WHEN m.enterprise_count <= (
      SELECT threshold FROM (
        SELECT
          enterprise_count AS threshold,
          ROW_NUMBER() OVER (ORDER BY enterprise_count ASC) AS rn,
          COUNT(*) OVER () AS total_rows
        FROM map_code_enterprise_count_ly
      ) t WHERE rn = CEIL(total_rows * 0.3)
    ) THEN '小众子链'
    ELSE '大众子链'
  END AS map_code_type
FROM map_code_enterprise_count_ly m;

-- 步骤1.3：计算所有动态阈值 - 不变
CREATE TABLE IF NOT EXISTS attr_distribution_stats_ly AS
SELECT
  -- 大众子链阈值（严格）
  MAX(CASE WHEN indicator = 'sub_chain_score_p20' THEN threshold END) AS sub_chain_score_p20,
  MAX(CASE WHEN indicator = 'total_code4_rate_p50' THEN threshold END) AS total_code4_rate_p50,
  MAX(CASE WHEN indicator = 'code4_rate_goods_p30' THEN threshold END) AS code4_rate_goods_p30,
  MAX(CASE WHEN indicator = 'health_score_p30' THEN threshold END) AS health_score_p30,
  MAX(CASE WHEN indicator = 'total_chain_score_p30' THEN threshold END) AS total_chain_score_p30,
  -- 小众子链阈值（放宽）
  MAX(CASE WHEN indicator = 'sub_chain_score_p50' THEN threshold END) AS sub_chain_score_p50,
  MAX(CASE WHEN indicator = 'total_code4_rate_p30' THEN threshold END) AS total_code4_rate_p30,
  MAX(CASE WHEN indicator = 'code4_rate_goods_p10' THEN threshold END) AS code4_rate_goods_p10,
  MAX(CASE WHEN indicator = 'health_score_p20' THEN threshold END) AS health_score_p20,
  MAX(CASE WHEN indicator = 'total_chain_score_p20' THEN threshold END) AS total_chain_score_p20
FROM (
  -- 大众子链阈值（5个）
  SELECT 'sub_chain_score_p20' AS indicator, sub_chain_score AS threshold FROM (SELECT sub_chain_score, ROW_NUMBER() OVER (ORDER BY sub_chain_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE sub_chain_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.2)
  UNION ALL
  SELECT 'total_code4_rate_p50' AS indicator, total_code4_rate AS threshold FROM (SELECT total_code4_rate, ROW_NUMBER() OVER (ORDER BY total_code4_rate ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE (total_code4_rate > 0 OR total_code4_amount > 0) AND total_code4_rate IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.5)
  UNION ALL
  SELECT 'code4_rate_goods_p30' AS indicator, code4_rate AS threshold FROM (SELECT code4_rate, ROW_NUMBER() OVER (ORDER BY code4_rate ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE data_source = '商品分类' AND (code4_rate > 0 OR code4_amount > 0) AND code4_rate IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.3)
  UNION ALL
  SELECT 'health_score_p30' AS indicator, invoice_health_score AS threshold FROM (SELECT invoice_health_score, ROW_NUMBER() OVER (ORDER BY invoice_health_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE invoice_health_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.3)
  UNION ALL
  SELECT 'total_chain_score_p30' AS indicator, total_chain_score AS threshold FROM (SELECT total_chain_score, ROW_NUMBER() OVER (ORDER BY total_chain_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE total_chain_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.3)
  -- 小众子链阈值（5个）
  UNION ALL
  SELECT 'sub_chain_score_p50' AS indicator, sub_chain_score AS threshold FROM (SELECT sub_chain_score, ROW_NUMBER() OVER (ORDER BY sub_chain_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE sub_chain_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.5)
  UNION ALL
  SELECT 'total_code4_rate_p30' AS indicator, total_code4_rate AS threshold FROM (SELECT total_code4_rate, ROW_NUMBER() OVER (ORDER BY total_code4_rate ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE (total_code4_rate > 0 OR total_code4_amount > 0) AND total_code4_rate IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.3)
  UNION ALL
  SELECT 'code4_rate_goods_p10' AS indicator, code4_rate AS threshold FROM (SELECT code4_rate, ROW_NUMBER() OVER (ORDER BY code4_rate ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE data_source = '商品分类' AND (code4_rate > 0 OR code4_amount > 0) AND code4_rate IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.1)
  UNION ALL
  SELECT 'health_score_p20' AS indicator, invoice_health_score AS threshold FROM (SELECT invoice_health_score, ROW_NUMBER() OVER (ORDER BY invoice_health_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE invoice_health_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.2)
  UNION ALL
  SELECT 'total_chain_score_p20' AS indicator, total_chain_score AS threshold FROM (SELECT total_chain_score, ROW_NUMBER() OVER (ORDER BY total_chain_score ASC) AS rn, COUNT(*) OVER () AS total_rows FROM chain_final_with_all_cols_ly WHERE total_chain_score IS NOT NULL) t WHERE rn = CEIL(total_rows * 0.2)
) all_quantiles;

CREATE TABLE IF NOT EXISTS filter_thresholds_ly_final AS
SELECT
  sub_chain_score_p20, total_code4_rate_p50, code4_rate_goods_p30, health_score_p30, total_chain_score_p30,
  sub_chain_score_p50, total_code4_rate_p30, code4_rate_goods_p10, health_score_p20, total_chain_score_p20
FROM attr_distribution_stats_ly;

-- #######################################
-- 步骤2：分级筛选 - 不变
-- #######################################
CREATE TABLE IF NOT EXISTS high_quality_public_ly AS
SELECT
  t.*,
  '大众子链-高质量企业' AS enterprise_type,
  '大众子链' AS map_code_type
FROM chain_final_with_all_cols_ly t
INNER JOIN map_code_type_ly m ON t.map_code = m.map_code
CROSS JOIN filter_thresholds_ly_final th
WHERE
  m.map_code_type = '大众子链'
  AND t.sub_chain_score >= th.sub_chain_score_p20
  AND t.total_code4_rate >= th.total_code4_rate_p50
  AND (t.data_source = '国民经济行业分类' OR (t.data_source = '商品分类' AND t.code4_rate >= th.code4_rate_goods_p30))
  AND t.invoice_health_score >= th.health_score_p30
  AND t.total_chain_score >= th.total_chain_score_p30
  AND t.total_code4_amount > 0
  AND (t.data_source != '商品分类' OR t.code4_amount > 0);

CREATE TABLE IF NOT EXISTS qualified_private_ly AS
SELECT
  t.*,
  '小众子链-合格企业' AS enterprise_type,
  '小众子链' AS map_code_type
FROM chain_final_with_all_cols_ly t
INNER JOIN map_code_type_ly m ON t.map_code = m.map_code
CROSS JOIN filter_thresholds_ly_final th
WHERE
  m.map_code_type = '小众子链'
  AND t.sub_chain_score >= th.sub_chain_score_p50
  AND t.total_code4_rate >= th.total_code4_rate_p30
  AND (t.data_source = '国民经济行业分类' OR (t.data_source = '商品分类' AND t.code4_rate >= th.code4_rate_goods_p10))
  AND t.invoice_health_score >= th.health_score_p20
  AND t.total_chain_score >= th.total_chain_score_p20
  AND t.total_code4_amount > 0
  AND (t.data_source != '商品分类' OR t.code4_amount > 0);

CREATE TABLE IF NOT EXISTS merged_qualified_ly AS
SELECT * FROM high_quality_public_ly
UNION ALL
SELECT * FROM qualified_private_ly;

-- #######################################
-- 步骤3：子链保底（核心修复：直接关联map_code_type获取子链类型）
-- #######################################
-- 步骤3.1：找出仍无企业的子链（仅保留map_code和map_name，不存map_code_type）
CREATE TABLE IF NOT EXISTS empty_map_code_ly AS
SELECT
  m.map_code,
  m.map_name
FROM map_code_type_ly m
WHERE
  m.map_code IS NOT NULL
  AND m.map_code NOT IN (SELECT DISTINCT map_code FROM merged_qualified_ly WHERE map_code IS NOT NULL);

-- 重新创建保底企业表：去掉rn列，字段顺序和merged_qualified完全一致
CREATE TABLE IF NOT EXISTS guaranteed_enterprise_new_ly AS
SELECT
  -- 按merged_qualified的字段顺序，逐一列出（不含rn）
  t.chain_name,
  t.map_code,
  t.tax_id,
  t.invoice_type,
  t.node1,
  t.node2,
  t.node3,
  t.node_type,
  t.node_path,
  t.map_name,
  t.data_source,
  t.invoice_amount,
  t.invoice_amount_range,
  t.code4_amount,
  t.code4_rate,
  t.node_num,
  t.total_code4_amount,
  t.total_code4_rate,
  t.company_name,
  t.register_capital,
  t.establish_year,
  t.invoice_size,
  t.tax_rating_level,
  t.sales_invoice_amount,
  t.income_invoice_amount,
  t.sales_abnormal_amt_rate,
  t.income_abnormal_amt_rate,
  t.industry1,
  t.industry2,
  t.industry3,
  t.industry4,
  t.province,
  t.province_short,
  t.city,
  t.district,
  t.id,
  t.update_time,
  t.has_both_sales_income,
  t.sub_chain_has_both,
  t.tax_rating_coeff,
  t.invoice_size_coeff,
  t.sub_chain_score,
  t.total_chain_score,
  t.invoice_health_score,
  t.final_score,
  -- 最后两列和merged_qualified对齐：enterprise_type、map_code_type
  CONCAT(mc.map_code_type, '-保底企业') AS enterprise_type,
  mc.map_code_type AS map_code_type
FROM (
  SELECT
    raw.*,  -- 原始表所有列（不含rn）
    ROW_NUMBER() OVER (PARTITION BY raw.map_code ORDER BY
      raw.final_score DESC,
      raw.sub_chain_score DESC,
      raw.invoice_health_score DESC
    ) AS rn  -- rn仅用于排序，不保留到最终表
  FROM chain_final_with_all_cols_ly raw
  INNER JOIN empty_map_code_ly em ON raw.map_code = em.map_code
  WHERE
    raw.invoice_health_score >= 25
    AND raw.total_code4_rate > 0
    AND (raw.data_source != '商品分类' OR raw.code4_rate > 0)
    AND raw.total_code4_amount > 0
) t
INNER JOIN map_code_type_ly mc ON t.map_code = mc.map_code
WHERE t.rn = 1;

-- 删除旧的guaranteed_enterprise（多了rn列）
DROP TABLE IF EXISTS guaranteed_enterprise_ly;
-- 重命名新表为guaranteed_enterprise（保持后续引用一致）
ALTER TABLE guaranteed_enterprise_new_ly RENAME guaranteed_enterprise_ly;


-- #######################################
-- 步骤4：最终合并+去重（多企业子链补选优先级调整，确保Q848438433覆盖）
-- #######################################
CREATE TABLE IF NOT EXISTS AM_chain_final_unique_enterprise_2025_ly AS
WITH all_distinct_data AS (
  -- 先去重获取所有唯一的企业数据
  SELECT DISTINCT tax_id, sub_chain_score, invoice_health_score
  FROM (
    SELECT sub_chain_score, invoice_health_score, tax_id FROM merged_qualified_ly
    UNION ALL
    SELECT sub_chain_score, invoice_health_score, tax_id FROM guaranteed_enterprise_ly
  ) temp
  WHERE sub_chain_score IS NOT NULL AND invoice_health_score IS NOT NULL
),
score_with_rank AS (
  -- 计算每个得分的分位数等级
  SELECT 
    tax_id,
    sub_chain_score,
    invoice_health_score,
    -- 产业关联度等级：按分数从高到低分成A、B、C、D
    CASE 
      WHEN NTILE(4) OVER (ORDER BY sub_chain_score DESC) = 1 THEN 'A'
      WHEN NTILE(4) OVER (ORDER BY sub_chain_score DESC) = 2 THEN 'B'
      WHEN NTILE(4) OVER (ORDER BY sub_chain_score DESC) = 3 THEN 'C'
      ELSE 'D'
    END AS industry_relevance_level,
    -- 企业经营评级：按分数从高到低分成A、B、C、D
    CASE 
      WHEN NTILE(4) OVER (ORDER BY invoice_health_score DESC) = 1 THEN 'A'
      WHEN NTILE(4) OVER (ORDER BY invoice_health_score DESC) = 2 THEN 'B'
      WHEN NTILE(4) OVER (ORDER BY invoice_health_score DESC) = 3 THEN 'C'
      ELSE 'D'
    END AS business_operation_level
  FROM all_distinct_data
)
SELECT
  final.*,
  -- 提取node_path中最后一个">"后的内容作为last_node
  -- 方法3: regexp_extract
  CASE 
    WHEN node_path IS NOT NULL AND node_path != '' THEN
      regexp_extract(node_path, '([^>]+)$', 1)
    ELSE NULL
  END as last_node,
  sr.industry_relevance_level,
  sr.business_operation_level,
  CASE
    WHEN final.enterprise_type LIKE '%保底企业' THEN '子链保底企业'
    WHEN final.map_code_type = '大众子链' 
         AND final.sub_chain_score >= (SELECT sub_chain_score_p20 FROM filter_thresholds_ly_final)
         AND final.total_code4_rate >= (SELECT total_code4_rate_p50 FROM filter_thresholds_ly_final)
    THEN '子链+全链双强企业'
    WHEN final.map_code_type = '大众子链' 
         AND final.sub_chain_score >= (SELECT sub_chain_score_p20 FROM filter_thresholds_ly_final)
         AND final.total_code4_rate < (SELECT total_code4_rate_p50 FROM filter_thresholds_ly_final)
    THEN '子链专精企业'
    WHEN final.map_code_type = '小众子链' THEN '小众子链核心企业'
    ELSE '全链强企业'
  END AS enterprise_value_type,
  '最优匹配子链' AS sub_chain_match_flag
FROM (
  SELECT
    t.*,
    ROW_NUMBER() OVER (
      PARTITION BY t.tax_id
      ORDER BY
        CASE WHEN t.is_single_enterprise_map = 1 THEN 0 
             WHEN t.map_code = 'Q848438433' THEN 1
             ELSE 2 END,
        CASE WHEN t.map_code_type = '大众子链' THEN 3 ELSE 4 END,
        CASE WHEN t.enterprise_type LIKE '%高质量企业' THEN 5 WHEN t.enterprise_type LIKE '%合格企业' THEN 6 ELSE 7 END,
        t.final_score DESC
    ) AS rn_tax
  FROM (
    SELECT
      merged_without_dup.*,
      COUNT(*) OVER (PARTITION BY merged_without_dup.map_code) AS map_enterprise_count,
      CASE WHEN COUNT(*) OVER (PARTITION BY merged_without_dup.map_code) = 1 THEN 1 ELSE 0 END AS is_single_enterprise_map
    FROM (
      SELECT DISTINCT * FROM (SELECT * FROM merged_qualified_ly UNION ALL SELECT * FROM guaranteed_enterprise_ly) temp
    ) merged_without_dup
  ) t
) final
LEFT JOIN score_with_rank sr ON final.tax_id = sr.tax_id
WHERE final.rn_tax = 1;

-- （可选）删除临时表
DROP TABLE map_code_enterprise_count_ly;
DROP TABLE map_code_type_ly;
DROP TABLE attr_distribution_stats_ly;
DROP TABLE filter_thresholds_ly_final;
DROP TABLE high_quality_public_ly;
DROP TABLE qualified_private_ly;
DROP TABLE merged_qualified_ly;
DROP TABLE empty_map_code_ly;
DROP TABLE guaranteed_enterprise_ly;
DROP TABLE temp_enter_attr_with_flags_ly;
DROP TABLE chain_final_with_all_cols_ly;

