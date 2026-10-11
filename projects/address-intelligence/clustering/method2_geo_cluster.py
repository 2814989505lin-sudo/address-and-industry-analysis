import os

import time

import numpy as np
import pandas as pd
import requests
from sklearn.cluster import HDBSCAN


"""
方法二：地址经纬度聚类商圈脚本

这个脚本的目标非常直接：
1. 读取测试_cleaned.xlsx里的企业名称和清洗后的地址 clean_address
2. 调用高德正向地理编码接口，把每个唯一地址转成经纬度
3. 把经纬度回填到每一条企业记录
4. 使用 HDBSCAN 对企业点位做空间聚类
5. 输出四个 sheet：
   - 聚类结果：成功进入某个簇的企业
   - 噪声点：有坐标，但 HDBSCAN 没有把它归入任何簇的企业
   - 编码失败：高德没有返回坐标的企业
   - 簇汇总：每个簇的统计信息

输出里保留 cluster_id 和簇统计量，不再额外给簇命名字母标签。
"""


# 这里沿用仓库里现有的高德 Key 来源方式，不在本次改成环境变量。
AMAP_KEY = os.environ['AMAP_API_KEY']
INPUT_FILE = "广州商圈_20260227_cleaned.xlsx"
OUTPUT_FILE = "狮岭_方法二_0323.xlsx"
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
GEOCODE_SLEEP_SECONDS = 0.6
MIN_CLUSTER_SIZE = 150
MIN_SAMPLES = 80


class GeocodeError(RuntimeError):
    """高德地理编码调用失败时抛出的异常。"""


