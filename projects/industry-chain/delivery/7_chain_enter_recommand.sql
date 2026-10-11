-- 要更改的部分
-- 输入：industry_chain_cls_AM_ly；AM_chain_final_unique_enterprise_2024_ly；tmp_raw_trade_month_AM_2024_ly；
-- chain_name = '增材制造产业链'    
-- 输出：AM_chain_enterprise_recommend_2401_2412_ly

CREATE TABLE `AM_chain_enterprise_recommend_2401_2412_ly` (
	`chain_name` varchar(100) NOT NULL COMMENT "",
  `from_tax_id` varchar(100) NOT NULL COMMENT "",
  `to_tax_id` varchar(100) NOT NULL COMMENT "",
  `invoice_type` varchar(50) NOT NULL COMMENT "",
  `company_name` varchar(65533) NOT NULL COMMENT "",
  `trade_type` varchar(50) NOT NULL COMMENT "",
  `recommend_company_name` varchar(65533) NOT NULL COMMENT "",
  `goods_code` varchar(65533) NOT NULL COMMENT "",
  `goods_code_num` int(11) NULL COMMENT "",
  `total_amount` decimal(20, 2) NULL COMMENT "",
  `invoice_num` int(11) NULL COMMENT "",
  `trade_score` decimal(20, 2) NULL COMMENT "",
  `trade_rate` varchar(50) NOT NULL COMMENT ""
) ENGINE=OLAP 
UNIQUE KEY(`chain_name`, `from_tax_id`, `to_tax_id`, `invoice_type`, `company_name`, `trade_type`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64 
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"enable_persistent_index" = "false",
"replicated_storage" = "true",
"compression" = "LZ4"
);

-- ======================================
-- 步骤-1：提取产业链的原始map_code（不做任何长度处理）
-- 核心：1. 从industry_chain_cls中获取机器人的原始map_code（varchar(50)）
--       2. 去重，避免重复匹配
-- ======================================
DROP TABLE IF EXISTS temp_chain_mapcode;
CREATE TABLE temp_chain_mapcode (
  `map_code` varchar(50) NOT NULL -- 存储原始map_code，类型与原表一致
) ENGINE=OLAP 
UNIQUE KEY(`map_code`)
DISTRIBUTED BY HASH(`map_code`) BUCKETS 1 -- 数据量小，分桶为1
PROPERTIES (
"replication_num" = "3",
"in_memory" = "true",
"compression" = "LZ4"
);

-- 插入机器人产业链的原始map_code（去重）
INSERT OVERWRITE temp_chain_mapcode
SELECT DISTINCT map_code
FROM industry_chain_cls_AM_ly
WHERE chain_name = '增材制造产业链' -- 注意与表中实际值一致（如无空格、别名等）
  AND map_code IS NOT NULL; -- 过滤NULL值，避免后续匹配异常

