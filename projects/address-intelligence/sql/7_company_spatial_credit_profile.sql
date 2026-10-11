-- 场景一：从单户企业出发，生成企业空间画像与综合信用评级结果表

-- 先删后建，便于整表重跑
DROP TABLE IF EXISTS fintech_stat.company_spatial_credit_profile_yuexiu_ly;

-- 结果表：一行一家企业，主键粒度为 credit_no
CREATE TABLE fintech_stat.company_spatial_credit_profile_yuexiu_ly (
    credit_no VARCHAR(255) COMMENT '企业统一社会信用代码',
    company_name VARCHAR(255) COMMENT '企业名称',
    company_address VARCHAR(500) COMMENT '企业登记地址',
    business_area VARCHAR(255) COMMENT '所属商圈',
    location_label VARCHAR(255) COMMENT '位置标签',
    entity_name VARCHAR(255) COMMENT '所在楼宇/综合体名',
    road_name VARCHAR(255) COMMENT '道路名',
    road_no VARCHAR(255) COMMENT '门牌号',
    industry_l2_name VARCHAR(255) COMMENT '企业国标大类',
    industry_l3_name VARCHAR(255) COMMENT '企业国标中类',
    is_sme TINYINT COMMENT '是否小微',
    is_valid TINYINT COMMENT '是否有效存续',
    capital_num DECIMAL(20, 2) COMMENT '注册资本（万）',
    annual_report_insured_number BIGINT COMMENT '年报参保人数',
    abnormal_stay_if_current INT COMMENT '当前经营异常条数',
    law_violated_cnt_current INT COMMENT '当前严重违法条数',
    capital_freeze_cnt_current INT COMMENT '当前股权冻结条数',
    area_major_l2_name VARCHAR(1000) COMMENT '所在商圈主导大类Top3',
    is_mainstream_industry VARCHAR(16) COMMENT '企业行业是否命中商圈主导大类Top3',
    spatial_uplift_score INT COMMENT '空间增信分',
    area_shell_ratio DECIMAL(10, 4) COMMENT '所在商圈空壳率',
    area_risk_ratio DECIMAL(10, 4) COMMENT '所在商圈违规密度',
    area_capital_freeze_ratio DECIMAL(10, 4) COMMENT '所在商圈股权冻结密度',
    spatial_warning_score INT COMMENT '空间预警分',
    spatial_credit_signal VARCHAR(16) COMMENT '空间授信信号',
    enterprise_quality_score INT COMMENT '企业自身质量积分',
    enterprise_quality_rating VARCHAR(4) COMMENT '企业质量评级',
    comprehensive_credit_score INT COMMENT '综合信用积分',
    comprehensive_credit_rating VARCHAR(4) COMMENT '综合信用评级'
) ENGINE=OLAP
DUPLICATE KEY (credit_no)
DISTRIBUTED BY HASH (credit_no) BUCKETS 10
PROPERTIES (
    "compression" = "LZ4",
    "fast_schema_evolution" = "true",
    "replicated_storage" = "true",
    "replication_num" = "1"
);

