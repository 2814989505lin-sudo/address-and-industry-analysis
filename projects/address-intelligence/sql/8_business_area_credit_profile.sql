-- 场景二：从商圈出发，生成商圈画像与三色评级结果表
-- 留空的字段用0代替，这也可能是像area_activity_ratio这样的字段偏小的原因

DROP TABLE IF EXISTS fintech_stat.business_area_credit_profile_yuexiu_ly;

CREATE TABLE fintech_stat.business_area_credit_profile_yuexiu_ly (
    business_area VARCHAR(255) COMMENT '商圈名称',
    area_company_count BIGINT COMMENT '商圈企业总数',
    area_major_l2_name VARCHAR(1000) COMMENT '主导大类Top3',
    area_major_l3_name VARCHAR(1000) COMMENT '主导中类Top3',
    area_valid_count BIGINT COMMENT '有效存续企业总数',
    area_valid_ratio DECIMAL(10, 4) COMMENT '有效存续占比',
    area_shell_ratio DECIMAL(10, 4) COMMENT '空壳率',
    area_risk_ratio DECIMAL(10, 4) COMMENT '违规/异常/冻结密度',
    area_activity_ratio DECIMAL(10, 4) COMMENT '经营活性占比',
    area_top3_l2_company_sum BIGINT COMMENT '主导大类Top3企业数合计',
    area_top3_l2_concentration DECIMAL(10, 4) COMMENT '主导大类Top3集中度',
    area_health_score INT COMMENT '健康度积分',
    area_risk_score INT COMMENT '风险度积分',
    area_grade VARCHAR(4) COMMENT '商圈三色评级'
) ENGINE=OLAP
DUPLICATE KEY (business_area)
DISTRIBUTED BY HASH (business_area) BUCKETS 10
PROPERTIES (
    "compression" = "LZ4",
    "fast_schema_evolution" = "true",
    "replicated_storage" = "true",
    "replication_num" = "1"
);

