import os

import asyncio
import json
import re
import time
from collections import Counter

import httpx
from sqlalchemy import create_engine, text


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

DB_TARGET = os.environ['SR_TARGET_DSN']

INPUT_STAT_TABLE = "fintech_stat.company_address_yuexiu_clustered_stat_ly_refresh2"
OUTPUT_BUSINESS_AREA_STAT_TABLE = "fintech_stat.company_address_yuexiu_business_area_stat_ly_v2"

API_URL = os.environ['LLM_API_URL']  # 堡垒机内网
API_KEY = os.environ['LLM_API_KEY']
MODEL = "deepseek-r1:70b"

API_RETRY_TIMES = 3
API_RETRY_SLEEP_SECONDS = 5

MIN_COMPANY_COUNT = 50

# vLLM max_model_len=4096 的下每批 label 数：系统提示词 ~900 tokens +
# 每个 label ~450 tokens + <think> 预留 ~1500 tokens
LLM_BATCH_SIZE = 3
LLM_CONCURRENCY = 4

INSERT_BATCH_SIZE = 500
MAJOR_INDUSTRY_TOP_N = 3

OUTPUT_INDUSTRY_COLUMNS = [
    (
        "customer_industry_l3_names_stat",
        "VARCHAR(5000) COMMENT '客户给的商圈国标行业中类统计'",
        "商圈客户中类行业统计",
    ),
    (
        "customer_industry_l2_names_stat",
        "VARCHAR(5000) COMMENT '客户给的商圈国标行业大类统计'",
        "商圈客户大类行业统计",
    ),
]

# ------------------------------------------------------------
# Prompt
# ------------------------------------------------------------

SYSTEM_PROMPT = """
你是一位熟悉广州市越秀区商业地理的专家。

你将收到一组"位置标签"数据，每个标签代表越秀区内一条路或几条相邻路的聚合。
每个标签包含：标签名(cluster_name)、企业数量(company_count)、路名列表(road_names)、主要楼宇/商场名称(top_entity_names)、所属街道(town_streets)、客户/基础维表行业中类(top_company_industry_l3_names)、客户/基础维表行业大类(top_company_industry_l2_names)。
行业画像只作为辅助证据。

你的任务是：综合以下三个维度，判断哪些标签属于同一个商圈，给出商圈名称和判断依据：
  a) 地理位置：路名是否相邻或属于同一段道路，是否位于同一街道
  b) 标志物：是否共享同一个商场、综合体、地标性楼宇
  c) 商业特征：高频行业（大类/中类）是否一致或构成合理组合
     商圈往往有清晰的行业画像，例如：
       · CBD / 商务写字楼商圈：金融业、商务服务业高频
       · 传统商业街 / 购物商圈：零售业、餐饮业、住宿业、居民服务高频
       · 专业批发市场：批发业及相关配套行业高频
       · 文化/教育聚集区：教育、文化艺术、新闻出版相关行业高频
       · 产业聚集 / 开发区：制造业、软件和信息技术服务业、科学研究和技术服务业、交通运输仓储业高频（涵盖工业园区、高新技术开发区、科技园、物流园等子类）
     同一商圈的标签之间行业画像应当一致或互补；画像差异很大时，通常意味着不是同一个商圈。

输出要求：
1. 输出一个 JSON 数组，每个元素代表一个商圈
2. 每个商圈包含以下字段：
   - "商圈"：商圈名称
   - "labels"：属于该商圈的标签名列表
   - "置信度"：你对这个判断的置信度，取值 "高"、"中"、"低"
3. 一个标签可以不属于任何商圈（如果无法判断）
4. 一个标签只能属于一个商圈
5. 商圈命名规则：优先使用现实中确定存在的固定名称；不确定是否为固定商圈名时，使用“核心道路/地标/街道 + 片区/商贸片区/专业市场片区”。不要凭空创造听起来合理但缺少输入证据的名称。对批发业区域，只有核心地标明确是某个批发市场/专业市场时，才使用“XXX批发市场”；否则使用更中性的“XXX商贸片区/专业市场片区/贸易区”。如果缺少稳定命名证据，宁可不输出该商圈。
6. 置信度说明（按上述三个维度综合评估）：
   - "高"：三个维度中至少两个明确支持
     · 例：路名相邻 + 楼宇/商场共享
     · 例：路名相邻 + 行业画像高度一致（如均为金融业主导的 CBD 标签）
   - "中"：仅一个维度明确支持，或多个维度弱支持
     · 例：路名接近但楼宇分散、行业画像也较杂
     · 例：仅行业画像接近，但路名和楼宇分布较散
   - "低"：三个维度都缺乏强证据，主要靠推测
7. 只输出 JSON 数组，不要输出任何其他内容\
"""


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

