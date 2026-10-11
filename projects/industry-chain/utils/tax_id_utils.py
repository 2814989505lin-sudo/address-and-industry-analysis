from .env_config import load_dotenv
load_dotenv()

import json
import pandas as pd
import os
import time
from sqlalchemy import create_engine, text
from requests import Session
from urllib.parse import quote

engine_starrocks = create_engine(os.environ['SR_SOURCE_DSN'])
engine = create_engine(os.environ['SR_TARGET_DSN'])


# Stream load session class
class LoadSession(Session):
    def rebuild_auth(self, prepared_request, response):
        """
        No code here means requests will always preserve the Authorization
        header when redirected.
        """

def Ser_cal(input_Series, type):
    '''
    统计函数
    param input_Series: 输入的Series
    param type: 统计类型
    '''
    if len(input_Series) > 0:
        if type == 'sum':
            return input_Series.sum(skipna=True)
        if type == 'max':
            return input_Series.max(skipna=True)
        if type == 'min':
            return input_Series.min(skipna=True)
        if type == 'nunique':
            return input_Series.nunique()
        if type == 'std':
            return round(input_Series.std(skipna=True), 4)
        if type == 'mean':
            return round(input_Series.mean(skipna=True), 4)
        if type == 'median':
            return round(input_Series.median(skipna=True), 4)
        if type == 'mode':
            return input_Series.mode()[0]
        else:
            return 0
    else:
        return 0


def calc_ratio(x1, x2, n=4):
    '''
    占比计算
    param x1: 分子
    param x2: 分母
    '''
    if x1 == -9999 or x2 == -9999:
        return -9999
    elif x2 > 0:
        return round(x1 / x2, n)
    else:
        return -9999


def get_region_code_name(tax_id):
    """
    获取税号对应的地区编码和名称（省份级别）
    :param tax_id: 企业税号（统一社会信用代码，18位）
    :return: tuple (省份编码前4位, 省份名称)，解析失败返回 ('', '')
    """
    # 全国34个一级行政区编码（前4位）与名称映射
    region_code_dict = {
        '1100': '北京市',
        '1200': '天津市',
        '1300': '河北省',
        '1400': '山西省',
        '1500': '内蒙古自治区',
        '2100': '辽宁省',
        '2200': '吉林省',
        '2300': '黑龙江省',
        '3100': '上海市',
        '3200': '江苏省',
        '3300': '浙江省',
        '3400': '安徽省',
        '3500': '福建省',
        '3600': '江西省',
        '3700': '山东省',
        '4100': '河南省',
        '4200': '湖北省',
        '4300': '湖南省',
        '4400': '广东省',
        '4500': '广西壮族自治区',
        '4600': '海南省',
        '5000': '重庆市',
        '5100': '四川省',
        '5200': '贵州省',
        '5300': '云南省',
        '5400': '西藏自治区',
        '6100': '陕西省',
        '6200': '甘肃省',
        '6300': '青海省',
        '6400': '宁夏回族自治区',
        '6500': '新疆维吾尔自治区',
        '7100': '台湾省',
        '8100': '香港特别行政区',
        '8200': '澳门特别行政区'
    }

    try:
        # 统一社会信用代码第3-6位为地区编码（前4位）
        # 规则：第1位是登记管理部门代码（9=工商），第2位是机构类别，3-6位是地区码
        if len(tax_id) >= 6:
            area_code = tax_id[2:6]  # 提取第3-6位（索引2-5）
            # 匹配前4位编码（如“4403”→取“4400”对应广东省）
            province_code = area_code[:2] + '00'  # 前2位+00，得到省级编码
            if province_code in region_code_dict:
                return province_code, region_code_dict[province_code]
        # 解析失败返回空
        return '', ''
    except Exception as e:
        print(f"税号解析错误：{str(e)}")
        return '', ''
