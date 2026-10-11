-- 场景三：从楼宇出发，生成楼宇画像与扫楼优先级结果表
-- 目标：
-- 1. 把企业明细表按「商圈 + 楼宇」聚合成楼宇画像。
-- 2. 输出楼内企业规模、存续情况、行业结构、经营活性、风险密度等指标。
-- 3. 根据健康度与风险度积分，把楼宇划分为「优 / 中 / 差」，辅助客户经理安排扫楼优先级。

-- 重建结果表，保证每次脚本执行都是全量刷新
DROP TABLE IF EXISTS fintech_stat.building_credit_profile_yuexiu_ly;

-- 楼宇画像结果表：一行代表一个商圈内的一栋楼宇
-- 注意：同名楼宇可能出现在不同商圈，因此主键粒度使用 business_area + entity_name
CREATE TABLE fintech_stat.building_credit_profile_yuexiu_ly (
    business_area VARCHAR(255) COMMENT '所属商圈',
    entity_name VARCHAR(255) COMMENT '楼宇名',
    bldg_company_count BIGINT COMMENT '楼内企业总数',
    bldg_valid_count BIGINT COMMENT '有效存续企业数',
    bldg_valid_ratio DECIMAL(10, 4) COMMENT '有效存续占比',
    bldg_total_insured BIGINT COMMENT '楼内年报参保总人数',
    bldg_avg_capital DECIMAL(20, 2) COMMENT '楼内平均注册资本',
    bldg_top5_l3 VARCHAR(2000) COMMENT '楼内主导中类Top5',
    bldg_shell_ratio DECIMAL(10, 4) COMMENT '楼内空壳率',
    bldg_risk_ratio DECIMAL(10, 4) COMMENT '楼内违规/异常/冻结密度',
    bldg_activity_ratio DECIMAL(10, 4) COMMENT '楼内经营活性占比',
    bldg_high_value_count BIGINT COMMENT '楼内高价值户数',
    bldg_health_score INT COMMENT '楼宇健康度积分',
    bldg_risk_score INT COMMENT '楼宇风险度积分',
    bldg_grade VARCHAR(4) COMMENT '楼宇评级'
) ENGINE=OLAP
DUPLICATE KEY (business_area, entity_name)
DISTRIBUTED BY HASH (business_area, entity_name) BUCKETS 10
PROPERTIES (
    "compression" = "LZ4",
    "fast_schema_evolution" = "true",
    "replicated_storage" = "true",
    "replication_num" = "1"
);