INSERT INTO fintech_stat.business_area_credit_profile_yuexiu_ly
WITH area_dim AS (
    -- 商圈维表：提供商圈名称、包含的 location_label 列表以及主导行业统计
    SELECT
        business_area,
        labels,
        COALESCE(company_count, 0) AS area_company_count,
        major_l2_name AS area_major_l2_name,
        major_l3_name AS area_major_l3_name,
        COALESCE(CAST(REGEXP_EXTRACT(SPLIT_PART(REPLACE(COALESCE(customer_industry_l2_names_stat, ''), '，', ','), ',', 1), '\\((\\d+)\\)', 1) AS BIGINT), 0) AS area_top1_l2_company_cnt,
        COALESCE(CAST(REGEXP_EXTRACT(SPLIT_PART(REPLACE(COALESCE(customer_industry_l2_names_stat, ''), '，', ','), ',', 2), '\\((\\d+)\\)', 1) AS BIGINT), 0) AS area_top2_l2_company_cnt,
        COALESCE(CAST(REGEXP_EXTRACT(SPLIT_PART(REPLACE(COALESCE(customer_industry_l2_names_stat, ''), '，', ','), ',', 3), '\\((\\d+)\\)', 1) AS BIGINT), 0) AS area_top3_l2_company_cnt
    FROM fintech_stat.company_address_yuexiu_business_area_stat_ly_v3
),
raw_company AS (
    -- 明细表本身不含 business_area，通过 location_label 去商圈维表反查
    SELECT
        t.*,
        a.business_area
    FROM fintech_stat.company_address_yuexiu_clustered_ly_refresh2 t
    LEFT JOIN area_dim a
        ON t.location_label IS NOT NULL
       AND TRIM(t.location_label) <> ''
       AND FIND_IN_SET(TRIM(t.location_label), REPLACE(COALESCE(a.labels, ''), '，', ',')) > 0
    WHERE t.credit_no IS NOT NULL
      AND TRIM(t.credit_no) <> ''
),
base_company AS (
    -- 基础清洗层：空值按 0 处理，便于后续直接按全体企业求平均
    SELECT
        r.business_area,
        COALESCE(r.is_valid, 0) AS is_valid,
        COALESCE(
            CAST(NULLIF(REGEXP_REPLACE(TRIM(COALESCE(r.annual_report_insured_number, '')), '[^0-9]', ''), '') AS BIGINT),
            0
        ) AS annual_report_insured_number,
        COALESCE(r.abnormal_stay_if_current, 0) AS abnormal_stay_if_current,
        COALESCE(r.law_violated_cnt_current, 0) AS law_violated_cnt_current,
        COALESCE(r.capital_freeze_cnt_current, 0) AS capital_freeze_cnt_current,
        STR_TO_DATE(SUBSTRING(TRIM(COALESCE(r.sale_valid_last_date, '')), 1, 10), '%Y-%m-%d') AS sale_valid_last_dt
    FROM raw_company r
    WHERE r.business_area IS NOT NULL
      AND TRIM(r.business_area) <> ''
),
area_metric AS (
    -- 商圈画像指标
    SELECT
        business_area,
        SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) AS area_valid_count,
        AVG(CASE WHEN is_valid = 1 THEN 1.0 ELSE 0 END) AS area_valid_ratio,
        AVG(CASE WHEN sale_valid_last_dt IS NOT NULL AND DATEDIFF(CURRENT_DATE(), sale_valid_last_dt) > 180 THEN 1.0 ELSE 0 END) AS area_shell_ratio,
        AVG(CASE WHEN abnormal_stay_if_current > 0 OR law_violated_cnt_current > 0 OR capital_freeze_cnt_current > 0 THEN 1.0 ELSE 0 END) AS area_risk_ratio,
        AVG(CASE WHEN annual_report_insured_number >= 5 THEN 1.0 ELSE 0 END) AS area_activity_ratio
    FROM base_company
    GROUP BY business_area
),
final_scored AS (
    SELECT
        d.business_area,
        d.area_company_count,
        d.area_major_l2_name,
        d.area_major_l3_name,
        COALESCE(m.area_valid_count, 0) AS area_valid_count,
        COALESCE(m.area_valid_ratio, 0) AS area_valid_ratio,
        COALESCE(m.area_shell_ratio, 0) AS area_shell_ratio,
        COALESCE(m.area_risk_ratio, 0) AS area_risk_ratio,
        COALESCE(m.area_activity_ratio, 0) AS area_activity_ratio,
        d.area_top1_l2_company_cnt + d.area_top2_l2_company_cnt + d.area_top3_l2_company_cnt AS area_top3_l2_company_sum
    FROM area_dim d
    LEFT JOIN area_metric m
        ON d.business_area = m.business_area
)
SELECT
    f.business_area,
    f.area_company_count,
    f.area_major_l2_name,
    f.area_major_l3_name,
    f.area_valid_count,
    CAST(f.area_valid_ratio AS DECIMAL(10, 4)) AS area_valid_ratio,
    CAST(f.area_shell_ratio AS DECIMAL(10, 4)) AS area_shell_ratio,
    CAST(f.area_risk_ratio AS DECIMAL(10, 4)) AS area_risk_ratio,
    CAST(f.area_activity_ratio AS DECIMAL(10, 4)) AS area_activity_ratio,
    f.area_top3_l2_company_sum,
    CAST(f.area_top3_l2_company_sum * 1.0 / NULLIF(f.area_company_count, 0) AS DECIMAL(10, 4)) AS area_top3_l2_concentration,
    (CASE WHEN f.area_valid_ratio >= 0.85 THEN 1 ELSE 0 END)
    + (CASE WHEN f.area_activity_ratio >= 0.50 THEN 1 ELSE 0 END)
    + (CASE WHEN f.area_company_count >= 10000 THEN 1 ELSE 0 END) AS area_health_score,
    (CASE WHEN f.area_shell_ratio > 0.30 THEN 1 ELSE 0 END)
    + (CASE WHEN f.area_risk_ratio > 0.15 THEN 1 ELSE 0 END)
    + (CASE WHEN f.area_top3_l2_company_sum * 1.0 / NULLIF(f.area_company_count, 0) < 0.80 THEN 1 ELSE 0 END) AS area_risk_score,
    CASE
        WHEN
            (CASE WHEN f.area_shell_ratio > 0.30 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_risk_ratio > 0.15 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_top3_l2_company_sum * 1.0 / NULLIF(f.area_company_count, 0) < 0.80 THEN 1 ELSE 0 END) >= 2
            THEN '红'
        WHEN
            (CASE WHEN f.area_valid_ratio >= 0.85 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_activity_ratio >= 0.50 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_company_count >= 10000 THEN 1 ELSE 0 END) = 3
            AND
            (CASE WHEN f.area_shell_ratio > 0.30 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_risk_ratio > 0.15 THEN 1 ELSE 0 END)
            + (CASE WHEN f.area_top3_l2_company_sum * 1.0 / NULLIF(f.area_company_count, 0) < 0.80 THEN 1 ELSE 0 END) = 0
            THEN '绿'
        ELSE '黄'
    END AS area_grade
FROM final_scored f;
