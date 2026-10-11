-- 场景四：从行业中类出发，生成行业集群画像与等级识别结果表
-- 目标：
-- 1. 以「商圈 + 国标行业中类」为粒度，识别每个商圈内是否形成行业集群。
-- 2. 输出集群规模、有效企业、小微企业、资本规模、龙头/初创/配套企业结构。
-- 3. 计算该集群在全区同行业中的占比，以及在本商圈企业中的占比。
-- 4. 根据企业数量、商圈内占比、全区同行业占比，划分为「核心 / 次要 / 潜在 / 非集群」。


-- 重建结果表，保证每次执行都是全量刷新
DROP TABLE IF EXISTS fintech_stat.industry_cluster_profile_yuexiu;

-- 行业集群画像结果表：
-- 一行代表某个商圈内的一个国标行业中类集群。
-- 主键粒度为 business_area + industry_l3_name。
CREATE TABLE fintech_stat.industry_cluster_profile_yuexiu (
    business_area VARCHAR(255) COMMENT '商圈',
    industry_l3_name VARCHAR(255) COMMENT '国标中类',
    industry_l2_name VARCHAR(255) COMMENT '国标大类',
    cluster_company_count BIGINT COMMENT '该行业在该商圈的企业数',
    cluster_valid_count BIGINT COMMENT '有效存续企业数',
    cluster_sme_count BIGINT COMMENT '集群内小微企业数',
    cluster_total_capital DECIMAL(20, 2) COMMENT '注册资本合计',
    cluster_avg_capital DECIMAL(20, 2) COMMENT '平均注册资本',
    cluster_leader_count BIGINT COMMENT '龙头企业数',
    cluster_startup_count BIGINT COMMENT '初创企业数',
    cluster_supporter_count BIGINT COMMENT '配套企业数',
    industry_total_all_areas BIGINT COMMENT '全区同行业企业总数',
    area_total_companies BIGINT COMMENT '该商圈企业总数',
    cluster_share_in_industry DECIMAL(10, 4) COMMENT '该集群占全区同行业比重',
    cluster_share_in_area DECIMAL(10, 4) COMMENT '该集群占该商圈企业比重',
    cluster_strength_score INT COMMENT '集群强度分',
    cluster_tier VARCHAR(16) COMMENT '集群等级'
) ENGINE=OLAP
DUPLICATE KEY (business_area, industry_l3_name)
DISTRIBUTED BY HASH (business_area, industry_l3_name) BUCKETS 10
PROPERTIES (
    "compression" = "LZ4",
    "fast_schema_evolution" = "true",
    "replicated_storage" = "true",
    "replication_num" = "1"
);

