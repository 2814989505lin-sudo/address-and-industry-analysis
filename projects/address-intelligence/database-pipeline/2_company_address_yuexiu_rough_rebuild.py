import os

import json
import re
import signal
import sys
import time
import asyncio
import httpx
from functools import wraps

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

DB_TARGET = os.environ['SR_TARGET_DSN']
API_KEY = os.environ['LLM_API_KEY']

# 数据源和结果表名固定为当前任务指定值。
ROUGH_TABLE = "fintech_stat.company_address_yuexiu_rough_ly"
CLEAN_TABLE = "fintech_stat.company_address_yuexiu_clean_ly"
PROGRESS_TABLE = "fintech_stat.company_address_yuexiu_rebuild_progress_ly"
PROCESSED_TABLE = "fintech_stat.company_address_yuexiu_processed_ly"

# 硅基流动 DeepSeek 兼容接口。
API_URL = os.environ['LLM_API_URL'] #堡垒机内网
MODEL = "deepseek-r1:70b"  # 修改模型名称
API_RETRY_TIMES = 5
API_RETRY_SLEEP_SECONDS = 2

# 并发配置
CONCURRENCY = 20  # 同时发给 vLLM 的最大请求数

# 批量保存进度和批量写库的粒度。
SAVE_INTERVAL = 100  # 每处理完一批就落库，配合并发可以设大一些
INSERT_BATCH_SIZE = 200

# 数据库操作重试配置
DB_RETRY_TIMES = 5
DB_RETRY_SLEEP_SECONDS = 1
DB_RETRY_BACKOFF_FACTOR = 2  # 指数退避因子

# 运行状态。
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

# 是否已经写入 processed 表的状态。
PROCESSED_STATUS_PENDING = "pending"
PROCESSED_STATUS_DONE = "done"

# 最终要保留的 8 个结构化字段。
STRUCTURED_FIELDS = [
    "province",
    "city",
    "district",
    "town_street",
    "road_name",
    "road_no",
    "entity_name",
    "additional_information",
]

# progress 和 processed 的基础字段顺序统一固定下来，避免后面写库列顺序漂移。
BASE_RESULT_FIELDS = [
    "credit_no",
    "company_name",
    "company_address",
] + STRUCTURED_FIELDS

PROGRESS_FIELDS = BASE_RESULT_FIELDS + [
    "api_error",
    "run_status",
    "processed_status",
]

# 系统提示词（固定部分）
SYSTEM_PROMPT = """
你是中文地址结构化助手。请只根据输入里的 company_address 做字段抽取。

要求：
1. 所有字段值都必须直接来自 company_address 原文，且必须是原文中的连续子串
2. 不允许补全、猜测、改写或归一化成 company_address 里不存在的写法
3. 同一段文本不要重复出现在多个字段中
4. 只输出一个合法 JSON 对象，不要输出解释
5. 禁止猜测性提取：如果不确定某段文本的归属字段，默认归入 additional_information

注意事项：
1. road_name 不要提取市场/园区/小区内部的编号道路
2. road_name 不要提取非正式的道路俗称
3. 如果地址中没有正式道路名称，road_name 留空
4. 如果 road_no 前面紧挨着的内容不是 road_name，则留空
5. road_no 只提取主干路号，格式为：数字 + "号"
6. road_no 如果有"之X"、"之X至Y号"、"附X"、"支X"等后缀，只保留到第一个"之"、"附"、"支"之前
7. road_no 不要包含后面的单元号、楼层号、铺位号或其他任何内容
8. entity_name 不要提取道路类词
9. entity_name 不要提取市场内部的区域/通道编号
10. entity_name 不要提取单字母、单数字、纯编号
11. entity_name 不要提取未闭合括号的内容
12. entity_name 不要提取单独的实体后缀
13. 当地址中同时出现公司全称和商业实体名称时，优先提取商业实体名称
"""

# 字段定义提示词
STANDARD_GOODNAME_PROMPT = """
字段框架与定义：
你只能输出以下 8 个字段，且字段名必须完全一致：

- `province`：省级行政区划，如"广东省"
- `city`：地级市或直辖市，如"广州市""北京市"
- `district`：区、县、县级市，如"白云区""天河区"
- `town_street`：镇、街道、街镇等次级行政区划，如"狮岭镇""石牌街道"
- `road_name`：道路名称，必须是正式的道路名。
- `road_no`：纯路号，如"208号""77-79号""77至79号"。
- `entity_name`：重要的商业/办公/居住类实体名称，必须是完整的名称。
- `additional_information`：前面字段抽取完成后，剩余的全部文本；包括：市场内部区域名、楼层号、房间号、档口号、铺位号、单元号，以及括号内的补充说明中不属于实体名称的部分
"""