_engine_cache = None

def get_output_industry_columns():
    """返回商圈统计表需要输出的行业统计列。"""
    return list(OUTPUT_INDUSTRY_COLUMNS)


def get_engine():
    """统一获取数据库引擎（单例缓存）。"""
    global _engine_cache
    if _engine_cache is None:
        _engine_cache = create_engine(DB_TARGET, pool_pre_ping=True)
    return _engine_cache


def normalize_value(value):
    if value is None:
        return ""
    return str(value).strip()


def chunk_rows(rows, chunk_size):
    for i in range(0, len(rows), chunk_size):
        yield rows[i:i + chunk_size]


def load_labels():
    """从 clustered_stat 表读取全量 label。"""
    sql = text(
        f"""
        SELECT
            cluster_name,
            company_count,
            road_names,
            top_entity_names,
            town_streets,
            top_company_industry_l2_names,
            top_company_industry_l3_names
        FROM {INPUT_STAT_TABLE}
        """
    )

    labels = []
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(sql)
        for db_row in result.mappings():
            labels.append({
                "cluster_name": normalize_value(db_row.get("cluster_name")),
                "company_count": int(db_row.get("company_count") or 0),
                "road_names": normalize_value(db_row.get("road_names")),
                "top_entity_names": normalize_value(db_row.get("top_entity_names")),
                "town_streets": normalize_value(db_row.get("town_streets")),
                "top_company_industry_l2_names": normalize_value(db_row.get("top_company_industry_l2_names")),
                "top_company_industry_l3_names": normalize_value(db_row.get("top_company_industry_l3_names")),
            })
    return labels


def build_user_prompt(labels):
    """把所有 label 拼成 LLM 可读的文本。"""
    lines = []
    for label in labels:
        parts = [f"标签: {label['cluster_name']}  企业数: {label['company_count']}"]
        if label["road_names"]:
            parts.append(f"  路名: {label['road_names']}")
        if label["top_entity_names"]:
            parts.append(f"  主要楼宇/商场: {label['top_entity_names']}")
        if label["town_streets"]:
            parts.append(f"  街道: {label['town_streets']}")
        if label["top_company_industry_l3_names"]:
            parts.append(f"  客户/基础维表行业中类高频分布: {label['top_company_industry_l3_names']}")
        if label["top_company_industry_l2_names"]:
            parts.append(f"  客户/基础维表行业大类高频分布: {label['top_company_industry_l2_names']}")
        lines.append("\n".join(parts))

    header = f"以下是广州市越秀区 {len(labels)} 个位置标签的数据：\n"
    return header + "\n\n".join(lines)