-- ======================================
-- 步骤0：创建临时表存储目标企业名称与tax_id的映射（核心：筛选目标企业）
-- ======================================
DROP TABLE IF EXISTS temp_target_enterprise;
CREATE TABLE temp_target_enterprise (
  `tax_id` varchar(100) NOT NULL,
  `company_name` varchar(200) NOT NULL
) ENGINE=OLAP 
UNIQUE KEY(`tax_id`)
DISTRIBUTED BY HASH(`tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "true",
"compression" = "LZ4"
);

-- 插入提供的企业名称对应的tax_id
INSERT OVERWRITE temp_target_enterprise
SELECT
  t.tax_id,
  t.company_name
FROM AM_chain_final_unique_enterprise_2024_ly t
WHERE t.company_name IN (
'南京凝聚力文化发展有限公司',
  '深圳市弘华鑫科技有限公司',
  '河南乐融融汽车服务有限公司',
  '深圳市厚德汇通信息咨询有限公司',
  '无锡易恒田企业信息咨询有限公司',
  '鸡西市鸡冠区韬莹服饰店',
  '盐城市城南新区吕二餐厅',
  '霸州市炜拓商贸中心',
  '广州华杰一加一科技有限公司',
  '深圳市艺启绘吧文化传播有限公司',
  '潍坊鹏鹍置业有限公司',
  '长汀县天凯汽车拖车服务部',
  '广东国大技术发展有限公司',
  '龙门县南昆山新南联通讯店',
  '沙市区孜远电脑经营部',
  '茂名市电白区康源食品厂',
  '广州市有元投资发展有限公司',
  '广州摩天轮文化传媒有限公司',
  '东莞市聚诚汽车消费咨询服务有限公司',
  '东莞市杉合电子有限公司',
  '涟水麦克童星艺术培训有限公司',
  '无锡汇聚美生物科技有限公司',
  '万源市天莉手机销售有限公司',
  '钟祥市旭浩劳务服务有限公司',
  '永定区恒睿电脑店',
  '昌吉市正邦财务咨询有限公司',
  '文水县灏天贸易有限公司',
  '东莞市鑫雅科技有限公司',
  '太湖县房管家管理咨询服务有限公司',
  '溧阳市一诚劳务有限公司',
  '成都濠新文化传播有限公司',
  '杭州人为本医药科技有限公司',
  '化州市丽岗镇真诚相待手机维修店',
  '池州市顺赢智慧信息技术有限公司',
  '深圳市力谨科技有限公司',
  '云南蓝显科技有限公司',
  '苏州虹迪自动化科技有限公司',
  '大厂回族自治县城区辰潞信息咨询中心',
  '龙山县金利宾馆',
  '安徽互达机电工程有限公司',
  '四川新开拓科技有限公司',
  '安徽维斯顿智能科技有限公司',
  '厦门博泽电子科技有限公司',
  '泰州市宏诚网络科技有限公司',
  '河南中裕检测技术有限公司',
  '东莞市三尼电子有限公司',
  '巴彦淖尔市启尚汽车销售服务有限公司',
  '惠州市远城物流有限公司',
  '禹州市宏健医疗器械有限公司',
  '西安志汇新诚电子有限责任公司',
  '张家港市龙盛电子有限公司',
  '北京芭九企业管理科技有限公司',
  '遂宁市鑫川机械设备有限公司',
  '惠州市诺云精密仪器有限公司',
  '新师国际教育科技集团有限公司',
  '惠州市金磊新能源科技有限公司',
  '深圳市陈龙舞美文化传播有限公司',
  '深圳市腾云智能科技有限公司',
  '贵州庆和佳商贸有限公司',
  '东莞金世达汽车有限公司',
  '东莞市顺业信息技术有限公司',
  '武汉智航文化传播有限公司',
  '海口大年工贸有限公司',
  '云浮市顺迪汽车销售有限公司',
  '仪征桦宇商业管理有限公司',
  '咸阳晟泽实业有限公司',
  '北京天利时代科技有限公司',
  '杭州哇洗智能科技有限公司',
  '深圳华凯瑞科技有限公司',
  '深圳市深信泰富供应链有限公司',
  '安乡四通机动车检测有限公司',
  '六安市国泰通信器材有限责任公司',
  '湖南燃动盒子运动健身有限公司',
  '苏州高擎三炬精密科技有限公司',
  '大连东鼎工业设备有限公司',
  '南通嘉浩防腐工程有限公司',
  '福州嘉德宏网络科技有限公司',
  '宜兴市事兴环保科技有限公司',
  '西宁正鼎企业管理有限公司',
  '靖江市新启元自动化科技有限公司',
  '青岛森江冶金新材料有限公司',
  '蒙自智能科技服务有限责任公司',
  '金华市雅澳供应链有限公司',
  '延安真岚轩科技有限公司',
  '济南泽升文化发展有限公司',
  '河南启硕信息科技有限公司',
  '扬州亚锋电器有限公司',
  '上海倔金企业登记代理有限公司',
  '西藏博宇金属股份有限公司',
  '武汉思蒂兰门窗科技有限公司',
  '完美互联（北京）信息技术有限公司',
  '博罗县诺成科技有限公司',
  '临海市星元灯饰厂',
  '北京数泰科技有限公司',
  '巩义市竹林宏业耐材有限公司',
  '深圳市芯科诚成电子有限公司',
  '山东地牛信息技术有限公司',
  '白碱滩区三生有幸商行',
  '晋城一家一装饰工程有限公司',
  '青岛西铁电子科技有限公司'
);

-- ======================================
-- 步骤1：创建临时表存储筛选后的交易数据（核心：只保留目标企业+机器人产业链匹配数据）
-- 关联条件为goods_code = map_code（完全等值匹配，不处理长度）
-- ======================================
DROP TABLE IF EXISTS temp_filtered_trade;
CREATE TABLE temp_filtered_trade (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `year_mon` varchar(20) NOT NULL,
  `goods_code` varchar(7) NOT NULL,
  `total_amount` decimal(20, 2) NULL,
  `invoice_num` int(50) NULL
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`, `year_mon`, `goods_code`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

-- 插入筛选后的数据：机器人产业链map_code等值匹配 + 91/92开头过滤 + 目标企业关联
INSERT OVERWRITE temp_filtered_trade
SELECT
  t.from_tax_id,
  t.to_tax_id,
  t.invoice_type,
  t.year_mon,
  t.goods_code,
  t.total_amount,
  t.invoice_num
FROM tmp_raw_trade_month_AM_2024_ly t
-- 关联目标企业
JOIN temp_target_enterprise te
  ON t.from_tax_id = te.tax_id OR t.to_tax_id = te.tax_id
-- 核心：直接用goods_code与机器人产业链的原始map_code做等值匹配
JOIN temp_chain_mapcode rc
  ON t.goods_code = rc.map_code
-- 原有过滤：from/to tax_id以91/92开头
WHERE LEFT(t.from_tax_id, 2) IN ('91', '92') 
  AND LEFT(t.to_tax_id, 2) IN ('91', '92');

-- ======================================
-- 步骤2：创建temp_trade_yearmon_unique（基于筛选后的数据）
-- ======================================
DROP TABLE IF EXISTS temp_trade_yearmon_unique;
CREATE TABLE temp_trade_yearmon_unique (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `year_mon` varchar(20) NOT NULL,
  `total_amount` decimal(20, 2) NULL COMMENT '每个year_mon的唯一金额'
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`, `year_mon`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_yearmon_unique
SELECT DISTINCT
  from_tax_id,
  to_tax_id,
  invoice_type,
  year_mon,
  total_amount
FROM temp_filtered_trade;

-- ======================================
-- 步骤3：预去重商品编码（基于筛选后的数据）
-- ======================================
DROP TABLE IF EXISTS temp_trade_goods_distinct;
CREATE TABLE temp_trade_goods_distinct (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `goods_code` varchar(7) NOT NULL
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`, `goods_code`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_goods_distinct
SELECT DISTINCT
  from_tax_id,
  to_tax_id,
  invoice_type,
  goods_code
FROM temp_filtered_trade;

-- ======================================
-- 步骤4：聚合商品编码（基于筛选后的数据）
-- ======================================
DROP TABLE IF EXISTS temp_trade_goods;
CREATE TABLE temp_trade_goods (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `goods_code` varchar(65533) NULL,
  `goods_code_num` int(50) NULL
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_goods
SELECT
  from_tax_id,
  to_tax_id,
  invoice_type,
  SUBSTRING(IFNULL(GROUP_CONCAT(goods_code SEPARATOR ','), ''), 1, 10000) AS goods_code,
  COUNT(goods_code) AS goods_code_num
FROM temp_trade_goods_distinct
GROUP BY from_tax_id, to_tax_id, invoice_type;

-- ======================================
-- 步骤5：预计算金额/次数/月份（基于筛选后的数据）
-- ======================================
DROP TABLE IF EXISTS temp_trade_amount;
CREATE TABLE temp_trade_amount (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `total_amount` decimal(20, 2) NULL,
  `invoice_num` int(50) NULL,
  `month_count` int(11) NULL
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_amount
SELECT
  ym.from_tax_id,
  ym.to_tax_id,
  ym.invoice_type,
  COALESCE(SUM(ym.total_amount), 0.00) AS total_amount,
  COALESCE(SUM(t.invoice_num), 0) AS invoice_num,
  COUNT(DISTINCT ym.year_mon) AS month_count
FROM temp_trade_yearmon_unique ym
LEFT JOIN temp_filtered_trade t
  ON ym.from_tax_id = t.from_tax_id
  AND ym.to_tax_id = t.to_tax_id
  AND ym.invoice_type = t.invoice_type
GROUP BY ym.from_tax_id, ym.to_tax_id, ym.invoice_type;

-- ======================================
-- 步骤6：合并临时表（基于筛选后的数据）
-- ======================================
DROP TABLE IF EXISTS temp_trade_base;
CREATE TABLE temp_trade_base (
  `from_tax_id` varchar(100) NOT NULL,
  `to_tax_id` varchar(100) NOT NULL,
  `invoice_type` varchar(50) NOT NULL,
  `goods_code` varchar(65533) NULL,
  `goods_code_num` int(50) NULL,
  `total_amount` decimal(20, 2) NULL,
  `invoice_num` int(50) NULL,
  `month_count` int(11) NULL
) ENGINE=OLAP 
UNIQUE KEY(`from_tax_id`, `to_tax_id`, `invoice_type`)
DISTRIBUTED BY HASH(`from_tax_id`) BUCKETS 64
PROPERTIES (
"replication_num" = "3",
"in_memory" = "false",
"compression" = "LZ4"
);

INSERT OVERWRITE temp_trade_base
SELECT
  g.from_tax_id,
  g.to_tax_id,
  g.invoice_type,
  g.goods_code,
  g.goods_code_num,
  a.total_amount,
  a.invoice_num,
  a.month_count
FROM temp_trade_goods g
LEFT JOIN temp_trade_amount a
  ON g.from_tax_id = a.from_tax_id
  AND g.to_tax_id = a.to_tax_id
  AND g.invoice_type = a.invoice_type;

-- ======================================
-- 步骤7：计算指标最大最小值（基于**过滤后**的数据）
-- ======================================
DROP TABLE IF EXISTS temp_indicator_minmax;
CREATE TABLE temp_indicator_minmax (
  `total_amount_min` decimal(20, 2) NULL,
  `total_amount_max` decimal(20, 2) NULL,
  `invoice_num_min` int(50) NULL,
  `invoice_num_max` int(50) NULL,
  `goods_code_num_min` int(50) NULL,
  `goods_code_num_max` int(50) NULL,
  `month_count_min` int(11) NULL,
  `month_count_max` int(11) NULL
) ENGINE=OLAP 
DISTRIBUTED BY HASH(`total_amount_min`) BUCKETS 1
PROPERTIES (
"replication_num" = "3",
"in_memory" = "true"
);

INSERT INTO temp_indicator_minmax
SELECT
  MIN(total_amount) AS total_amount_min,
  MAX(total_amount) AS total_amount_max,
  MIN(invoice_num) AS invoice_num_min,
  MAX(invoice_num) AS invoice_num_max,
  MIN(goods_code_num) AS goods_code_num_min,
  MAX(goods_code_num) AS goods_code_num_max,
  MIN(month_count) AS month_count_min,
  MAX(month_count) AS month_count_max
FROM temp_trade_base;

-- ======================================
-- 步骤8：填充新表（核心修改：新增chain_name列，值为机器人产业链；表名替换）
-- ======================================
-- 替换原有的temp_quantile和等级判断部分，其余逻辑不变
TRUNCATE TABLE AM_chain_enterprise_recommend_2401_2412_ly;

INSERT INTO AM_chain_enterprise_recommend_2401_2412_ly (
  chain_name,
  from_tax_id,
  to_tax_id,
  invoice_type,
  company_name,
  trade_type,
  recommend_company_name,
  goods_code,
  goods_code_num,
  total_amount,
  invoice_num,
  trade_score,
  trade_rate
)
WITH temp_score AS (
  -- 原有的打分逻辑，不变
  SELECT
    t.*,
    ROUND(
      IF(m.total_amount_max = m.total_amount_min, 50, (t.total_amount - m.total_amount_min)/(m.total_amount_max - m.total_amount_min)*100)*0.4
      + IF(m.invoice_num_max = m.invoice_num_min,50, (t.invoice_num - m.invoice_num_min)/(m.invoice_num_max - m.invoice_num_min)*100)*0.25
      + IF(m.goods_code_num_max = m.goods_code_num_min,50, (t.goods_code_num - m.goods_code_num_min)/(m.goods_code_num_max - m.goods_code_num_min)*100)*0.2
      + IF(m.month_count_max = m.month_count_min,50, (t.month_count - m.month_count_min)/(m.month_count_max - m.month_count_min)*100)*0.15
    ,2) AS trade_score
  FROM temp_trade_base t
  CROSS JOIN temp_indicator_minmax m
),
temp_score_with_company AS (
  -- 原有的关联和过滤逻辑，不变
  SELECT DISTINCT
    ts.*,
    IFNULL(te.company_name, '未知企业') AS company_name,
    CASE 
      WHEN ts.from_tax_id = te.tax_id THEN '经销商'
      WHEN ts.to_tax_id = te.tax_id THEN '供应商'
    END AS trade_type,
    COALESCE(NULLIF(cb.company_name, ''), '未知企业') AS recommend_company_name
  FROM temp_score ts
  JOIN temp_target_enterprise te
    ON ts.from_tax_id = te.tax_id OR ts.to_tax_id = te.tax_id
  LEFT JOIN `fintech_data_lake`.`jwy_ods__mongo_pazxdb__company_base__si` cb
    ON CASE 
      WHEN ts.from_tax_id = te.tax_id THEN ts.to_tax_id
      WHEN ts.to_tax_id = te.tax_id THEN ts.from_tax_id
    END = cb.credit_no
  WHERE COALESCE(NULLIF(cb.company_name, ''), '未知企业') <> '未知企业'
),
-- 步骤1：提取去重的trade_score（关键：只保留唯一分值）
distinct_scores AS (
  SELECT DISTINCT trade_score
  FROM temp_score_with_company
),
-- 步骤2：对去重后的分值计算分位数（q1、q2、q3）
score_quantile AS (
  SELECT
    -- 基于去重后的分值计算分位数（核心修改点）
    PERCENTILE_APPROX(trade_score, 0.25) AS q1, -- 25分位（去重分值的25%）
    PERCENTILE_APPROX(trade_score, 0.5) AS q2,  -- 50分位（去重分值的50%）
    PERCENTILE_APPROX(trade_score, 0.75) AS q3  -- 75分位（去重分值的75%）
  FROM distinct_scores
)
-- 最终查询：用去重分值的分位数划分原数据的等级
SELECT
  '增材制造产业链' AS chain_name,
  tsc.from_tax_id,
  tsc.to_tax_id,
  tsc.invoice_type,
  tsc.company_name,
  tsc.trade_type,
  tsc.recommend_company_name,
  SUBSTRING(COALESCE(tsc.goods_code, ''), 1, 65533) AS goods_code,
  tsc.goods_code_num,
  tsc.total_amount,
  tsc.invoice_num,
  tsc.trade_score,
  -- 核心：按去重分值的分位数区间划分等级
  CASE
    WHEN tsc.trade_score >= sq.q3 THEN 'A'  -- 最高25%的去重分值段（原数据中所有≥该值的行都为A）
    WHEN tsc.trade_score >= sq.q2 THEN 'B'  -- 25%-50%的去重分值段
    WHEN tsc.trade_score >= sq.q1 THEN 'C'  -- 50%-75%的去重分值段
    ELSE 'D'                                -- 75%-100%的去重分值段（必含最小值，一定有数据）
  END AS trade_rate
FROM temp_score_with_company tsc
-- 关联去重分值的分位数结果（全局唯一一行，CROSS JOIN不影响数据量）
CROSS JOIN score_quantile sq
WHERE
  tsc.from_tax_id IS NOT NULL
  AND tsc.to_tax_id IS NOT NULL
  AND tsc.recommend_company_name IS NOT NULL;

-- ======================================
-- 清理临时表
-- ======================================
DROP TABLE IF EXISTS temp_chain_mapcode;
DROP TABLE IF EXISTS temp_target_enterprise;
DROP TABLE IF EXISTS temp_filtered_trade;
DROP TABLE IF EXISTS temp_trade_yearmon_unique;
DROP TABLE IF EXISTS temp_trade_goods_distinct;
DROP TABLE IF EXISTS temp_trade_goods;
DROP TABLE IF EXISTS temp_trade_amount;
DROP TABLE IF EXISTS temp_trade_base;
DROP TABLE IF EXISTS temp_indicator_minmax;