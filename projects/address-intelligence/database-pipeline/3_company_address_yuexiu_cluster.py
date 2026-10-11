import math
import os
import pickle
import re
import time
from pathlib import Path

import numpy as np
import requests
from sqlalchemy import create_engine, text


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

# 数据库连接和 embedding 接口 key 都只从环境变量读取。
DB_TARGET = os.environ['SR_TARGET_DSN']
API_URL = os.environ['EMBEDDING_API_URL']
API_KEY = os.environ['EMBEDDING_API_KEY']

# 输入表、输出表和本地缓存文件固定为当前任务指定值。
ROOT_PATH = Path(__file__).resolve().parent
CACHE_FILE = "location_embedding_cache_yuexiu_ly.pkl"
CACHE_PATH = ROOT_PATH / CACHE_FILE

INPUT_TABLE = "fintech_stat.company_address_yuexiu_processed_ly"
OUTPUT_TABLE = "fintech_stat.company_address_yuexiu_clustered_ly"

# embedding 模型配置直接沿用现有 Excel 版脚本。
MODEL = "Qwen3-Embedding-8B"
DIMENSIONS = None  # 使用模型默认维度
BATCH_SIZE = 64

# 聚类打分参数。
W_CORE = 0.5
W_ROAD_NO = 0.20
K_ROAD_NO = 150
THRESHOLD_LINK = 0.8
ENTITY_RESCUE_MARGIN = 0.05
ENTITY_SIM_THRESHOLD = 0.7

# 数据库读写需要的字段。
INPUT_FIELDS = [
    "credit_no",
    "company_name",
    "company_address",
    "province",
    "city",
    "district",
    "town_street",
    "road_name",
    "road_no",
    "entity_name",
    "additional_information",
]

OUTPUT_FIELDS = INPUT_FIELDS + ["location_label"]

# 聚类节点仍按这四个位置字段去重。
LOCATION_FIELDS = ["district", "road_name", "road_no", "entity_name"]

# 批量写库时每批提交的行数。
INSERT_BATCH_SIZE = 500


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def clean_text(value):
    """把数据库值统一成干净字符串，避免 None 影响后续拼接和比较。"""
    if value is None:
        return ""
    return str(value).strip()

def get_engine():
    """统一创建数据库连接。"""
    if not DB_TARGET:
        raise ValueError("缺少环境变量 DB_TARGET，无法建立数据库连接。")
    return create_engine(DB_TARGET)


def load_cache(path):
    """读取本地 embedding 缓存，模型版本不一致时直接丢弃。"""
    if not path.exists():
        return {}

    with path.open("rb") as file:
        data = pickle.load(file)

    if not isinstance(data, dict):
        return {}

    if data.get("model") != MODEL:
        return {}

    items = data.get("items", {})
    return items if isinstance(items, dict) else {}


def save_cache(path, cache):
    """把当前 embedding 缓存回写到本地文件。"""
    with path.open("wb") as file:
        pickle.dump(
            {"model": MODEL, "items": cache},
            file,
            protocol=pickle.HIGHEST_PROTOCOL,
        )


def get_embedding_single(text):
    """获取单个文本的 embedding"""
    url = API_URL
    
    payload = {
        "model": MODEL,
        "input": text
    }
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    
    data = response.json()
    return data["data"][0]["embedding"]