# 合并后的系统提示词
FULL_SYSTEM_PROMPT = SYSTEM_PROMPT + STANDARD_GOODNAME_PROMPT

# 中断保存用的全局变量。
current_rows = None
current_rows_by_key = None
dirty_row_keys = None
processed_appended_count = 0


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def normalize_value(value):
    """把数据库值统一成干净字符串。"""
    if value is None:
        return ""
    return str(value).strip()

_engine_cache = None

def get_engine():
    """统一获取数据库引擎（单例缓存，避免重复建池）。"""
    global _engine_cache
    if _engine_cache is None:
        if not DB_TARGET:
            raise ValueError("缺少环境变量 DB_TARGET，无法建立数据库连接。")
        _engine_cache = create_engine(DB_TARGET, pool_pre_ping=True)
    return _engine_cache


def make_row_key(row):
    """用三列组合成稳定主键，避免同地址不同公司互相覆盖。"""
    return (
        normalize_value(row.get("credit_no", "")),
        normalize_value(row.get("company_name", "")),
        normalize_value(row.get("company_address", "")),
    )


def truncate_text(value, max_length=500):
    """截断过长错误信息，避免单条异常把表字段撑爆。"""
    if len(value) <= max_length:
        return value
    return value[:max_length] + "..."


def extract_json_text(content):
    """从模型返回内容中提取第一个完整 JSON 对象。"""
    cleaned_text = content.strip()

    # DeepSeek R1 会返回 <think>...</think> 思考过程，先剥离
    cleaned_text = re.sub(r"<think>.*?</think>", "", cleaned_text, flags=re.S).strip()

    if cleaned_text.startswith("```"):
        cleaned_text = re.sub(r"^```(?:json)?", "", cleaned_text).strip()
        cleaned_text = re.sub(r"```$", "", cleaned_text).strip()

    # 用 raw_decode 只解析第一个完整 JSON 对象，忽略后面的多余内容
    idx = cleaned_text.find("{")
    if idx != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(cleaned_text, idx)
            return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    # 兜底：正则提取
    match = re.search(r"\{[^{}]*\}", cleaned_text)
    if match:
        return match.group(0)

    return cleaned_text


def build_prompt(row):
    """构造单行用户提示词（只传地址）。"""
    company_address = normalize_value(row.get("company_address", ""))
    return company_address  # 直接返回地址字符串作为用户输入


async def vllm_72b_infer_jwy_async(client, sys_text, user_text):
    """异步调用 vLLM 接口（带重试）。"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": str(sys_text)},
            {"role": "user", "content": str(user_text)}
        ],
        "temperature": 0.1
    }

    last_error = None
    for attempt in range(1, API_RETRY_TIMES + 1):
        try:
            resp = await client.post(API_URL, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
            last_error = e
            if attempt < API_RETRY_TIMES:
                await asyncio.sleep(API_RETRY_SLEEP_SECONDS * attempt)
            else:
                raise


async def rebuild_row_async(row, client):
    """异步调用模型重构单行。"""
    user_prompt = build_prompt(row)
    
    content = await vllm_72b_infer_jwy_async(
        client,
        FULL_SYSTEM_PROMPT,
        user_prompt
    )
    
    json_text = extract_json_text(content)
    rebuilt_data = json.loads(json_text)

    if not isinstance(rebuilt_data, dict):
        raise ValueError(f"模型返回的 JSON 不是对象: {truncate_text(str(rebuilt_data))}")

    result = {}
    for field_name in STRUCTURED_FIELDS:
        result[field_name] = normalize_value(rebuilt_data.get(field_name, ""))

    return result


def chunk_rows(rows, chunk_size):
    """把大列表切成固定大小的小批次。"""
    for index in range(0, len(rows), chunk_size):
        yield rows[index:index + chunk_size]


def build_db_payload(row, fields):
    """把行数据转成 SQL 参数字典。"""
    payload = {}
    for field_name in fields:
        if field_name == "additional_information":
            payload["additional_information"] = row.get(field_name, "")
        else:
            payload[field_name] = row.get(field_name, "")
    return payload



def is_db_connection_error(error):
    """判断是否为数据库连接相关的错误。"""
    error_str = str(error).lower()
    connection_errors = [
        "backend node not found",
        "connection refused",
        "can't connect",
        "lost connection",
        "connection timed out",
        "timeout",
        "server has gone away",
        "broken pipe",
        "unable to connect",
    ]
    
    return any(err in error_str for err in connection_errors)


def retry_db_operation(func):
    """装饰器：为数据库操作添加重试机制。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None
        
        for attempt in range(1, DB_RETRY_TIMES + 1):
            try:
                return func(*args, **kwargs)
            except (OperationalError, InterfaceError, DBAPIError) as e:
                last_exception = e
                
                # 检查是否为连接错误
                if not is_db_connection_error(e):
                    # 非连接错误，直接抛出
                    raise
                
                if attempt < DB_RETRY_TIMES:
                    # 计算等待时间（指数退避）
                    wait_time = DB_RETRY_SLEEP_SECONDS * (DB_RETRY_BACKOFF_FACTOR ** (attempt - 1))
                    print(f"⚠️ 数据库连接错误 (尝试 {attempt}/{DB_RETRY_TIMES}): {str(e)[:100]}")
                    print(f"   等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ 数据库连接错误，已重试 {DB_RETRY_TIMES} 次，仍然失败")
                    raise
            except Exception as e:
                # 其他异常直接抛出
                raise
        
        # 如果所有重试都失败，抛出最后一个异常
        if last_exception:
            raise last_exception
    
    return wrapper


