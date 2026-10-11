import math
import os
from itertools import combinations
from pathlib import Path

import requests
from requests import HTTPError
from openpyxl import Workbook, load_workbook


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

INPUT_FILE_NAME = "clean_output.xlsx"
INPUT_PATH = Path(__file__).with_name(INPUT_FILE_NAME)

OUTPUT_FILE_NAME = "entity_name_similarity.xlsx"
OUTPUT_PATH = Path(__file__).with_name(OUTPUT_FILE_NAME)

API_KEY = os.environ['EMBEDDING_API_KEY']
API_KEY_ENV_NAME = "DASHSCOPE_API_KEY"
API_URL = os.environ['EMBEDDING_API_URL']
MODEL_NAME = "text-embedding-v4"
BATCH_SIZE = 10


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def normalize_value(value):
    if value is None:
        return ""
    return str(value).strip()


def load_unique_entity_names():
    workbook = load_workbook(INPUT_PATH, read_only=True, data_only=True)
    worksheet = workbook[workbook.sheetnames[0]]
    rows = worksheet.iter_rows(values_only=True)
    header = list(next(rows, []) or [])
    if "entity_name" not in header:
        raise ValueError("clean_output.xlsx 中找不到 entity_name 列")

    index = header.index("entity_name")
    seen = set()
    names = []
    for row in rows:
        name = normalize_value(row[index] if index < len(row) else "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    workbook.close()
    if len(names) < 2:
        raise ValueError("去重后的 entity_name 少于 2 个，无法做两两相似度")
    return names


def build_similarity_rows(names, matrix):
    rows = []
    for i, j in combinations(range(len(names)), 2):
        score = float(matrix[i][j])
        rows.append((names[i], names[j], score))
        rows.append((names[j], names[i], score))
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def write_rows(rows):
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("similarity")
    worksheet.append(["entity_name_1", "entity_name_2", "cosine_similarity"])
    for row in rows:
        worksheet.append(row)
    workbook.save(OUTPUT_PATH)


def get_api_key():
    api_key = API_KEY.strip() or os.getenv(API_KEY_ENV_NAME, "").strip()
    if not api_key:
        raise ValueError(
            f"缺少 API key。请先设置环境变量 {API_KEY_ENV_NAME}，或在脚本顶部的 API_KEY 中填入。"
        )
    return api_key


def get_embeddings(names):
    headers = {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}
    embeddings = []
    total = math.ceil(len(names) / BATCH_SIZE)
    for i in range(0, len(names), BATCH_SIZE):
        batch_no = i // BATCH_SIZE + 1
        print(f"开始请求 embedding，第 {batch_no}/{total} 批 ...")
        response = requests.post(
            API_URL,
            headers=headers,
            json={"model": MODEL_NAME, "input": names[i : i + BATCH_SIZE]},
            timeout=300,
        )
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise ValueError(f"embedding 请求失败：HTTP {response.status_code}，响应内容：{response.text}") from exc
        data = response.json().get("data", [])
        if len(data) != len(names[i : i + BATCH_SIZE]):
            raise ValueError("embedding 返回数量异常")
        embeddings.extend(item["embedding"] for item in sorted(data, key=lambda item: item["index"]))
    return embeddings


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_similarity_matrix(embeddings):
    size = len(embeddings)
    matrix = [[0.0] * size for _ in range(size)]
    for i in range(size):
        matrix[i][i] = 1.0
        for j in range(i + 1, size):
            score = cosine_similarity(embeddings[i], embeddings[j])
            matrix[i][j] = score
            matrix[j][i] = score
    return matrix


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------

def run():
    print("开始读取 clean_output.xlsx ...")
    names = load_unique_entity_names()
    print(f"entity_name 去重完成，共 {len(names)} 条")

    print(f"开始调用在线模型：{MODEL_NAME}")
    embeddings = get_embeddings(names)
    print("embedding 生成完成")

    print("开始计算两两余弦相似度 ...")
    rows = build_similarity_rows(names, build_similarity_matrix(embeddings))
    print(f"相似度计算完成，共 {len(rows)} 对")

    print(f"开始写出结果：{OUTPUT_FILE_NAME}")
    write_rows(rows)
    print("处理完成")


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    run()


if __name__ == "__main__":
    main()