INSERT INTO fintech_stat.company_spatial_credit_profile_yuexiu_ly
WITH area_dim AS (
    -- 商圈维表：保留商圈名称、包含的 location_label 列表，并把主导大类 Top3 拆开
    -- 这里同时承担 label -> 商圈 的映射作用，因为 refresh2 明细表本身不含 business_area
    SELECT
        business_area,
        labels,
        major_l2_name AS area_major_l2_name,
        TRIM(REGEXP_REPLACE(SPLIT_PART(REPLACE(COALESCE(major_l2_name, ''), '，', ','), ',', 1), '\\(.*$', '')) AS area_top1_l2,
        TRIM(REGEXP_REPLACE(SPLIT_PART(REPLACE(COALESCE(major_l2_name, ''), '，', ','), ',', 2), '\\(.*$', '')) AS area_top2_l2,
        TRIM(REGEXP_REPLACE(SPLIT_PART(REPLACE(COALESCE(major_l2_name, ''), '，', ','), ',', 3), '\\(.*$', '')) AS area_top3_l2
    FROM fintech_stat.company_address_yuexiu_business_area_stat_ly_v3
),
raw_company AS (
    -- 上游已经按 credit_no 完成去重，这里直接读取企业明细
    -- business_area 通过 location_label 去商圈维表反查得到
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
    -- 基础清洗层：统一空值、把字符串型人数转数值，并把日期字段转成 date
    SELECT
        r.credit_no,
        r.company_name,
        r.company_address,
        r.business_area,
        r.location_label,
        r.entity_name,
        r.road_name,
        r.road_no,
        r.industry_l2_name,
        r.industry_l3_name,
        COALESCE(r.is_sme, 0) AS is_sme,
        COALESCE(r.is_valid, 0) AS is_valid,
        COALESCE(r.capital_num, 0) AS capital_num,
        COALESCE(
            CAST(NULLIF(REGEXP_REPLACE(TRIM(COALESCE(r.annual_report_insured_number, '')), '[^0-9]', ''), '') AS BIGINT),
            0
        ) AS annual_report_insured_number,
        COALESCE(r.abnormal_stay_if_current, 0) AS abnormal_stay_if_current,
        COALESCE(r.law_violated_cnt_current, 0) AS law_violated_cnt_current,
        COALESCE(r.capital_freeze_cnt_current, 0) AS capital_freeze_cnt_current,
        -- 最近销项开票日期：用于识别空壳和近期经营活跃度
        STR_TO_DATE(SUBSTRING(TRIM(COALESCE(r.sale_valid_last_date, '')), 1, 10), '%Y-%m-%d') AS sale_valid_last_dt,
        -- 成立日期：用于计算企业资历
        STR_TO_DATE(SUBSTRING(TRIM(COALESCE(r.establish_date, '')), 1, 10), '%Y-%m-%d') AS establish_dt
    FROM raw_company r
),
area_metric AS (
    -- 商圈风险指标：空壳率、违规密度、股权冻结密度
    -- 口径：空值按 0 处理，分母计入该商圈全部企业
    SELECT
        business_area,
        AVG(CASE WHEN sale_valid_last_dt IS NOT NULL AND DATEDIFF(CURRENT_DATE(), sale_valid_last_dt) > 180 THEN 1.0 ELSE 0 END) AS area_shell_ratio,
        AVG(CASE WHEN law_violated_cnt_current > 0 OR abnormal_stay_if_current > 0 THEN 1.0 ELSE 0 END) AS area_risk_ratio,
        AVG(CASE WHEN capital_freeze_cnt_current > 0 THEN 1.0 ELSE 0 END) AS area_capital_freeze_ratio
    FROM base_company
    GROUP BY business_area
),
joined AS (
    -- 把企业基础信息、商圈主导行业、商圈风险合并到同一层
    SELECT
        b.credit_no,
        b.company_name,
        b.company_address,
        b.business_area,
        b.location_label,
        b.entity_name,
        b.road_name,
        b.road_no,
        b.industry_l2_name,
        b.industry_l3_name,
        b.is_sme,
        b.is_valid,
        b.capital_num,
        b.annual_report_insured_number,
        b.abnormal_stay_if_current,
        b.law_violated_cnt_current,
        b.capital_freeze_cnt_current,
        b.sale_valid_last_dt,
        b.establish_dt,
        d.area_major_l2_name,
        d.area_top1_l2,
        d.area_top2_l2,
        d.area_top3_l2,
        COALESCE(a.area_shell_ratio, 0) AS area_shell_ratio,
        COALESCE(a.area_risk_ratio, 0) AS area_risk_ratio,
        COALESCE(a.area_capital_freeze_ratio, 0) AS area_capital_freeze_ratio
    FROM base_company b
    LEFT JOIN area_dim d
        ON b.business_area = d.business_area
    LEFT JOIN area_metric a
        ON b.business_area = a.business_area
),
signal_scored AS (
    -- 评分层 1：
    -- 1) 判断企业所属大类是否命中商圈主导 Top3，形成空间增信分
    -- 2) 依据商圈风险、商圈空壳率形成空间预警分
    -- 3) 依据参保、资本、近期开票、成立年限形成企业自身质量分
    SELECT
        j.*,
        CASE
            WHEN j.industry_l2_name = j.area_top1_l2 THEN 'top1'
            WHEN j.industry_l2_name = j.area_top2_l2 THEN 'top2'
            WHEN j.industry_l2_name = j.area_top3_l2 THEN 'top3'
            ELSE 'other'
        END AS is_mainstream_industry,
        CASE
            WHEN j.industry_l2_name = j.area_top1_l2 THEN 3
            WHEN j.industry_l2_name = j.area_top2_l2 THEN 2
            WHEN j.industry_l2_name = j.area_top3_l2 THEN 1
            ELSE 0
        END AS spatial_uplift_score,
        (CASE WHEN j.area_risk_ratio > 0.15 THEN 1 ELSE 0 END)
        + (CASE WHEN j.area_shell_ratio > 0.30 THEN 1 ELSE 0 END) AS spatial_warning_score,
        (CASE WHEN j.annual_report_insured_number >= 10 THEN 1 ELSE 0 END)
        + (CASE WHEN j.capital_num >= 500 THEN 1 ELSE 0 END)
        + (CASE WHEN j.sale_valid_last_dt IS NOT NULL AND DATEDIFF(CURRENT_DATE(), j.sale_valid_last_dt) <= 90 THEN 1 ELSE 0 END)
        + (CASE WHEN j.establish_dt IS NOT NULL AND DATEDIFF(CURRENT_DATE(), j.establish_dt) >= 1095 THEN 1 ELSE 0 END) AS enterprise_quality_score
    FROM joined j
),
final_scored AS (
    -- 评分层 2：
    -- 1) 根据增信分和预警分生成空间授信信号
    -- 2) 企业自身有异常/违法/冻结/非有效存续时一票否决为 D
    -- 3) 综合信用分 = 企业质量分 * 2 + 空间增信分 - 空间预警分
    SELECT
        s.*,
        CASE
            WHEN s.spatial_warning_score >= 2 THEN '下调'
            WHEN s.spatial_warning_score = 1 AND s.spatial_uplift_score <= 1 THEN '关注'
            WHEN s.spatial_uplift_score >= 2 AND s.spatial_warning_score = 0 THEN '上浮'
            ELSE '持平'
        END AS spatial_credit_signal,
        CASE
            WHEN s.is_valid <> 1
              OR s.abnormal_stay_if_current > 0
              OR s.law_violated_cnt_current > 0
              OR s.capital_freeze_cnt_current > 0 THEN 'D'
            WHEN s.enterprise_quality_score = 4 THEN 'A'
            WHEN s.enterprise_quality_score = 3 THEN 'B'
            WHEN s.enterprise_quality_score >= 1 THEN 'C'
            ELSE 'D'
        END AS enterprise_quality_rating,
        s.enterprise_quality_score * 2 + s.spatial_uplift_score - s.spatial_warning_score AS comprehensive_credit_score
    FROM signal_scored s
)
-- 最终输出层：补齐综合评级
SELECT
    f.credit_no,
    f.company_name,
    f.company_address,
    f.business_area,
    f.location_label,
    f.entity_name,
    f.road_name,
    f.road_no,
    f.industry_l2_name,
    f.industry_l3_name,
    f.is_sme,
    f.is_valid,
    f.capital_num,
    f.annual_report_insured_number,
    f.abnormal_stay_if_current,
    f.law_violated_cnt_current,
    f.capital_freeze_cnt_current,
    f.area_major_l2_name,
    f.is_mainstream_industry,
    f.spatial_uplift_score,
    CAST(f.area_shell_ratio AS DECIMAL(10, 4)) AS area_shell_ratio,
    CAST(f.area_risk_ratio AS DECIMAL(10, 4)) AS area_risk_ratio,
    CAST(f.area_capital_freeze_ratio AS DECIMAL(10, 4)) AS area_capital_freeze_ratio,
    f.spatial_warning_score,
    f.spatial_credit_signal,
    f.enterprise_quality_score,
    f.enterprise_quality_rating,
    f.comprehensive_credit_score,
    CASE
        -- 综合评级同样保留一票否决逻辑
        WHEN f.is_valid <> 1
          OR f.abnormal_stay_if_current > 0
          OR f.law_violated_cnt_current > 0
          OR f.capital_freeze_cnt_current > 0 THEN 'D'
        WHEN f.comprehensive_credit_score >= 9 THEN 'A'
        WHEN f.comprehensive_credit_score >= 6 THEN 'B'
        WHEN f.comprehensive_credit_score >= 3 THEN 'C'
        ELSE 'D'
    END AS comprehensive_credit_rating
FROM final_scored f;