def migrate_tables_to_unique_key():
    """检测旧的 DUPLICATE KEY 表并迁移为 UNIQUE KEY（自动处理）。"""
    engine = get_engine()
    tables = [PROGRESS_TABLE, PROCESSED_TABLE]

    for table_name in tables:
        try:
            with engine.connect() as conn:
                result = conn.execute(text(f"SHOW CREATE TABLE {table_name}"))
                row = result.first()
                if row and "DUPLICATE KEY" in str(row[1]):
                    print(f"⚠️  检测到 {table_name} 使用 DUPLICATE KEY，迁移为 UNIQUE KEY...")
                    with engine.begin() as tx:
                        tx.execute(text(f"DROP TABLE {table_name}"))
                    print(f"  ✓ 已删除旧表 {table_name}，将以 UNIQUE KEY 重建")
                elif row:
                    print(f"✓ {table_name} 已是 UNIQUE KEY")
        except Exception:
            pass  # 表不存在，后续 create_tables 会创建


def create_tables():
    """创建 progress 表和 processed 表（UNIQUE KEY 模型）。"""
    progress_sql = f"""
    CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
        credit_no VARCHAR(255) COMMENT '统一社会信用代码',
        company_name VARCHAR(255) COMMENT '公司名称',
        company_address VARCHAR(500) COMMENT '公司地址',
        province VARCHAR(255) COMMENT '省',
        city VARCHAR(255) COMMENT '市',
        district VARCHAR(255) COMMENT '区县',
        town_street VARCHAR(255) COMMENT '镇街',
        road_name VARCHAR(255) COMMENT '道路名',
        road_no VARCHAR(255) COMMENT '门牌号',
        entity_name VARCHAR(255) COMMENT '实体名',
        additional_information VARCHAR(500) COMMENT '剩余信息',
        api_error VARCHAR(2000) COMMENT '接口错误信息',
        run_status VARCHAR(50) COMMENT '运行状态',
        processed_status VARCHAR(50) COMMENT '是否已进入processed'
    ) ENGINE=OLAP
    UNIQUE KEY(credit_no, company_name, company_address)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    processed_sql = f"""
    CREATE TABLE IF NOT EXISTS {PROCESSED_TABLE} (
        credit_no VARCHAR(255) COMMENT '统一社会信用代码',
        company_name VARCHAR(255) COMMENT '公司名称',
        company_address VARCHAR(500) COMMENT '公司地址',
        province VARCHAR(255) COMMENT '省',
        city VARCHAR(255) COMMENT '市',
        district VARCHAR(255) COMMENT '区县',
        town_street VARCHAR(255) COMMENT '镇街',
        road_name VARCHAR(255) COMMENT '道路名',
        road_no VARCHAR(255) COMMENT '门牌号',
        entity_name VARCHAR(255) COMMENT '实体名',
        additional_information VARCHAR(500) COMMENT '剩余信息'
    ) ENGINE=OLAP
    UNIQUE KEY(credit_no, company_name, company_address)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(progress_sql))
        connection.execute(text(processed_sql))


