import time
import os
import pickle
import math
import hashlib

import numpy as np
import pandas as pd
import requests
from scipy.spatial import cKDTree

"""
方法二：地址经纬度聚类商圈脚本（优化版 - 使用 cKDTree 半径查询，确保连接完整）
"""
#------------------------------------------1.参数-------------------------------------------
# 高德地图 API 配置
AMAP_KEY = os.environ['AMAP_API_KEY']
INPUT_FILE = "广州商圈_20260227_cleaned.xlsx"
OUTPUT_FILE = "客户_药+狮_方法二.xlsx"
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
GEOCODE_SLEEP_SECONDS = 0.6

# 聚类参数
CLUSTER_DISTANCE_THRESHOLD = 3000  # 单位：米
MIN_CLUSTER_SIZE = 1000            # 最小簇大小

# 平面距离计算参数（针对广州纬度）
LAT_TO_METER = 111000  # 1度纬度 ≈ 111,000 米
LON_TO_METER = 102000  # 1度经度 ≈ 102,000 米（针对广州地区）

# 缓存文件
GEOCODE_CACHE_FILE = "geocode_cache.pkl"

# 内存优化配置
KD_TREE_LEAF_SIZE = 40
BATCH_SIZE = 1000  # 批处理大小

#------------------------------------------2.工具函数-------------------------------------------
class GeocodeError(RuntimeError):
    pass


class UnionFind:
    """并查集（路径压缩 + 按大小合并）"""
    def __init__(self, n):
        self.parent = np.arange(n, dtype=np.int32)
        self.size = np.ones(n, dtype=np.int32)

    def find(self, x):
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a, b):
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True

    def component_roots(self):
        roots = np.empty(len(self.parent), dtype=np.int32)
        for i in range(len(self.parent)):
            roots[i] = self.find(i)
        return roots


