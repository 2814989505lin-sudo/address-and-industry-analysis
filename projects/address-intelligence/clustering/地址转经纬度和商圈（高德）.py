# -*- coding: utf-8 -*-
"""
高德方法一脚本：
读取测试_cleaned.xlsx中的 clean_address 列，
调用高德正向地理编码和逆地理编码，
并将逆地理编码返回的 businessAreas 全部名称按顺序用英文逗号拼接，
最终写回同一个 Excel 文件中的“当前商圈”列。
"""
import os


import time

import requests
from openpyxl import load_workbook


# ====== 配置区 ======
# 这里沿用原脚本中的高德 Key，不在本次改造中调整密钥管理方式。
AMAP_KEY = os.environ['AMAP_API_KEY']
# ===================


def get_current_business_area(address: str, key: str = AMAP_KEY) -> str:
    """
    输入清洗后的地址，返回当前商圈字符串。

    返回规则固定为：
    1. 先对地址做高德正向地理编码，拿到经纬度。
    2. 再用经纬度做高德逆地理编码。
    3. 只提取 addressComponent.businessAreas 中的 name。
    4. 将全部商圈名称按高德原始顺序用英文逗号拼接。

    本函数不返回经纬度、不返回错误信息、不返回 POI，只返回最终要写入 Excel 的商圈文本。
    任一步失败时，按本次规则直接返回空字符串。
    """
    # 空地址不发请求，直接返回空字符串。
    if address is None:
        return ""

    # 统一转成字符串并去掉首尾空白，避免单元格里出现纯空格内容。
    clean_address = str(address).strip()
    if not clean_address:
        return ""

    try:
        # 第一步：调用高德正向地理编码，将地址转换为经纬度。
        geo_url = "https://restapi.amap.com/v3/geocode/geo"
        geo_params = {
            "address": clean_address,
            "key": key,
            "output": "json",
        }
        geo_res = requests.get(geo_url, params=geo_params, timeout=10).json()

        # 如果正向地理编码失败，按规则直接返回空字符串。
        if geo_res.get("status") != "1" or not geo_res.get("geocodes"):
            return ""

        # 只取第一个地理编码结果，这是当前方法一里唯一使用的坐标。
        first_geo = geo_res["geocodes"][0]
        location = first_geo.get("location", "")
        if not location:
            return ""

        # 第二步：调用高德逆地理编码，基于坐标获取商圈信息。
        regeo_url = "https://restapi.amap.com/v3/geocode/regeo"
        regeo_params = {
            "location": location,
            "key": key,
            "extensions": "all",
            "output": "json",
        }
        regeo_res = requests.get(regeo_url, params=regeo_params, timeout=10).json()

        # 如果逆地理编码失败，按规则直接返回空字符串。
        if regeo_res.get("status") != "1":
            return ""

        # 只提取 businessAreas，不使用 POI、经纬度、行政区等其它字段。
        address_component = regeo_res.get("regeocode", {}).get("addressComponent", {})
        business_areas = address_component.get("businessAreas", [])

        # 高德返回的 businessAreas 预期是对象列表，这里只保留存在 name 的项。
        area_names = []
        for business_area in business_areas:
            if isinstance(business_area, dict):
                area_name = str(business_area.get("name", "")).strip()
                if area_name:
                    area_names.append(area_name)

        # 将全部商圈按原顺序用英文逗号拼接。
        return ",".join(area_names)

    except Exception:
        # 本次实现不输出错误列，因此请求异常时统一返回空字符串。
        return ""


def process_workbook(file_path: str) -> None:
    """
    读取 Excel 中的 clean_address 列，并将当前商圈写入“当前商圈”列。

    处理规则固定为：
    1. 若缺少 clean_address，明确抛出异常。
    2. 若已有“当前商圈”列，则覆盖该列内容。
    3. 若没有“当前商圈”列，则在最后新增该列。
    4. 逐行调用高德接口处理。
    5. 保存时直接覆盖原文件。
    """
    # 加载原始工作簿，并固定处理第一个 sheet。
    workbook = load_workbook(file_path)
    worksheet = workbook[workbook.sheetnames[0]]

    # 读取首行表头，用于定位输入列和输出列。
    headers = [cell.value for cell in worksheet[1]]
    if "clean_address" not in headers:
        raise ValueError("Missing required column: clean_address")

    # 定位 clean_address 输入列。
    source_col = headers.index("clean_address") + 1

    # 如果“当前商圈”列已经存在，则覆盖原列；否则新增到最后一列。
    if "当前商圈" in headers:
        target_col = headers.index("当前商圈") + 1
    else:
        target_col = len(headers) + 1
        worksheet.cell(row=1, column=target_col, value="当前商圈")

    # 先统计实际需要处理的地址总数，只统计非空 clean_address。
    total_count = 0
    for row_idx in range(2, worksheet.max_row + 1):
        address = worksheet.cell(row=row_idx, column=source_col).value
        if address is not None and str(address).strip():
            total_count += 1

    # 从第二行开始逐条处理地址，并将结果写入目标列。
    current_count = 0
    for row_idx in range(2, worksheet.max_row + 1):
        address = worksheet.cell(row=row_idx, column=source_col).value

        # 空地址直接写空字符串；非空地址调用高德获取当前商圈。
        if address is not None and str(address).strip():
            current_count += 1
            print(f"{current_count}/{total_count}")
            current_business_area = get_current_business_area(address)
        else:
            current_business_area = ""

        worksheet.cell(row=row_idx, column=target_col, value=current_business_area)

        # 保留简单限频，避免请求过快。
        time.sleep(0.2)

    # 直接覆盖保存原文件。
    workbook.save(file_path)


if __name__ == "__main__":
    # 固定处理当前目录下的测试_cleaned.xlsx。
    process_workbook("测试_cleaned.xlsx")