def geocode_address(address, key=AMAP_KEY):
    # geocode_address 只负责一件事：
    # 给一条清洗后的地址，向高德请求一次，最后返回 (lon, lat)。
    #
    # 这里故意把“系统异常”和“业务无结果”区分开：
    # - 系统异常：例如网络错误、HTTP 错误、接口返回失败，这些要直接抛异常
    # - 业务无结果：例如接口成功，但这个地址没有匹配到 geocodes，这时返回 (None, None)

    try:
        # 方法二的第一步是真实调用高德正向地理编码接口，把地址转为经纬度。
        response = requests.get(
            GEOCODE_URL,
            params={"address": address, "key": key, "output": "json"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # 请求链路异常属于系统问题，必须直接失败，不能伪装成“地址编码失败”。
        raise GeocodeError(f"高德地理编码请求失败: address={address}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        # 高德返回非 JSON 响应时，继续写结果会污染输出，因此直接中断。
        raise GeocodeError(f"高德地理编码返回了无法解析的响应: address={address}") from exc

    if payload.get("status") != "1":
        info = str(payload.get("info", "")).strip()
        infocode = str(payload.get("infocode", "")).strip()

        # 只要高德明确告诉我们这次接口调用失败，就不要继续往下取 geocodes。
        # 这里直接抛出错误，方便第一时间看到是 Key、配额、QPS 还是其它接口问题。
        raise GeocodeError(
            f"高德地理编码接口返回失败: address={address}, info={info}, infocode={infocode}"
        )

    # 高德有时会返回 status=1 但没有 geocodes 键，或者 geocodes 是空列表。
    # 这两种都属于“接口正常，但当前地址没有匹配到坐标”，应该返回空坐标而不是直接报 KeyError。
    geocodes = payload.get("geocodes")
    if not geocodes:
        return None, None

    # 高德返回的 location 形如 "113.333046,23.132714"。
    # 这里固定只取第一个 geocode 结果，并拆成 lon / lat 两个浮点数。
    location = str(geocodes[0].get("location", "")).strip()
    if not location or "," not in location:
        # 有些极端情况下 geocodes 不为空，但 location 字段本身无效。
        # 这种情况继续按“没有有效坐标”处理，不往下强行解析。
        return None, None

    lon_text, lat_text = location.split(",", 1)
    try:
        return float(lon_text), float(lat_text)
    except ValueError:
        return None, None


def euclidean_distance(x1, y1, x2, y2):
    # 这里直接计算二维平面上的欧式距离。
    # 当前最简单版脚本里传进来的就是 lon / lat，
    # 所以结果单位不是米，而是经纬度的度数差。
    return np.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def cluster_points(df, min_cluster_size=MIN_CLUSTER_SIZE, min_samples=MIN_SAMPLES):
    # cluster_points 的输入应该已经是一张带 lon / lat 的企业明细表。

    # 只有拿到有效经纬度的企业才有资格进入聚类。
    valid_mask = df["lon"].notna() & df["lat"].notna()
    valid_points = df.loc[valid_mask].copy()

    # 下面这些列是“聚类阶段新增的业务字段”。
    # 它们先初始化为空，等 HDBSCAN 跑完以后，再把真实值填进去。
    valid_points["cluster_id"] = pd.Series(pd.array([pd.NA] * len(valid_points), dtype="Int64"), index=valid_points.index)
    valid_points["cluster_company_count"] = pd.Series(
        pd.array([pd.NA] * len(valid_points), dtype="Int64"),
        index=valid_points.index,
    )
    valid_points["cluster_center_lon"] = np.nan
    valid_points["cluster_center_lat"] = np.nan
    valid_points["cluster_radius"] = np.nan

    # 直接把 lon / lat 当成二维平面上的点
    coordinates_xy = valid_points[["lon", "lat"]].to_numpy()

    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_epsilon=0.01,
        cluster_selection_method="eom",
        allow_single_cluster=True,
    )
    labels = clusterer.fit_predict(coordinates_xy)


    # raw_cluster_label 是 HDBSCAN 原始返回的簇编号。
    # 这个编号本身不稳定，所以后面还要再按业务规则重排成稳定的 cluster_id。
    valid_points["raw_cluster_label"] = labels
    valid_points["cluster_id"] = labels

    # HDBSCAN 的 -1 表示噪声点；只有非 -1 才参与正式簇统计与命名。
    clustered_points = valid_points.loc[valid_points["raw_cluster_label"] != -1].copy()
    if clustered_points.empty:
        valid_points["cluster_id"] = valid_points["cluster_id"].astype("Int64")
        valid_points = valid_points.drop(columns=["raw_cluster_label"])
        summary_columns = [
            "cluster_id",
            "cluster_company_count",
            "cluster_center_lon",
            "cluster_center_lat",
            "cluster_radius",
        ]
        return valid_points, pd.DataFrame(columns=summary_columns)

#-------------------------------------------------------------------------------------------
    # 先按 HDBSCAN 的原始簇标签，计算每个簇的基础统计值：
    # - cluster_company_count：簇内企业数
    # - cluster_center_lon / lat：这里先用简单均值作为簇中心点
    summary = (
        clustered_points.groupby("raw_cluster_label", as_index=False)
        .agg(
            cluster_company_count=("company_name", "size"),
            cluster_center_lon=("lon", "mean"),
            cluster_center_lat=("lat", "mean"),
        )
    )

    # 簇半径定义为：簇内所有点到簇中心点的最大平面欧式距离。
    # 当前直接使用 lon / lat 计算，所以这里的单位不是米，而是经纬度度数差。
    # 这样得到的是一个“覆盖这个簇所有企业点的最大半径”，便于判断这个簇是紧还是散。
    radius_values = []
    for raw_label in summary["raw_cluster_label"]:
        members = clustered_points.loc[clustered_points["raw_cluster_label"] == raw_label]
        center = summary.loc[summary["raw_cluster_label"] == raw_label].iloc[0]
        distances = euclidean_distance(
            members["lon"].to_numpy(),
            members["lat"].to_numpy(),
            np.full(len(members), center["cluster_center_lon"]),
            np.full(len(members), center["cluster_center_lat"]),
        )
        radius_values.append(float(distances.max()) if len(distances) else 0.0)

    summary["cluster_radius"] = radius_values
    # 这里按既定顺序重新编号：先按企业数降序，再按中心点经纬度升序。
    # 这样做的目的不是数学正确性，而是为了让重复运行时 cluster_id 尽量稳定，
    # 不至于今天同一批数据跑出来 0 号簇是这里，明天又换成别的地方。
    summary = summary.sort_values(
        by=["cluster_company_count", "cluster_center_lon", "cluster_center_lat"],
        ascending=[False, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    summary["cluster_id"] = pd.Series(range(len(summary)), dtype="Int64")

    raw_to_business_id = dict(zip(summary["raw_cluster_label"], summary["cluster_id"]))
    raw_to_company_count = dict(zip(summary["raw_cluster_label"], summary["cluster_company_count"]))
    raw_to_center_lon = dict(zip(summary["raw_cluster_label"], summary["cluster_center_lon"]))
    raw_to_center_lat = dict(zip(summary["raw_cluster_label"], summary["cluster_center_lat"]))
    raw_to_radius = dict(zip(summary["raw_cluster_label"], summary["cluster_radius"]))

    # 把“原始簇标签 -> 业务簇编号/统计值”映射回每一行企业明细。既将属性回填至明细表 (valid_points)
    # 最后每条企业记录都会知道自己属于哪个 cluster_id，以及这个簇的规模/中心/半径。
    clustered_mask = valid_points["raw_cluster_label"] != -1
    valid_points.loc[clustered_mask, "cluster_id"] = (
        valid_points.loc[clustered_mask, "raw_cluster_label"].map(raw_to_business_id).astype("Int64")
    )
    valid_points.loc[clustered_mask, "cluster_company_count"] = (
        valid_points.loc[clustered_mask, "raw_cluster_label"].map(raw_to_company_count).astype("Int64")
    )
    valid_points.loc[clustered_mask, "cluster_center_lon"] = valid_points.loc[
        clustered_mask, "raw_cluster_label"
    ].map(raw_to_center_lon)
    valid_points.loc[clustered_mask, "cluster_center_lat"] = valid_points.loc[
        clustered_mask, "raw_cluster_label"
    ].map(raw_to_center_lat)
    valid_points.loc[clustered_mask, "cluster_radius"] = valid_points.loc[
        clustered_mask, "raw_cluster_label"
    ].map(raw_to_radius)

    valid_points.loc[~clustered_mask, "cluster_id"] = -1
    valid_points["cluster_id"] = valid_points["cluster_id"].astype("Int64")
    valid_points = valid_points.drop(columns=["raw_cluster_label"])

    # summary 是“簇级结果”，一簇一行；
    # valid_points 是“企业级结果”，一企业一行。
    summary = summary[
        [
            "cluster_id",
            "cluster_company_count",
            "cluster_center_lon",
            "cluster_center_lat",
            "cluster_radius",
        ]
    ]
    return valid_points, summary


def process_workbook(input_path=INPUT_FILE, output_path=OUTPUT_FILE):
    # 读取 Excel 后，先只校验这次方案真正依赖的两列：
    # company_name 用来统计簇内企业数，clean_address 用来做地理编码。
    source_df = pd.read_excel(input_path)

    # 这里不是按 6998 行逐条调高德，而是先对 clean_address 去重。
    # 原因很直接：相同清洗地址的经纬度应该一致，没有必要重复请求。
    normalized_address = source_df["clean_address"].fillna("").astype(str).str.strip()
    unique_addresses = list(dict.fromkeys(address for address in normalized_address if address))
    geocode_lookup = {}

    #正地理编码
    for index, address in enumerate(unique_addresses, start=1):
        print(f"地理编码 {index}/{len(unique_addresses)}")
        try:
            geocode_lookup[address] = geocode_address(address)
        except GeocodeError as e:
            print(f"警告：地址解析跳过 -> {e}")
            geocode_lookup[address] = (None, None)  # 给一个空值，保证后续程序能跑完
        if GEOCODE_SLEEP_SECONDS > 0:
            # 这里用 sleep 控制请求节奏，避免因为 QPS 太高被高德直接限流。
            time.sleep(GEOCODE_SLEEP_SECONDS)

    # 把唯一地址的经纬度查找结果映射回每一条企业记录。
    # 到这里开始，同一 clean_address 的多家公司会共享完全相同的 lon / lat。
    with_coordinates = source_df.copy()
    coordinates = normalized_address.map(lambda address: geocode_lookup.get(address, (None, None)))
    with_coordinates["lon"] = pd.to_numeric(coordinates.map(lambda value: value[0]), errors="coerce")
    with_coordinates["lat"] = pd.to_numeric(coordinates.map(lambda value: value[1]), errors="coerce")

#---------------------------------------聚类-------------------------------------------------
    # 到这里为止，数据已经从“文本地址表”变成“企业 + 经纬度表”，下一步进入空间聚类。
    clustered_valid_points, summary_df = cluster_points(with_coordinates)

    # 下面开始把聚类结果写回明细表，并拆成三个业务 sheet：
    # 1. 聚类结果：成功成簇的企业
    # 2. 噪声点：有坐标但没进任何簇的企业
    # 3. 编码失败：根本没有拿到坐标的企业
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

    # result_df 是最终用于切 sheet 的总表，也就是创建一个包含所有企业（包括那些经纬度查询失败的企业）的大表
    # 它直接复用 with_coordinates 里的经纬度，只额外补上聚类相关列。
    result_df = with_coordinates.copy()
    result_df["cluster_id"] = pd.Series(pd.array([pd.NA] * len(result_df), dtype="Int64"), index=result_df.index)
    result_df["cluster_company_count"] = pd.Series(
        pd.array([pd.NA] * len(result_df), dtype="Int64"),
        index=result_df.index,
    )
    for column in ["cluster_center_lon", "cluster_center_lat", "cluster_radius"]:
        result_df[column] = np.nan

    if not clustered_valid_points.empty:
        result_df.loc[clustered_valid_points.index, cluster_columns] = clustered_valid_points[cluster_columns]

    # 用经纬度判断是否编码失败；剩下的有效点再按 cluster_id 拆成“成簇”或“噪声”。
    failed_mask = result_df["lon"].isna() | result_df["lat"].isna()
    clustered_mask = (~failed_mask) & (result_df["cluster_id"] != -1)
    noise_mask = (~failed_mask) & ~clustered_mask

    clustered_result = result_df.loc[clustered_mask, full_columns].copy()
    noise_result = result_df.loc[noise_mask, full_columns].copy()
    failed_result = result_df.loc[failed_mask, business_columns + coordinate_columns].copy()

    # 噪声点没有正式簇统计量，导出时保持为空。
    for column in ["cluster_company_count", "cluster_center_lon", "cluster_center_lat", "cluster_radius"]:
        noise_result[column] = np.nan

    # 最终输出四个 sheet：
    # 聚类结果 / 噪声点 / 编码失败 / 簇汇总。
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # 聚类结果：成功进入正式簇的企业明细
        clustered_result.to_excel(writer, sheet_name="聚类结果", index=False)
        # 噪声点：有坐标，但聚类后标签为 -1 的企业明细
        noise_result.to_excel(writer, sheet_name="噪声点", index=False)
        # 编码失败：高德没有返回有效 lon / lat 的企业明细
        failed_result.to_excel(writer, sheet_name="编码失败", index=False)
        # 簇汇总：每个 cluster_id 的统计信息，一簇一行
        summary_df.to_excel(writer, sheet_name="簇汇总", index=False)

    print(f"已输出: {output_path}")


if __name__ == "__main__":
    process_workbook()