-- 写入行业集群画像结果
INSERT INTO fintech_stat.industry_cluster_profile_yuexiu
WITH area_dim AS (
    -- 商圈维表：
    -- 企业明细表只有 location_label，没有直接落 business_area。
    -- 这里读取商圈包含的 labels，用于后续把 location_label 映射回 business_area。
    SELECT
        business_area,
        labels
    FROM fintech_stat.company_address_yuexiu_business_area_stat_ly_v3
),
raw_company AS (
    -- 企业明细层：
    -- 通过 FIND_IN_SET 判断企业 location_label 是否落在某个商圈的 labels 里。
    -- 只保留有统一社会信用代码的企业，避免无主体数据进入行业集群统计。
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
    -- 1. 仅保留已匹配到商圈且行业中类非空的企业。
    -- 2. 清洗行业大类/中类、小微标识、存续标识、注册资本、参保人数、成立日期。
    -- 3. 参保人数可能是字符串，这里去掉非数字字符后转 BIGINT。
    SELECT
        r.business_area,
        NULLIF(TRIM(r.industry_l2_name), '') AS industry_l2_name,
        NULLIF(TRIM(r.industry_l3_name), '') AS industry_l3_name,
        COALESCE(r.is_valid, 0) AS is_valid,
        COALESCE(r.is_sme, 0) AS is_sme,
        COALESCE(r.capital_num, 0) AS capital_num,
        COALESCE(
            CAST(NULLIF(REGEXP_REPLACE(TRIM(COALESCE(r.annual_report_insured_number, '')), '[^0-9]', ''), '') AS BIGINT),
            0
        ) AS annual_report_insured_number,
        STR_TO_DATE(SUBSTRING(TRIM(COALESCE(r.establish_date, '')), 1, 10), '%Y-%m-%d') AS establish_dt
    FROM raw_company r
    WHERE r.business_area IS NOT NULL
      AND TRIM(r.business_area) <> ''
      AND NULLIF(TRIM(r.industry_l3_name), '') IS NOT NULL
),
cluster_metric AS (
    -- 行业集群画像指标：
    -- 统计口径为「商圈 + 行业中类」。
    -- cluster_company_count：该商圈内该行业中类的企业数。
    -- cluster_valid_count：其中有效存续企业数。
    -- cluster_sme_count：其中小微企业数。
    -- cluster_total_capital / cluster_avg_capital：资本规模与平均资本。
    -- industry_total_all_areas：全区同一行业中类的企业总数，用于计算全区同行业占比。
    -- area_total_companies：该商圈内有行业中类的企业总数，用于计算商圈内占比。
    SELECT
        business_area,
        industry_l3_name,
        MAX(industry_l2_name) AS industry_l2_name,
        COUNT(*) AS cluster_company_count,
        SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) AS cluster_valid_count,
        SUM(CASE WHEN is_sme = 1 THEN 1 ELSE 0 END) AS cluster_sme_count,
        SUM(capital_num) AS cluster_total_capital,
        AVG(capital_num) AS cluster_avg_capital,
        -- 龙头企业：
        -- 注册资本 >= 5000 万、年报参保人数 >= 50、成立满 10 年。
        -- 这里的 3650 天近似表示 10 年。
        SUM(
            CASE
                WHEN capital_num >= 1000
                 AND annual_report_insured_number >= 10
                 AND establish_dt IS NOT NULL
                 AND DATEDIFF(CURRENT_DATE(), establish_dt) >= 3650
                    THEN 1
                ELSE 0
            END
        ) AS cluster_leader_count,
        -- 初创企业：
        -- 成立时间在 3 年以内。这里的 1095 天近似表示 3 年。
        SUM(
            CASE
                WHEN establish_dt IS NOT NULL
                 AND DATEDIFF(CURRENT_DATE(), establish_dt) <= 1095
                    THEN 1
                ELSE 0
            END
        ) AS cluster_startup_count,
        -- 配套企业：
        -- 有效存续、不是龙头企业、且成立超过 3 年。
        -- 该指标用于观察集群内是否存在稳定的中腰部企业支撑。
        SUM(
            CASE
                WHEN is_valid = 1
                 AND NOT (
                     capital_num >= 5000
                     AND annual_report_insured_number >= 50
                     AND establish_dt IS NOT NULL
                     AND DATEDIFF(CURRENT_DATE(), establish_dt) >= 3650
                 )
                 AND establish_dt IS NOT NULL
                 AND DATEDIFF(CURRENT_DATE(), establish_dt) > 1095
                    THEN 1
                ELSE 0
            END
        ) AS cluster_supporter_count,
        SUM(COUNT(*)) OVER (PARTITION BY industry_l3_name) AS industry_total_all_areas,
        SUM(COUNT(*)) OVER (PARTITION BY business_area) AS area_total_companies
    FROM base_company
    GROUP BY business_area, industry_l3_name
)
SELECT
    -- 基础维度
    c.business_area,
    c.industry_l3_name,
    c.industry_l2_name,
    -- 集群规模与主体结构
    c.cluster_company_count,
    c.cluster_valid_count,
    c.cluster_sme_count,
    -- 集群资本规模
    CAST(c.cluster_total_capital AS DECIMAL(20, 2)) AS cluster_total_capital,
    CAST(c.cluster_avg_capital AS DECIMAL(20, 2)) AS cluster_avg_capital,
    -- 集群企业类型结构
    c.cluster_leader_count,
    c.cluster_startup_count,
    c.cluster_supporter_count,
    -- 分母口径：
    -- industry_total_all_areas 是全区同行业企业数；
    -- area_total_companies 是该商圈企业总数。
    c.industry_total_all_areas,
    c.area_total_companies,
    -- 该集群占全区同行业的比重：
    -- 反映该商圈在该行业中类上的全区集中度。
    CAST(c.cluster_company_count * 1.0 / NULLIF(c.industry_total_all_areas, 0) AS DECIMAL(10, 4)) AS cluster_share_in_industry,
    -- 该集群占该商圈企业的比重：
    -- 反映该行业中类是否是该商圈内部的主导产业。
    CAST(c.cluster_company_count * 1.0 / NULLIF(c.area_total_companies, 0) AS DECIMAL(10, 4)) AS cluster_share_in_area,
    -- 集群强度分：
    -- 企业数 >= 3000 记 1 分；
    -- 占该商圈企业比重 >= 40% 记 1 分；
    -- 占全区同行业比重 >= 40% 记 1 分。
    (CASE WHEN c.cluster_company_count >= 3000 THEN 1 ELSE 0 END)
    + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.area_total_companies, 0) >= 0.40 THEN 1 ELSE 0 END)
    + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.industry_total_all_areas, 0) >= 0.40 THEN 1 ELSE 0 END) AS cluster_strength_score,
    -- 集群等级：
    -- 核心：强度分 = 3，规模、商圈内主导性、全区集中度都达标。
    -- 次要：强度分 = 2，已具备较明显集群特征。
    -- 潜在：强度分 = 1，存在一定集聚迹象。
    -- 非集群：强度分 = 0。
    CASE
        WHEN
            (CASE WHEN c.cluster_company_count >= 3000 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.area_total_companies, 0) >= 0.40 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.industry_total_all_areas, 0) >= 0.40 THEN 1 ELSE 0 END) = 3
            THEN '核心'
        WHEN
            (CASE WHEN c.cluster_company_count >= 3000 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.area_total_companies, 0) >= 0.40 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.industry_total_all_areas, 0) >= 0.40 THEN 1 ELSE 0 END) = 2
            THEN '次要'
        WHEN
            (CASE WHEN c.cluster_company_count >= 3000 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.area_total_companies, 0) >= 0.40 THEN 1 ELSE 0 END)
            + (CASE WHEN c.cluster_company_count * 1.0 / NULLIF(c.industry_total_all_areas, 0) >= 0.40 THEN 1 ELSE 0 END) = 1
            THEN '潜在'
        ELSE '非集群'
    END AS cluster_tier
FROM cluster_metric c;