-- 写入楼宇画像结果
INSERT INTO fintech_stat.building_credit_profile_yuexiu_ly
WITH area_dim AS (
    -- 商圈维表：
    -- 明细企业表只有 location_label，没有直接落 business_area。
    -- 这里读取商圈包含的 labels，用于后续把 location_label 映射回 business_area。
    SELECT
        business_area,
        labels
    FROM fintech_stat.company_address_yuexiu_business_area_stat_ly_v3
),
raw_company AS (
    -- 企业明细层：
    -- 通过 FIND_IN_SET 判断企业 location_label 是否落在某个商圈的 labels 里。
    -- 只保留有统一社会信用代码的企业，避免无主体数据进入楼宇统计。
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
    -- 基础清洗层：
    -- 1. 仅保留已匹配到商圈且 entity_name 非空的企业，作为楼宇画像的统计基础。
    -- 2. 清洗行业中类、注册资本、参保人数、风险字段和最近销项开票日期。
    -- 3. 参保人数可能是字符串，这里去掉非数字字符后转 BIGINT。
    SELECT
        r.business_area,
        NULLIF(TRIM(r.entity_name), '') AS entity_name,
        NULLIF(TRIM(r.industry_l3_name), '') AS industry_l3_name,
        COALESCE(r.is_valid, 0) AS is_valid,
        COALESCE(r.capital_num, 0) AS capital_num,
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
      AND NULLIF(TRIM(r.entity_name), '') IS NOT NULL
),
building_l3_ranked AS (
    -- 楼宇行业中类排序：
    -- 先统计每栋楼内各行业中类的企业数，再按企业数降序排名。
    -- rn = 1 表示该楼宇企业数最多的行业中类。
    SELECT
        business_area,
        entity_name,
        industry_l3_name,
        company_cnt,
        ROW_NUMBER() OVER (
            PARTITION BY business_area, entity_name
            ORDER BY company_cnt DESC, industry_l3_name
        ) AS rn
    FROM (
        SELECT
            business_area,
            entity_name,
            industry_l3_name,
            COUNT(*) AS company_cnt
        FROM base_company
        WHERE industry_l3_name IS NOT NULL
        GROUP BY business_area, entity_name, industry_l3_name
    ) x
),
building_l3_top5 AS (
    -- 楼宇主导行业 Top5：
    -- 把排序后的行业中类拼成「行业名(企业数)」格式。
    -- 固定按 Top1 ~ Top5 拼接，保证输出顺序稳定，便于前端展示或人工查看。
    SELECT
        business_area,
        entity_name,
        CONCAT_WS(', ',
            MAX(CASE WHEN rn = 1 THEN CONCAT(industry_l3_name, '(', company_cnt, ')') END),
            MAX(CASE WHEN rn = 2 THEN CONCAT(industry_l3_name, '(', company_cnt, ')') END),
            MAX(CASE WHEN rn = 3 THEN CONCAT(industry_l3_name, '(', company_cnt, ')') END),
            MAX(CASE WHEN rn = 4 THEN CONCAT(industry_l3_name, '(', company_cnt, ')') END),
            MAX(CASE WHEN rn = 5 THEN CONCAT(industry_l3_name, '(', company_cnt, ')') END)
        ) AS bldg_top5_l3
    FROM building_l3_ranked
    WHERE rn <= 5
    GROUP BY business_area, entity_name
),
building_metric AS (
    -- 楼宇核心指标聚合：
    -- 统计口径为「商圈 + 楼宇」，分母均为该楼宇内全部企业。
    -- bldg_shell_ratio：最近销项开票日期超过 180 天的企业占比。
    -- bldg_risk_ratio：当前存在经营异常、严重违法或股权冻结的企业占比。
    -- bldg_activity_ratio：年报参保人数 >= 10 的企业占比，用于近似衡量经营活性。
    -- bldg_high_value_count：有效存续、资本较高、参保人数较高、近期有开票且无风险记录的企业数。
    SELECT
        business_area,
        entity_name,
        COUNT(*) AS bldg_company_count,
        SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) AS bldg_valid_count,
        AVG(CASE WHEN is_valid = 1 THEN 1.0 ELSE 0 END) AS bldg_valid_ratio,
        SUM(annual_report_insured_number) AS bldg_total_insured,
        AVG(capital_num) AS bldg_avg_capital,
        AVG(CASE WHEN sale_valid_last_dt IS NOT NULL AND DATEDIFF(CURRENT_DATE(), sale_valid_last_dt) > 180 THEN 1.0 ELSE 0 END) AS bldg_shell_ratio,
        AVG(CASE WHEN abnormal_stay_if_current > 0 OR law_violated_cnt_current > 0 OR capital_freeze_cnt_current > 0 THEN 1.0 ELSE 0 END) AS bldg_risk_ratio,
        AVG(CASE WHEN annual_report_insured_number >= 10 THEN 1.0 ELSE 0 END) AS bldg_activity_ratio,
        SUM(
            CASE
                WHEN is_valid = 1
                 AND capital_num > 1000
                 AND annual_report_insured_number >= 100
                 AND sale_valid_last_dt IS NOT NULL
                 AND DATEDIFF(CURRENT_DATE(), sale_valid_last_dt) <= 90
                 AND abnormal_stay_if_current = 0
                 AND law_violated_cnt_current = 0
                 AND capital_freeze_cnt_current = 0
                    THEN 1
                ELSE 0
            END
        ) AS bldg_high_value_count
    FROM base_company
    GROUP BY business_area, entity_name
),
final_scored AS (
    SELECT
        m.business_area,
        m.entity_name,
        m.bldg_company_count,
        m.bldg_valid_count,
        m.bldg_valid_ratio,
        m.bldg_total_insured,
        m.bldg_avg_capital,
        COALESCE(t.bldg_top5_l3, '') AS bldg_top5_l3,
        m.bldg_shell_ratio,
        m.bldg_risk_ratio,
        m.bldg_activity_ratio,
        m.bldg_high_value_count,
        -- 健康度积分：
        -- 有效存续占比 >= 85% 记 1 分；
        -- 经营活性占比 >= 30% 记 1 分；
        -- 楼内企业数 >= 50 记 1 分。
        (CASE WHEN m.bldg_valid_ratio >= 0.85 THEN 1 ELSE 0 END)
        + (CASE WHEN m.bldg_activity_ratio >= 0.30 THEN 1 ELSE 0 END)
        + (CASE WHEN m.bldg_company_count >= 50 THEN 1 ELSE 0 END) AS bldg_health_score,
        -- 风险度积分：
        -- 空壳率 > 30% 记 1 分；
        -- 风险密度 > 15% 记 1 分；
        -- 楼内企业数 < 10 记 1 分，表示样本规模偏小、扫楼价值较低或稳定性不足。
        (CASE WHEN m.bldg_shell_ratio > 0.30 THEN 1 ELSE 0 END)
        + (CASE WHEN m.bldg_risk_ratio > 0.15 THEN 1 ELSE 0 END)
        + (CASE WHEN m.bldg_company_count < 10 THEN 1 ELSE 0 END) AS bldg_risk_score,
        -- 高价值户信号：
        -- 高价值户占比 >= 5% 时，认为楼宇存在优质锚点户。
        CASE
            WHEN m.bldg_high_value_count * 1.0 / NULLIF(m.bldg_company_count, 0) >= 0.05
                THEN 1
            ELSE 0
        END AS bldg_high_value_signal
    FROM building_metric m
    -- 补充楼宇行业 Top5，部分楼宇没有行业中类时输出空字符串
    LEFT JOIN building_l3_top5 t
        ON m.business_area = t.business_area
       AND m.entity_name = t.entity_name
)
SELECT
    -- 基础维度
    f.business_area,
    f.entity_name,
    -- 楼宇规模与主体质量
    f.bldg_company_count,
    f.bldg_valid_count,
    CAST(f.bldg_valid_ratio AS DECIMAL(10, 4)) AS bldg_valid_ratio,
    -- 楼宇经营实力
    f.bldg_total_insured,
    CAST(f.bldg_avg_capital AS DECIMAL(20, 2)) AS bldg_avg_capital,
    -- 楼宇主导行业结构
    f.bldg_top5_l3,
    -- 楼宇风险与活性
    CAST(f.bldg_shell_ratio AS DECIMAL(10, 4)) AS bldg_shell_ratio,
    CAST(f.bldg_risk_ratio AS DECIMAL(10, 4)) AS bldg_risk_ratio,
    CAST(f.bldg_activity_ratio AS DECIMAL(10, 4)) AS bldg_activity_ratio,
    f.bldg_high_value_count,
    f.bldg_health_score,
    f.bldg_risk_score,
    -- 楼宇评级：
    -- 差：风险度积分 >= 2。
    -- 优：风险度为 0，且健康度满分，或健康度 2 分但存在高价值户信号。
    -- 中：其他情况。
    CASE
        WHEN f.bldg_risk_score >= 2
            THEN '差'
        WHEN
            f.bldg_risk_score = 0
            AND (
                f.bldg_health_score = 3
                OR (f.bldg_health_score = 2 AND f.bldg_high_value_signal = 1)
            )
            THEN '优'
        ELSE '中'
    END AS bldg_grade
FROM final_scored f;