def diagnose_neighbor_density(coords, distance_threshold):
    """诊断当前阈值下的邻居密度，提前判断是否会爆内存"""
    meters_coords = convert_to_meters(coords)
    tree = cKDTree(meters_coords, leafsize=KD_TREE_LEAF_SIZE)
    counts = tree.query_ball_point(meters_coords, r=distance_threshold, return_length=True)
    est_unique_edges = int(((counts - 1).sum()) // 2)

    print("\n邻居密度诊断：")
    print(f"  点数: {len(coords)}")
    print(f"  邻居数统计(含自身): min={int(counts.min())}, P50={int(np.median(counts))}, P95={int(np.percentile(counts, 95))}, max={int(counts.max())}")
    print(f"  预计唯一边数约: {est_unique_edges:,}")
    if est_unique_edges > 20_000_000:
        print("  ⚠ 当前阈值下边数极大，不适合再用 Python set 存全部边；改用并查集流式合并。")

    return tree, meters_coords, counts, est_unique_edges


def plane_distance(lon1, lat1, lon2, lat2):
    """计算两点之间的平面距离（米）"""
    delta_lon = lon1 - lon2
    delta_lat = lat1 - lat2
    x = delta_lon * LON_TO_METER
    y = delta_lat * LAT_TO_METER
    return math.sqrt(x * x + y * y)


def convert_to_meters(coords):
    """将经纬度坐标转换为米制坐标（用于 KDTree）"""
    meters_coords = np.zeros_like(coords, dtype=np.float32)
    meters_coords[:, 0] = coords[:, 0] * LON_TO_METER
    meters_coords[:, 1] = coords[:, 1] * LAT_TO_METER
    return meters_coords


def load_geocode_cache():
    """加载地理编码缓存"""
    if os.path.exists(GEOCODE_CACHE_FILE):
        try:
            with open(GEOCODE_CACHE_FILE, 'rb') as f:
                cache = pickle.load(f)
            print(f"成功加载地理编码缓存，共 {len(cache)} 条记录")
            return cache
        except Exception as e:
            print(f"加载缓存失败: {e}，将重新进行地理编码")
            return {}
    return {}


def save_geocode_cache(cache):
    """保存地理编码缓存"""
    try:
        with open(GEOCODE_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
        print(f"地理编码缓存已保存，共 {len(cache)} 条记录")
    except Exception as e:
        print(f"保存缓存失败: {e}")

#------------------------------------------3.核心逻辑-------------------------------------------
def geocode_address(address, key=AMAP_KEY):
    """向高德请求地理编码"""
    try:
        response = requests.get(
            GEOCODE_URL,
            params={"address": address, "key": key, "output": "json"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise GeocodeError(f"高德地理编码请求失败: address={address}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise GeocodeError(f"高德地理编码返回了无法解析的响应: address={address}") from exc

    if payload.get("status") != "1":
        info = str(payload.get("info", "")).strip()
        infocode = str(payload.get("infocode", "")).strip()
        raise GeocodeError(
            f"高德地理编码接口返回失败: address={address}, info={info}, infocode={infocode}"
        )

    geocodes = payload.get("geocodes")
    if not geocodes:
        return None, None

    location = str(geocodes[0].get("location", "")).strip()
    if not location or "," not in location:
        return None, None

    lon_text, lat_text = location.split(",", 1)
    try:
        return float(lon_text), float(lat_text)
    except ValueError:
        return None, None


def cluster_points(df, distance_threshold=CLUSTER_DISTANCE_THRESHOLD,
                   min_cluster_size=MIN_CLUSTER_SIZE):
    """主聚类函数"""
    valid_mask = df["lon"].notna() & df["lat"].notna()
    valid_points = df.loc[valid_mask].copy()

    # 初始化列
    valid_points["cluster_id"] = pd.Series(pd.array([pd.NA] * len(valid_points), dtype="Int64"), index=valid_points.index)
    valid_points["cluster_company_count"] = pd.Series(
        pd.array([pd.NA] * len(valid_points), dtype="Int64"),
        index=valid_points.index,
    )
    valid_points["cluster_center_lon"] = np.nan
    valid_points["cluster_center_lat"] = np.nan
    valid_points["cluster_radius"] = np.nan

    # 提取坐标
    coordinates = valid_points[["lon", "lat"]].to_numpy(dtype=np.float64)

    # 检查并移除无效坐标
    finite_mask = np.isfinite(coordinates).all(axis=1)
    if not finite_mask.all():
        print(f"警告：发现 {np.sum(~finite_mask)} 个无效坐标点，已移除")
        coordinates = coordinates[finite_mask]
        valid_points = valid_points[finite_mask].copy()

    n_points = len(coordinates)
    print(f"有效坐标点数量: {n_points}")

    if n_points < 2:
        print(f"警告：有效坐标点数量({n_points})不足2个，跳过聚类")
        valid_points["cluster_id"] = -1
        summary_columns = [
            "cluster_id",
            "cluster_company_count",
            "cluster_center_lon",
            "cluster_center_lat",
            "cluster_radius",
        ]
        return valid_points, pd.DataFrame(columns=summary_columns)

    # 使用基于图的连通分量方法进行聚类
    labels = cluster_with_sparse_linkage(
        coordinates,
        distance_threshold,
        min_cluster_size
    )

    # 存储聚类标签
    valid_points["cluster_id"] = labels

    # 获取非噪声点
    clustered_points = valid_points.loc[valid_points["cluster_id"] != -1].copy()

    if clustered_points.empty:
        valid_points["cluster_id"] = valid_points["cluster_id"].astype("Int64")
        summary_columns = [
            "cluster_id",
            "cluster_company_count",
            "cluster_center_lon",
            "cluster_center_lat",
            "cluster_radius",
        ]
        return valid_points, pd.DataFrame(columns=summary_columns)

    # 计算每个簇的统计信息
    summary = (
        clustered_points.groupby("cluster_id", as_index=False)
        .agg(
            cluster_company_count=("company_name", "size"),
            cluster_center_lon=("lon", "mean"),
            cluster_center_lat=("lat", "mean"),
        )
    )

    # 计算簇半径
    radius_values = []
    for cluster_id in summary["cluster_id"]:
        members = clustered_points.loc[clustered_points["cluster_id"] == cluster_id]
        center = summary.loc[summary["cluster_id"] == cluster_id].iloc[0]

        max_radius = 0
        for _, row in members.iterrows():
            dist = plane_distance(
                center["cluster_center_lon"], center["cluster_center_lat"],
                row["lon"], row["lat"]
            )
            max_radius = max(max_radius, dist)

        radius_values.append(max_radius)

    summary["cluster_radius"] = radius_values

    # 回填统计信息
    for cluster_id in summary["cluster_id"]:
        mask = valid_points["cluster_id"] == cluster_id
        valid_points.loc[mask, "cluster_company_count"] = summary.loc[
            summary["cluster_id"] == cluster_id, "cluster_company_count"
        ].values[0]
        valid_points.loc[mask, "cluster_center_lon"] = summary.loc[
            summary["cluster_id"] == cluster_id, "cluster_center_lon"
        ].values[0]
        valid_points.loc[mask, "cluster_center_lat"] = summary.loc[
            summary["cluster_id"] == cluster_id, "cluster_center_lat"
        ].values[0]
        valid_points.loc[mask, "cluster_radius"] = summary.loc[
            summary["cluster_id"] == cluster_id, "cluster_radius"
        ].values[0]

    # 噪声点清空统计信息
    noise_mask = valid_points["cluster_id"] == -1
    valid_points.loc[noise_mask, "cluster_company_count"] = pd.NA
    valid_points.loc[noise_mask, "cluster_center_lon"] = np.nan
    valid_points.loc[noise_mask, "cluster_center_lat"] = np.nan
    valid_points.loc[noise_mask, "cluster_radius"] = np.nan

    valid_points["cluster_id"] = valid_points["cluster_id"].astype("Int64")

    return valid_points, summary


def cluster_with_sparse_linkage(coords, distance_threshold, min_cluster_size):
    """使用 cKDTree + 并查集进行单链聚类（不再把全部边存入内存）"""
    n = len(coords)

    print(f"\n使用基于图的连通分量方法进行聚类（阈值={distance_threshold}米）...")
    start_time = time.time()

    tree, meters_coords, counts, est_unique_edges = diagnose_neighbor_density(coords, distance_threshold)

    print("\n开始并查集合并（流式，不缓存全部边）...")
    uf = UnionFind(n)
    error_count = 0
    union_attempts = 0
    actual_merges = 0

    n_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(n_batches):
        start_idx = batch_idx * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, n)

        print(f"处理批次 {batch_idx+1}/{n_batches} (点 {start_idx}-{end_idx-1})...")

        for i in range(start_idx, end_idx):
            try:
                indices = tree.query_ball_point(meters_coords[i], r=distance_threshold)
                if len(indices) == 0:
                    continue

                for j in indices:
                    if j > i:
                        union_attempts += 1
                        if uf.union(i, j):
                            actual_merges += 1

            except Exception as e:
                error_count += 1
                print(f"处理点 {i} 时出错: {repr(e)}")
                continue

        print(f"  当前累计 union 尝试数: {union_attempts:,}，实际合并次数: {actual_merges:,}，累计报错点数: {error_count}")

    roots = uf.component_roots()
    _, inverse = np.unique(roots, return_inverse=True)
    labels = inverse.astype(np.int32)

    unique, component_counts = np.unique(labels, return_counts=True)

    print(f"\n找到 {len(unique)} 个连通分量:")
    for label, count in zip(unique, component_counts):
        print(f"  分量 {label}: {count} 个点")

    small_components = [label for label, count in zip(unique, component_counts) if count < min_cluster_size]
    if small_components:
        print(f"\n过滤条件: min_cluster_size = {min_cluster_size}")
        print(f"  将过滤 {len(small_components)} 个小分量")
        for label in small_components:
            labels[labels == label] = -1

    unique_labels = np.unique(labels)
    label_mapping = {}
    next_label = 0
    for label in unique_labels:
        if label != -1:
            label_mapping[label] = next_label
            next_label += 1
        else:
            label_mapping[label] = -1

    final_labels = np.array([label_mapping[label] for label in labels], dtype=np.int32)

    elapsed_time = time.time() - start_time
    print(f"\n聚类完成:")
    print(f"  - 预计唯一边数(诊断): {est_unique_edges:,}")
    print(f"  - union 尝试数: {union_attempts:,}")
    print(f"  - 实际合并次数: {actual_merges:,}")
    print(f"  - 最终簇数: {next_label}")
    print(f"  - 噪声点数: {np.sum(final_labels == -1)}")
    print(f"  - 总报错点数: {error_count}")
    print(f"  - 耗时: {elapsed_time:.2f} 秒")

    return final_labels

#------------------------------------------4.主处理函数-------------------------------------------
def process_workbook(input_path=INPUT_FILE, output_path=OUTPUT_FILE, use_cache=True):
    """主处理函数"""
    source_df = pd.read_excel(input_path)

    # 去重地址
    normalized_address = source_df["clean_address"].fillna("").astype(str).str.strip()
    unique_addresses = list(dict.fromkeys(address for address in normalized_address if address))

    # 加载缓存
    geocode_lookup = {}
    if use_cache:
        geocode_lookup = load_geocode_cache()
        uncached_addresses = [addr for addr in unique_addresses if addr not in geocode_lookup]
        print(f"已缓存 {len(geocode_lookup)} 个地址，需要新编码 {len(uncached_addresses)} 个地址")
    else:
        uncached_addresses = unique_addresses
        print(f"未使用缓存，将对 {len(uncached_addresses)} 个地址进行编码")

    # 地理编码
    for index, address in enumerate(uncached_addresses, start=1):
        print(f"地理编码 {index}/{len(uncached_addresses)}: {address[:50]}...")
        try:
            geocode_lookup[address] = geocode_address(address)
        except GeocodeError as e:
            print(f"警告：地址解析跳过 -> {e}")
            geocode_lookup[address] = (None, None)

        if GEOCODE_SLEEP_SECONDS > 0:
            time.sleep(GEOCODE_SLEEP_SECONDS)

        if index % 100 == 0 and use_cache:
            save_geocode_cache(geocode_lookup)

    if use_cache:
        save_geocode_cache(geocode_lookup)

    # 映射坐标
    with_coordinates = source_df.copy()
    coordinates = normalized_address.map(lambda address: geocode_lookup.get(address, (None, None)))
    with_coordinates["lon"] = pd.to_numeric(coordinates.map(lambda value: value[0]), errors="coerce")
    with_coordinates["lat"] = pd.to_numeric(coordinates.map(lambda value: value[1]), errors="coerce")

    # 聚类
    print("\n开始空间聚类...")
    clustered_valid_points, summary_df = cluster_points(with_coordinates)

    # 构建最终结果表
    coordinate_columns = ["lon", "lat"]
    cluster_columns = [
        "cluster_id",
        "cluster_company_count",
        "cluster_center_lon",
        "cluster_center_lat",
        "cluster_radius",
    ]
    business_columns = [col for col in with_coordinates.columns if col not in coordinate_columns + cluster_columns]
    full_columns = business_columns + coordinate_columns + cluster_columns

    result_df = with_coordinates.copy()
    for col in cluster_columns:
        if col not in result_df.columns:
            result_df[col] = np.nan

    if not clustered_valid_points.empty:
        result_df.loc[clustered_valid_points.index, cluster_columns] = clustered_valid_points[cluster_columns]

    # 分类输出
    failed_mask = result_df["lon"].isna() | result_df["lat"].isna()
    clustered_mask = (~failed_mask) & (result_df["cluster_id"] != -1) & (result_df["cluster_id"].notna())
    noise_mask = (~failed_mask) & ~clustered_mask

    clustered_result = result_df.loc[clustered_mask, full_columns].copy()
    noise_result = result_df.loc[noise_mask, full_columns].copy()
    failed_result = result_df.loc[failed_mask, business_columns + coordinate_columns].copy()

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        clustered_result.to_excel(writer, sheet_name="聚类结果", index=False)
        noise_result.to_excel(writer, sheet_name="噪声点", index=False)
        failed_result.to_excel(writer, sheet_name="编码失败", index=False)
        summary_df.to_excel(writer, sheet_name="簇汇总", index=False)

    print(f"\n完成！输出文件: {output_path}")
    print(f"统计信息:")
    print(f"  - 聚类结果: {len(clustered_result)} 家企业")
    print(f"  - 噪声点: {len(noise_result)} 家企业")
    print(f"  - 编码失败: {len(failed_result)} 家企业")
    print(f"  - 簇数量: {len(summary_df)}")


if __name__ == "__main__":
    process_workbook(use_cache=True)