def fetch_embeddings_batch(texts):
    """批量获取 embeddings，支持分批处理"""
    cache = load_cache(CACHE_PATH)
    
    # 去重并过滤空文本
    unique_texts = []
    seen = set()
    for text in texts:
        cleaned = clean_text(text)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique_texts.append(cleaned)
    
    # 分离已缓存和未缓存的文本
    cached_embeddings = {}
    missing_texts = []
    
    for text in unique_texts:
        if text in cache:
            cached_embeddings[text] = cache[text]
        else:
            missing_texts.append(text)
    
    print(f"缓存命中: {len(cached_embeddings)} 条，需要请求: {len(missing_texts)} 条")
    
    # 批量请求缺失的 embeddings
    if missing_texts:
        total_batches = math.ceil(len(missing_texts) / BATCH_SIZE)
        
        for i in range(0, len(missing_texts), BATCH_SIZE):
            batch = missing_texts[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            print(f"请求 embedding 批次 {batch_num}/{total_batches}，数量: {len(batch)}")
            
            url = API_URL
            payload = {
                "model": MODEL,
                "input": batch
            }
            
            headers = {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=300)
            response.raise_for_status()
            
            data = response.json()
            embeddings_data = data.get("data", [])
            
            if len(embeddings_data) != len(batch):
                raise ValueError(f"embedding 返回数量异常: 期望 {len(batch)}，实际 {len(embeddings_data)}")
            
            # 按 index 排序确保顺序正确
            sorted_embeddings = sorted(embeddings_data, key=lambda x: x["index"])
            
            # 保存到缓存
            for text, emb_data in zip(batch, sorted_embeddings):
                embedding = emb_data["embedding"]
                cache[text] = embedding
                cached_embeddings[text] = embedding
            
            # 每批处理后保存缓存
            save_cache(CACHE_PATH, cache)
            print(f"批次 {batch_num} 完成，已保存缓存")
    
    print(f"embedding 获取完成，共 {len(cached_embeddings)} 条")
    return cached_embeddings


def fetch_embeddings(values):
    """获取 embeddings 的主函数，兼容原有调用方式"""
    # 过滤空值
    valid_values = [v for v in values if v and clean_text(v)]
    if not valid_values:
        return {}
    
    # 使用批量获取方法
    return fetch_embeddings_batch(valid_values)


def parse_road_no(value):
    """从 road_no 中抽取全部数字，多个数字时取平均值。"""
    nums = [int(item) for item in re.findall(r"\d+", clean_text(value))]
    if not nums:
        return None
    return sum(nums) / len(nums)


def road_base_name(road_name):
    """提取路名基干，用于判断两个 road_name 是否属于同一条路。"""
    name = clean_text(road_name)
    if not name:
        return ""

    name = re.sub(r"[一二三四五六七八九十百\d]+(?=(?:马)?路|街|(?:大)?道|巷)", "", name)
    name = re.sub(r"[东西南北中](?=(?:马)?路|街|(?:大)?道|巷)", "", name)
    return name


def build_sim_cache(values, embeddings):
    """对唯一文本值预计算余弦相似度矩阵。"""
    items = [item for item in sorted(set(values)) if item]
    if not items:
        return {}, np.zeros((0, 0), dtype=np.float32)

    matrix = np.asarray([embeddings[item] for item in items], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    return {item: index for index, item in enumerate(items)}, matrix @ matrix.T


def build_road_no_cache(values, k):
    """对唯一门牌数值预计算距离分矩阵。"""
    items = sorted(set(value for value in values if value is not None))
    if not items:
        return {}, np.zeros((0, 0), dtype=np.float32)

    numbers = np.asarray(items, dtype=np.float32)
    scores = 1.0 / (1.0 + np.abs(numbers[:, None] - numbers[None, :]) / k)
    return {item: index for index, item in enumerate(items)}, scores


def total_score(left_node, right_node, caches):
    """计算基础位置分，只看路名和门牌号。"""
    left_has_road = bool(left_node["road_name"])
    right_has_road = bool(right_node["road_name"])

    if left_has_road and right_has_road:
        if road_base_name(left_node["road_name"]) != road_base_name(right_node["road_name"]):
            return 0.0

    core_sim = (
        float(caches["core_scores"][left_node["core_id"], right_node["core_id"]])
        if left_node["core_id"] is not None and right_node["core_id"] is not None
        else None
    )
    if core_sim is None:
        return 0.0

    road_no_score = None
    if (
        left_has_road
        and right_has_road
        and road_base_name(left_node["road_name"]) == road_base_name(right_node["road_name"])
        and left_node["road_no_id"] is not None
        and right_node["road_no_id"] is not None
    ):
        road_no_score = float(caches["road_scores"][left_node["road_no_id"], right_node["road_no_id"]])

    if road_no_score is not None:
        return (W_CORE * core_sim + W_ROAD_NO * road_no_score) / (W_CORE + W_ROAD_NO)
    return core_sim


def can_link(left_node, right_node, score, caches):
    """基础分达阈值直接连边，否则仅允许 entity 补救。"""
    if score >= THRESHOLD_LINK:
        return True

    if (
        score >= THRESHOLD_LINK - ENTITY_RESCUE_MARGIN
        and left_node["entity_name"]
        and right_node["entity_name"]
        and left_node["poi_id"] is not None
        and right_node["poi_id"] is not None
    ):
        poi_sim = float(caches["poi_scores"][left_node["poi_id"], right_node["poi_id"]])
        if poi_sim >= ENTITY_SIM_THRESHOLD:
            return True

    return False


def union_find(count):
    """并查集，用于把连边后的节点合并成簇。"""
    parent = list(range(count))
    rank = [0] * count

    def find(node_id):
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left_id, right_id):
        root_left = find(left_id)
        root_right = find(right_id)
        if root_left == root_right:
            return False

        if rank[root_left] < rank[root_right]:
            parent[root_left] = root_right
        elif rank[root_left] > rank[root_right]:
            parent[root_right] = root_left
        else:
            parent[root_right] = root_left
            rank[root_left] += 1
        return True

    return find, union


def chunk_rows(rows, chunk_size):
    """把大列表切成小批次，便于分批写库。"""
    for index in range(0, len(rows), chunk_size):
        yield rows[index:index + chunk_size]


class SimpleProgressBar:
    """轻量级命令行进度条，避免额外引入第三方依赖。"""

    def __init__(self, total, title, width=30, min_interval=0.2):
        self.total = max(int(total), 0)
        self.title = title
        self.width = width
        self.min_interval = min_interval
        self.current = 0
        self.postfix = ""
        self.start_time = time.time()
        self.last_render_time = 0.0

    def render(self, force=False):
        now = time.time()
        if not force and now - self.last_render_time < self.min_interval:
            return

        total = self.total if self.total > 0 else 1
        ratio = min(self.current / total, 1.0)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = now - self.start_time

        line = (
            f"\r{self.title}: [{bar}] "
            f"{self.current}/{self.total} ({ratio * 100:5.1f}%) "
            f"耗时 {elapsed:6.1f}s"
        )
        if self.postfix:
            line += f" | {self.postfix}"

        print(line, end="", flush=True)
        self.last_render_time = now

    def update(self, increment=1, postfix=None):
        self.current += increment
        if postfix is not None:
            self.postfix = postfix
        self.render()

    def finish(self):
        self.current = self.total
        self.render(force=True)
        print()


def load_rows():
    """从 processed 表读取全量结构化结果。"""
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
            additional_information
        FROM {INPUT_TABLE}
        """
    )

    rows = []
    engine = get_engine()
    with engine.connect() as connection:
        result = connection.execute(sql)
        for db_row in result.mappings():
            rows.append({field_name: clean_text(db_row.get(field_name)) for field_name in INPUT_FIELDS})

    return rows


def build_nodes(rows):
    """按四个位置字段去重成节点，避免重复行重复参与聚类。"""
    nodes = []
    node_map = {}

    for row_id, row in enumerate(rows):
        key = tuple(clean_text(row.get(field_name)) for field_name in LOCATION_FIELDS)
        node_id = node_map.get(key)

        if node_id is None:
            node_id = len(nodes)
            node_map[key] = node_id

            node = dict(zip(LOCATION_FIELDS, key))
            node["row_ids"] = []
            node["count"] = 0
            node["core_text"] = node["road_name"] if node["road_name"] else ""
            node["poi_text"] = node["entity_name"]
            node["road_no_value"] = parse_road_no(node["road_no"])
            nodes.append(node)

        node = nodes[node_id]
        node["row_ids"].append(row_id)
        node["count"] += 1

    return nodes


def create_output_table():
    """创建 clustered 结果表。"""
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {OUTPUT_TABLE} (
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
        location_label VARCHAR(255) COMMENT '位置标签'
    ) ENGINE=OLAP
    DUPLICATE KEY(credit_no, company_name, company_address)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(create_sql))


def truncate_output_table():
    """每次运行前先清空 clustered 表，保证结果只反映本次全量输入。"""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {OUTPUT_TABLE}"))


def insert_rows(rows):
    """把带 location_label 的结果分批写入 clustered 表。"""
    if not rows:
        return

    insert_sql = text(
        f"""
        INSERT INTO {OUTPUT_TABLE} (
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
            location_label
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
            :location_label
        )
        """
    )

    engine = get_engine()
    with engine.begin() as connection:
        for batch in chunk_rows(rows, INSERT_BATCH_SIZE):
            connection.execute(insert_sql, batch)


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------

def run():
    """完整执行一次 processed -> clustered 的位置聚类。"""
    print(f"开始读取输入表：{INPUT_TABLE}")
    rows = load_rows()
    if not rows:
        raise ValueError("输入表为空")

    nodes = build_nodes(rows)
    print(f"位置节点去重完成，共 {len(nodes)} 个")

    print(f"开始调用在线模型：{MODEL}")
    core_embeddings = fetch_embeddings([node["core_text"] for node in nodes])
    poi_embeddings = fetch_embeddings([node["poi_text"] for node in nodes if node["poi_text"]])

    core_index, core_scores = build_sim_cache([node["core_text"] for node in nodes], core_embeddings)
    poi_index, poi_scores = build_sim_cache([node["poi_text"] for node in nodes if node["poi_text"]], poi_embeddings)
    road_index, road_scores = build_road_no_cache([node["road_no_value"] for node in nodes], K_ROAD_NO)

    for node in nodes:
        node["core_id"] = core_index.get(node["core_text"])
        node["poi_id"] = poi_index.get(node["poi_text"]) if node["poi_text"] else None
        node["road_no_id"] = road_index.get(node["road_no_value"])

    print("embedding 生成完成")
    print("开始聚类 ...")

    find, union = union_find(len(nodes))
    linked = [[] for _ in nodes]
    caches = {
        "core_scores": core_scores,
        "poi_scores": poi_scores,
        "road_scores": road_scores,
    }

    # 当前算法只在同一 district 内做节点两两比较，这一点保持不变。
    district_groups = {}
    for node_id, node in enumerate(nodes):
        district_groups.setdefault(node["district"], []).append(node_id)

    total_pair_count = sum(len(node_ids) * (len(node_ids) - 1) // 2 for node_ids in district_groups.values())
    print(
        f"开始聚类比较：共 {len(district_groups)} 个 district 分组，"
        f"需要比较 {total_pair_count} 对节点"
    )
    progress_bar = SimpleProgressBar(total_pair_count, "聚类进度")

    for district, node_ids in district_groups.items():
        print(f"处理中：district={district or '(空)'}，节点数={len(node_ids)}")
        for offset, left_id in enumerate(node_ids):
            compared_count = len(node_ids) - offset - 1
            for right_id in node_ids[offset + 1:]:
                score = total_score(nodes[left_id], nodes[right_id], caches)
                if can_link(nodes[left_id], nodes[right_id], score, caches):
                    union(left_id, right_id)
                    linked[left_id].append((right_id, score))
                    linked[right_id].append((left_id, score))
            if compared_count > 0:
                progress_bar.update(
                    compared_count,
                    postfix=f"district={district or '(空)'} 节点数={len(node_ids)}",
                )

    progress_bar.finish()

    cluster_map = {}
    for node_id in range(len(nodes)):
        cluster_map.setdefault(find(node_id), []).append(node_id)

    ordered_clusters = sorted(cluster_map.values(), key=lambda members: (-len(members), min(members)))
    node_labels = {}

    for cluster_no, members in enumerate(ordered_clusters, 1):
        member_set = set(members)

        # 仍保留"选代表节点"的逻辑。
        # 虽然这版不输出 cluster 摘要，但这能保持 label 分配和原脚本行为一致。
        representative = max(
            members,
            key=lambda node_id: (
                len([other_id for other_id, _ in linked[node_id] if other_id in member_set]),
                sum(score for other_id, score in linked[node_id] if other_id in member_set),
                bool(nodes[node_id]["entity_name"]),
                -node_id,
            ),
        )
        _ = representative

        label = f"LOC_{cluster_no:06d}"
        for node_id in members:
            node_labels[node_id] = label

    output_rows = []
    for node_id, node in enumerate(nodes):
        for row_id in node["row_ids"]:
            output_row = dict(rows[row_id])
            output_row["location_label"] = node_labels[node_id]
            output_rows.append(output_row)

    print(f"开始写出结果表：{OUTPUT_TABLE}")
    create_output_table()
    truncate_output_table()
    insert_rows(output_rows)

    print("处理完成")
    print(f"输入行数: {len(rows)}")
    print(f"节点数: {len(nodes)}")
    print(f"cluster 数: {len(ordered_clusters)}")
    print(f"输出行数: {len(output_rows)}")


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    """程序入口。"""
    run()


if __name__ == "__main__":
    main()