def load_rough_rows():
    """从 rough 源表读取三列输入，不读取 rough_reasons。"""
    sql = text(
        f"""
        SELECT
            credit_no,
            company_name,
            company_address
        FROM {ROUGH_TABLE}
        """
    )

    rows = []
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(sql)
        for db_row in result.mappings():
            company_address = normalize_value(db_row.get("company_address"))
            if not company_address:
                continue

            row = {
                "credit_no": normalize_value(db_row.get("credit_no")),
                "company_name": normalize_value(db_row.get("company_name")),
                "company_address": company_address,
            }
            for field_name in STRUCTURED_FIELDS:
                row[field_name] = ""
            row["api_error"] = ""
            row["run_status"] = STATUS_PENDING
            row["processed_status"] = PROCESSED_STATUS_PENDING
            rows.append(row)

    return rows


def load_existing_progress():
    """读取已有 progress 表，用来续跑。"""
    sql = text(
        f"""
        SELECT
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information,
            api_error,
            run_status,
            processed_status
        FROM {PROGRESS_TABLE}
        """
    )

    progress = {}
    engine = get_engine()
    with engine.connect() as connection:
        try:
            result = connection.execute(sql)
        except Exception:
            return progress

        for db_row in result.mappings():
            row = {
                "credit_no": normalize_value(db_row.get("credit_no")),
                "company_name": normalize_value(db_row.get("company_name")),
                "company_address": normalize_value(db_row.get("company_address")),
                "province": normalize_value(db_row.get("province")),
                "city": normalize_value(db_row.get("city")),
                "district": normalize_value(db_row.get("district")),
                "town_street": normalize_value(db_row.get("town_street")),
                "road_name": normalize_value(db_row.get("road_name")),
                "road_no": normalize_value(db_row.get("road_no")),
                "entity_name": normalize_value(db_row.get("entity_name")),
                "additional_information": normalize_value(db_row.get("additional_information")),
                "api_error": normalize_value(db_row.get("api_error")),
                "run_status": normalize_value(db_row.get("run_status")) or STATUS_PENDING,
                "processed_status": normalize_value(db_row.get("processed_status")) or PROCESSED_STATUS_PENDING,
            }
            progress[make_row_key(row)] = row

    return progress


def merge_existing_progress(rows, progress):
    """把历史成功/失败状态合并到本次 rough 源数据里。"""
    for row in rows:
        old_row = progress.get(make_row_key(row))
        if not old_row:
            continue

        for field_name in STRUCTURED_FIELDS:
            row[field_name] = old_row.get(field_name, "")
        row["api_error"] = old_row.get("api_error", "")
        row["run_status"] = old_row.get("run_status", STATUS_PENDING) or STATUS_PENDING
        row["processed_status"] = old_row.get("processed_status", PROCESSED_STATUS_PENDING) or PROCESSED_STATUS_PENDING