async def call_llm_async(client, system_prompt, user_prompt, batch_number):
    """异步调用堡垒机内网 vLLM，带重试。"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }

    last_error = None
    for attempt in range(1, API_RETRY_TIMES + 1):
        try:
            print(f"批次 {batch_number} 调用 LLM（第 {attempt} 次）...")
            resp = await client.post(API_URL, headers=headers, json=payload, timeout=600)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            print(f"批次 {batch_number} LLM 返回 {len(content)} 字符")
            return content
        except httpx.HTTPStatusError as e:
            last_error = e
            resp_text = getattr(e.response, "text", "") or ""
            print(f"批次 {batch_number} HTTP 出错: {e}  响应: {resp_text[:500]}")
            if attempt < API_RETRY_TIMES:
                await asyncio.sleep(API_RETRY_SLEEP_SECONDS * attempt)
        except (httpx.RequestError, httpx.TimeoutException, KeyError, IndexError) as e:
            last_error = e
            print(f"批次 {batch_number} 请求出错: {type(e).__name__}: {e}")
            if attempt < API_RETRY_TIMES:
                await asyncio.sleep(API_RETRY_SLEEP_SECONDS * attempt)

    raise RuntimeError(f"LLM 调用失败，最后错误: {last_error}") from last_error


def extract_json_array(content):
    """从 LLM 返回内容中提取 JSON 数组，兼容 DeepSeek R1 的 <think> 标签。"""
    text_value = content.strip()

    # DeepSeek R1 会返回 <think>...</think> 思考过程，先剥离
    text_value = re.sub(r"<think>.*?</think>", "", text_value, flags=re.S).strip()

    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?", "", text_value).strip()
        text_value = re.sub(r"```$", "", text_value).strip()

    idx = text_value.find("[")
    if idx != -1:
        try:
            result, _ = json.JSONDecoder().raw_decode(text_value, idx)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 返回内容中解析 JSON 数组:\n{text_value[:500]}")


NAME_STAT_ITEM_PATTERN = re.compile(r"^\s*(.+?)\((\d+)\)\s*$")


def parse_name_stat_items(value):
    """解析 `名称(数量)` 逗号串。"""
    items = []
    text_value = (value or "").strip()
    if not text_value:
        return items

    for part in text_value.split(","):
        part = part.strip()
        if not part:
            continue
        match = NAME_STAT_ITEM_PATTERN.match(part)
        if not match:
            print(f"警告: 无法解析名称统计项: {part}")
            continue
        name, count = match.groups()
        items.append((name.strip(), int(count)))
    return items


def merge_name_stat_strings(values):
    """合并多个 `名称(数量)` 字符串，并按数量降序输出。"""
    counter = Counter()

    for value in values:
        for name, count in parse_name_stat_items(value):
            counter[name] += count

    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return ",".join(f"{name}({count})" for name, count in ordered)


def top_industry_names_from_name_stat(value, limit=MAJOR_INDUSTRY_TOP_N):
    """从 `名称(数量)` 统计串取企业数前三的名称。"""
    items = sorted(
        parse_name_stat_items(value),
        key=lambda item: (-item[1], item[0]),
    )
    return ",".join(name for name, _ in items[:limit])


VALID_CONFIDENCE = {"高", "中", "低"}
REQUIRED_DISTRICT_FIELDS = ("商圈", "labels", "置信度")


def validate_districts(districts, labels):
    """校验 LLM 返回：字段完整、置信度合法、label 不重复、label 已知。"""
    known_label_names = {label["cluster_name"] for label in labels}
    seen_labels = {}
    issues = 0

    for i, d in enumerate(districts):
        if not isinstance(d, dict):
            print(f"警告: 第 {i} 项不是 dict: {d!r}")
            issues += 1
            continue

        for field in REQUIRED_DISTRICT_FIELDS:
            if field not in d:
                print(f"警告: 商圈 {d.get('商圈', f'#{i}')} 缺少字段 {field}")
                issues += 1

        confidence = d.get("置信度")
        if confidence is not None and confidence not in VALID_CONFIDENCE:
            print(f"警告: 商圈 {d.get('商圈', f'#{i}')} 置信度非法: {confidence!r}")
            issues += 1

        district_labels = d.get("labels", []) or []
        if not isinstance(district_labels, list):
            print(f"警告: 商圈 {d.get('商圈', f'#{i}')} 的 labels 不是列表: {district_labels!r}")
            issues += 1
            continue

        for name in district_labels:
            if name not in known_label_names:
                print(f"警告: 商圈 {d.get('商圈', f'#{i}')} 引用未知标签: {name}")
                issues += 1
            if name in seen_labels:
                print(
                    f"警告: 标签 {name} 被重复分配到商圈 "
                    f"{seen_labels[name]} 和 {d.get('商圈', f'#{i}')}"
                )
                issues += 1
            else:
                seen_labels[name] = d.get("商圈", f"#{i}")

    if issues == 0:
        print("LLM 返回校验通过")
    else:
        print(f"LLM 返回校验发现 {issues} 个问题（已打印详情）")


CONFIDENCE_RANK = {"高": 3, "中": 2, "低": 1}


def merge_districts_by_name(districts):
    """跨批次合并：相同商圈名的条目把 labels、依据取并集，置信度取最低。"""
    merged = {}

    for d in districts:
        if not isinstance(d, dict):
            continue
        name = (d.get("商圈") or "").strip()
        if not name:
            continue

        entry = merged.get(name)
        if entry is None:
            merged[name] = {
                "商圈": name,
                "labels": list(d.get("labels", []) or []),
                "依据_entity": list(d.get("依据_entity", []) or []),
                "依据_road": list(d.get("依据_road", []) or []),
                "依据_行业": list(d.get("依据_行业", []) or []),
                "置信度": d.get("置信度", ""),
            }
            continue

        for field in ("labels", "依据_entity", "依据_road", "依据_行业"):
            for item in d.get(field, []) or []:
                if item not in entry[field]:
                    entry[field].append(item)

        new_conf = d.get("置信度", "")
        if CONFIDENCE_RANK.get(new_conf, 0) < CONFIDENCE_RANK.get(entry["置信度"], 0):
            entry["置信度"] = new_conf

    return list(merged.values())


async def process_single_batch_async(
    batch_idx,
    total_batches,
    batch,
    client,
    semaphore,
    system_prompt,
):
    """处理单个 LLM 批次，失败时返回失败标记而不中断全局。"""
    batch_number = batch_idx + 1
    batch_t0 = time.time()
    print(f"\n--- 批次 {batch_number}/{total_batches}（{len(batch)} 个 label） ---")

    user_prompt = build_user_prompt(batch)

    try:
        async with semaphore:
            raw = await call_llm_async(client, system_prompt, user_prompt, batch_number)
    except Exception as e:
        print(f"批次 {batch_number} LLM 调用失败: {e}")
        return {
            "batch_idx": batch_idx,
            "failed": True,
            "districts": [],
        }

    try:
        districts = extract_json_array(raw)
        elapsed = time.time() - batch_t0
        print(f"批次 {batch_number} 解析出 {len(districts)} 个商圈，耗时 {elapsed:.1f}s")
        return {
            "batch_idx": batch_idx,
            "failed": False,
            "districts": districts,
        }
    except ValueError as e:
        print(f"批次 {batch_number} JSON 解析失败: {e}")
        return {
            "batch_idx": batch_idx,
            "failed": True,
            "districts": [],
        }


async def process_labels_in_batches_async(labels, batch_size, system_prompt):
    """分批异步调 LLM，收集并合并所有 districts。"""
    total = len(labels)
    total_batches = (total + batch_size - 1) // batch_size
    all_districts_raw = []
    failed_batches = []

    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
    async with httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=LLM_CONCURRENCY + 4,
            max_keepalive_connections=LLM_CONCURRENCY,
        )
    ) as client:
        tasks = []
        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            batch = labels[start:start + batch_size]
            tasks.append(
                process_single_batch_async(
                    batch_idx=batch_idx,
                    total_batches=total_batches,
                    batch=batch,
                    client=client,
                    semaphore=semaphore,
                    system_prompt=system_prompt,
                )
            )
        results = await asyncio.gather(*tasks)

    for result in sorted(results, key=lambda item: item["batch_idx"]):
        if result["failed"]:
            failed_batches.append(result["batch_idx"] + 1)
            continue
        all_districts_raw.extend(result["districts"])

    if failed_batches:
        print(f"\n警告: {len(failed_batches)} 个批次失败: {failed_batches}")

    merged = merge_districts_by_name(all_districts_raw)
    print(f"\n跨批次合并: {len(all_districts_raw)} 条原始条目 → {len(merged)} 个唯一商圈")
    return merged


def enrich_districts_with_stats(districts, labels):
    """基于商圈包含的 labels 计算商圈企业数和行业统计。"""
    label_map = {label["cluster_name"]: label for label in labels}
    enriched = []

    for district in districts:
        current = dict(district)
        district_labels = current.get("labels", []) or []

        matched_labels = []
        for cluster_name in district_labels:
            label = label_map.get(cluster_name)
            if label is None:
                print(f"警告: 商圈 {current.get('商圈', '')} 包含未知标签 {cluster_name}，已跳过")
                continue
            matched_labels.append(label)

        current["商圈企业数量"] = sum(label.get("company_count", 0) for label in matched_labels)
        current["商圈客户中类行业统计"] = merge_name_stat_strings(
            label.get("top_company_industry_l3_names", "") for label in matched_labels
        )
        current["商圈客户大类行业统计"] = merge_name_stat_strings(
            label.get("top_company_industry_l2_names", "") for label in matched_labels
        )
        current["major_l3_name"] = top_industry_names_from_name_stat(
            current["商圈客户中类行业统计"]
        )
        current["major_l2_name"] = top_industry_names_from_name_stat(
            current["商圈客户大类行业统计"]
        )
        enriched.append(current)

    return enriched


# ------------------------------------------------------------
# 数据库写入
# ------------------------------------------------------------

def create_business_area_stat_table():
    """重建商圈统计结果表。"""
    industry_columns = get_output_industry_columns()
    column_definitions = [
        "business_area VARCHAR(255) COMMENT '商圈'",
        "labels VARCHAR(5000) COMMENT '包含的位置标签'",
        "company_count BIGINT COMMENT '商圈企业数量'",
        "major_l3_name VARCHAR(1000) COMMENT '企业数排名前三的行业中类'",
        "major_l2_name VARCHAR(1000) COMMENT '企业数排名前三的行业大类'",
    ]
    column_definitions.extend(
        f"{column_name} {column_definition}"
        for column_name, column_definition, _ in industry_columns
    )
    column_definitions.append("confidence VARCHAR(16) COMMENT '置信度'")
    columns_sql = ",\n        ".join(column_definitions)

    drop_sql = f"DROP TABLE IF EXISTS {OUTPUT_BUSINESS_AREA_STAT_TABLE}"
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {OUTPUT_BUSINESS_AREA_STAT_TABLE} (
        {columns_sql}
    ) ENGINE=OLAP
    DUPLICATE KEY(business_area)
    DISTRIBUTED BY HASH(business_area) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(drop_sql))
        conn.execute(text(create_sql))


def write_business_area_stat_table(districts):
    """写商圈主表。"""
    industry_columns = get_output_industry_columns()
    create_business_area_stat_table()

    if not districts:
        return 0

    insert_columns = [
        "business_area",
        "labels",
        "company_count",
        "major_l3_name",
        "major_l2_name",
    ]
    insert_columns.extend(column_name for column_name, _, _ in industry_columns)
    insert_columns.append("confidence")
    insert_column_sql = ",\n            ".join(insert_columns)
    insert_value_sql = ",\n            ".join(f":{column_name}" for column_name in insert_columns)

    insert_sql = text(
        f"""
        INSERT INTO {OUTPUT_BUSINESS_AREA_STAT_TABLE} (
            {insert_column_sql}
        ) VALUES (
            {insert_value_sql}
        )
        """
    )

    payloads = []
    for d in districts:
        payload = {
            "business_area": d.get("商圈", ""),
            "labels": ",".join(d.get("labels", []) or []),
            "company_count": int(d.get("商圈企业数量", 0) or 0),
            "major_l3_name": d.get("major_l3_name", ""),
            "major_l2_name": d.get("major_l2_name", ""),
            "confidence": d.get("置信度", ""),
        }
        for column_name, _, district_field_name in industry_columns:
            payload[column_name] = d.get(district_field_name, "")
        payloads.append(payload)

    engine = get_engine()
    with engine.begin() as conn:
        for batch in chunk_rows(payloads, INSERT_BATCH_SIZE):
            conn.execute(insert_sql, batch)

    return len(payloads)


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

async def main_async():
    system_prompt = SYSTEM_PROMPT

    print(f"输入表（stat）: {INPUT_STAT_TABLE}")
    print(f"输出表（商圈统计）: {OUTPUT_BUSINESS_AREA_STAT_TABLE}")
    print(f"LLM 接口: {API_URL}")
    print(f"LLM 模型: {MODEL}")
    print(f"LLM 并发数: {LLM_CONCURRENCY}")
    print("行业字段口径: 客户/基础维表行业中类 + 大类")

    all_labels = load_labels()
    print(f"读取到全量标签数: {len(all_labels)}")

    labels = [l for l in all_labels if l["company_count"] >= MIN_COMPANY_COUNT]
    dropped = len(all_labels) - len(labels)
    print(f"过滤后标签数: {len(labels)}（丢弃 company_count < {MIN_COMPANY_COUNT} 的 {dropped} 个）")

    total_batches = (len(labels) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
    print(f"分批调用：每批 {LLM_BATCH_SIZE} 个 label，共 {total_batches} 批（并发 {LLM_CONCURRENCY}）")

    districts = await process_labels_in_batches_async(
        labels,
        LLM_BATCH_SIZE,
        system_prompt,
    )
    validate_districts(districts, labels)
    districts = enrich_districts_with_stats(districts, labels)
    print(f"合并后共 {len(districts)} 个商圈")

    written_ba_stat = write_business_area_stat_table(districts)
    print(f"商圈统计表写入完成: {OUTPUT_BUSINESS_AREA_STAT_TABLE}（{written_ba_stat} 行）")

    # 统计
    assigned_label_names = set()
    for d in districts:
        for name in d.get("labels", []) or []:
            assigned_label_names.add(name)

    unassigned_labels = [
        label["cluster_name"] for label in labels
        if label["cluster_name"] not in assigned_label_names
    ]

    confidence_counts = {}
    for d in districts:
        c = d.get("置信度", "未知")
        confidence_counts[c] = confidence_counts.get(c, 0) + 1

    print(f"\n--- 统计 ---")
    print(f"输入标签数: {len(labels)}")
    print(f"商圈数: {len(districts)}")
    print(f"已归入商圈的标签数: {len(assigned_label_names)}")
    print(f"未归入商圈的标签数: {len(unassigned_labels)}")
    print(f"置信度分布: {confidence_counts}")

    if unassigned_labels:
        print(f"\n--- 未归入商圈的标签（{len(unassigned_labels)} 个） ---")
        for name in unassigned_labels:
            print(f"  - {name}")


if __name__ == "__main__":
    asyncio.run(main_async())
