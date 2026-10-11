-- 为每个表创建索引
CREATE INDEX idx_company_2023 ON PI_chain_final_unique_enterprise_2023_ly(company_name);
CREATE INDEX idx_company_2024 ON PI_chain_final_unique_enterprise_2024_ly(company_name);
CREATE INDEX idx_company_2025 ON PI_chain_final_unique_enterprise_2025_ly(company_name);
WITH
-- 1. 找出三年都存在的公司名（只基于公司名）
common_companies AS (
    SELECT DISTINCT t2025.company_name
    FROM PI_chain_final_unique_enterprise_2025_ly t2025
    INNER JOIN PI_chain_final_unique_enterprise_2024_ly t2024 
        ON t2025.company_name = t2024.company_name
    INNER JOIN PI_chain_final_unique_enterprise_2023_ly t2023 
        ON t2025.company_name = t2023.company_name
),
-- 2. 为每个公司从 2025 表中随机选一行，记录 id 和等级
company_random_row AS (
    SELECT id, company_name, industry_relevance_level
    FROM (
        SELECT id, company_name, industry_relevance_level,
               ROW_NUMBER() OVER (PARTITION BY company_name ORDER BY RAND()) AS rn
        FROM PI_chain_final_unique_enterprise_2025_ly
        WHERE company_name IN (SELECT company_name FROM common_companies)
          AND industry_relevance_level IN ('A', 'B', 'C', 'D')
    ) t
    WHERE rn = 1
),
-- 3. 在每个等级内随机编号
ranked_companies AS (
    SELECT id, company_name, industry_relevance_level,
           ROW_NUMBER() OVER (PARTITION BY industry_relevance_level ORDER BY RAND()) AS level_rn
    FROM company_random_row
)
-- 4. 按等级取指定数量的公司 id
SELECT company_name,industry_relevance_level
FROM ranked_companies
WHERE (industry_relevance_level = 'A' AND level_rn <= 40)
   OR (industry_relevance_level = 'B' AND level_rn <= 30)
   OR (industry_relevance_level = 'C' AND level_rn <= 20)
   OR (industry_relevance_level = 'D' AND level_rn <= 10);


-- 另一种删除索引的语法
DROP INDEX idx_company_2023 ON PI_chain_final_unique_enterprise_2023_ly;
DROP INDEX idx_company_2024 ON PI_chain_final_unique_enterprise_2024_ly;
DROP INDEX idx_company_2025 ON PI_chain_final_unique_enterprise_2025_ly;