@retry_db_operation
def replace_rows_in_progress(rows):
    """写入 progress（UNIQUE KEY 表自动按主键去重，无需 DELETE）。"""
    if not rows:
        return 0

    insert_sql = text(
        f"""
        INSERT INTO {PROGRESS_TABLE} (
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information,
            api_error,
            run_status,
            processed_status
        ) VALUES (
            :credit_no,
            :company_name,
            :company_address,
            :province,
            :city,
            :district,
            :town_street,
            :road_name,
            :road_no,
            :entity_name,
            :additional_information,
            :api_error,
            :run_status,
            :processed_status
        )
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        for batch in chunk_rows(rows, INSERT_BATCH_SIZE):
            payload = [build_db_payload(row, PROGRESS_FIELDS) for row in batch]
            connection.execute(insert_sql, payload)

    return len(rows)


@retry_db_operation
def replace_rows_in_processed(rows):
    """写入 processed（UNIQUE KEY 表自动按主键去重，无需 DELETE）。"""
    if not rows:
        return 0

    insert_sql = text(
        f"""
        INSERT INTO {PROCESSED_TABLE} (
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information
        ) VALUES (
            :credit_no,
            :company_name,
            :company_address,
            :province,
            :city,
            :district,
            :town_street,
            :road_name,
            :road_no,
            :entity_name,
            :additional_information
        )
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        for batch in chunk_rows(rows, INSERT_BATCH_SIZE):
            payload = [build_db_payload(row, BASE_RESULT_FIELDS) for row in batch]
            connection.execute(insert_sql, payload)

    return len(rows)


def count_rows(table_name):
    """统计指定表当前行数。"""
    sql = text(f"SELECT COUNT(1) AS row_count FROM {table_name}")
    engine = get_engine()
    with engine.connect() as connection:
        row = connection.execute(sql).mappings().first()
        return int(row["row_count"]) if row else 0


@retry_db_operation
def ensure_processed_initialized():
    """首次运行时，先把 clean 表全量灌入 processed（带重试）。"""
    if count_rows(PROCESSED_TABLE) > 0:
        return False

    insert_sql = text(
        f"""
        INSERT INTO {PROCESSED_TABLE} (
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information
        )
        SELECT
            credit_no,
            company_name,
            company_address,
            province,
            city,
            district,
            town_street,
            road_name,
            road_no,
            entity_name,
            additional_information
        FROM {CLEAN_TABLE}
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(insert_sql)

    return True


def mark_row_dirty(row):
    """把一行加入脏集合，等待后续增量保存。"""
    if dirty_row_keys is None:
        return
    dirty_row_keys.add(make_row_key(row))


def get_dirty_rows():
    """取出自上次保存以来发生变化的行。"""
    if not dirty_row_keys or not current_rows_by_key:
        return []
    return [
        current_rows_by_key[row_key]
        for row_key in dirty_row_keys
        if row_key in current_rows_by_key
    ]


def get_rows_pending_processed(rows):
    """找出需要写入 processed 的成功行。"""
    result = []
    for row in rows:
        if row.get("run_status") == STATUS_SUCCESS and row.get("processed_status") != PROCESSED_STATUS_DONE:
            result.append(row)
    return result


def flush_incremental_state():
    """把脏行按 key 增量刷新到 processed 和 progress。"""
    global processed_appended_count

    dirty_rows = get_dirty_rows()
    if not dirty_rows:
        return 0, 0

    flush_start_time = time.time()
    processed_rows = get_rows_pending_processed(dirty_rows)
    progress_rows = dirty_rows

    print(f"开始增量保存: dirty_rows={len(dirty_rows)}, processed_rows={len(processed_rows)}")

    processed_written = 0
    if processed_rows:
        processed_written = replace_rows_in_processed(processed_rows)
        for row in processed_rows:
            row["processed_status"] = PROCESSED_STATUS_DONE
        processed_appended_count += processed_written

    progress_written = replace_rows_in_progress(progress_rows)
    dirty_row_keys.clear()

    flush_elapsed = time.time() - flush_start_time
    print(
        f"✓ 增量保存完成: progress={progress_written}, "
        f"processed={processed_written}, 耗时={flush_elapsed:.2f} 秒"
    )

    return progress_written, processed_written


def is_row_successful(row):
    """判断一行是否已经成功跑过模型。"""
    return row.get("run_status") == STATUS_SUCCESS


def save_progress_snapshot():
    """在中途中断或阶段性保存时，把脏行增量刷回数据库。"""
    if current_rows is None:
        return
    flush_incremental_state()
    print(f"✓ 已保存增量进度到 {PROGRESS_TABLE}")


def signal_handler(signum, frame):
    """收到中断信号时先保存 progress，再退出。"""
    print("\n收到中断信号，正在保存当前进度...")
    try:
        save_progress_snapshot()
        print("✓ 进度已保存，可以安全退出")
    except Exception as error:
        print(f"✗ 保存进度失败: {error}")
    sys.exit(0)


# ------------------------------------------------------------
# 核心逻辑（异步版本）
# ------------------------------------------------------------

async def process_unique_address(address, rows_group, client, semaphore):
    """对一个唯一地址调一次 LLM，结果扇出到所有同地址行。"""
    async with semaphore:
        representative_row = rows_group[0]
        try:
            rebuilt_row = await rebuild_row_async(representative_row, client)
            for row in rows_group:
                for field_name in STRUCTURED_FIELDS:
                    row[field_name] = rebuilt_row[field_name]
                row["api_error"] = ""
                row["run_status"] = STATUS_SUCCESS
                mark_row_dirty(row)
        except Exception as error:
            for row in rows_group:
                for field_name in STRUCTURED_FIELDS:
                    row[field_name] = ""
                row["api_error"] = truncate_text(str(error), 1800)
                row["run_status"] = STATUS_FAILED
                row["processed_status"] = PROCESSED_STATUS_PENDING
                mark_row_dirty(row)
            print(f"  ❌ 处理失败 [{address}] ({len(rows_group)}行): {error}")


async def process_pending_rows_async(pending_rows, rows, save_interval):
    """异步并发处理待处理的行（按地址去重，每个唯一地址只调一次 LLM）。"""
    # 按 company_address 分组
    address_groups = {}
    for row in pending_rows:
        addr = row.get("company_address", "")
        if addr not in address_groups:
            address_groups[addr] = []
        address_groups[addr].append(row)

    unique_addresses = list(address_groups.keys())
    unique_count = len(unique_addresses)

    print(
        f"地址去重: 待处理行={len(pending_rows)}, "
        f"唯一地址={unique_count}, "
        f"节省 API 调用={len(pending_rows) - unique_count}"
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=CONCURRENCY + 10,
            max_keepalive_connections=CONCURRENCY,
        )
    ) as client:
        for batch_start in range(0, unique_count, save_interval):
            batch_end = min(batch_start + save_interval, unique_count)
            batch_addrs = unique_addresses[batch_start:batch_end]
            batch_row_count = sum(len(address_groups[a]) for a in batch_addrs)

            batch_t0 = time.time()
            print(
                f"\n▶ 批次 {batch_start + 1}-{batch_end}/{unique_count} "
                f"(唯一地址={len(batch_addrs)}, 覆盖行数={batch_row_count}, 并发={CONCURRENCY})"
            )

            tasks = [
                process_unique_address(addr, address_groups[addr], client, semaphore)
                for addr in batch_addrs
            ]
            await asyncio.gather(*tasks)

            batch_elapsed = time.time() - batch_t0
            success_rows = sum(
                1 for a in batch_addrs
                for r in address_groups[a]
                if r.get("run_status") == STATUS_SUCCESS
            )
            print(
                f"✓ 批次完成: 成功行={success_rows}/{batch_row_count}, "
                f"API调用={len(batch_addrs)}, "
                f"耗时={batch_elapsed:.1f}s, "
                f"总进度={batch_end}/{unique_count}"
            )
            await asyncio.to_thread(save_progress_snapshot)

    return unique_count


async def main_async():
    """异步主程序入口。"""
    global current_rows, current_rows_by_key, dirty_row_keys, processed_appended_count

    start_time = time.time()
    processed_appended_count = 0

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    migrate_tables_to_unique_key()
    create_tables()
    ensure_processed_initialized()

    rows = load_rough_rows()
    progress = load_existing_progress()
    merge_existing_progress(rows, progress)
    current_rows = rows
    current_rows_by_key = {make_row_key(row): row for row in rows}
    dirty_row_keys = set()

    pending_rows = []
    success_skip_count = 0

    for row in rows:
        if is_row_successful(row):
            success_skip_count += 1
            if row.get("processed_status") != PROCESSED_STATUS_DONE:
                mark_row_dirty(row)
        else:
            pending_rows.append(row)

    total_count = len(rows)
    pending_count = len(pending_rows)

    print(f"rough 源表: {ROUGH_TABLE}")
    print(f"总记录数: {total_count}")
    print(f"已成功跳过: {success_skip_count}")
    print(f"待处理行数: {pending_count}")
    print(f"模型: {MODEL}")
    print(f"并发数: {CONCURRENCY}")
    print(f"保存间隔: 每 {SAVE_INTERVAL} 个唯一地址")
    print("开始处理 rough 重构...\n")

    api_calls = await process_pending_rows_async(pending_rows, rows, SAVE_INTERVAL)

    save_progress_snapshot()
    appended_count = processed_appended_count

    success_count = sum(1 for row in rows if row.get("run_status") == STATUS_SUCCESS)
    failed_count = sum(1 for row in rows if row.get("run_status") == STATUS_FAILED)
    processed_count = count_rows(PROCESSED_TABLE)

    print("\n" + "=" * 60)
    print("处理完成")
    print(f"progress 表: {PROGRESS_TABLE}")
    print(f"processed 表: {PROCESSED_TABLE}")
    print(f"本次实际 API 调用（去重后）: {api_calls}")
    print(f"本次覆盖行数: {pending_count}")
    print(f"累计 success: {success_count}")
    print(f"累计 failed: {failed_count}")
    print(f"本次追加到 processed: {appended_count}")
    print(f"processed 当前总行数: {processed_count}")
    print("=" * 60)

    end_time = time.time()
    print(f"运行时间: {end_time - start_time:.4f} 秒")


def main():
    """同步包装器，调用异步主函数。"""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
