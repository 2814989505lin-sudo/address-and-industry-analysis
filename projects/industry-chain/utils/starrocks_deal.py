from .env_config import load_dotenv
load_dotenv()
from .env_config import get_stream_load_config

import json
import pandas as pd
import os
import requests
import time
from sqlalchemy import create_engine, text
from requests import Session
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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


def load_data_batch(dataframes, tablename):
    """
    批量导入数据使用stream_load
    dataframes: pandas DataFrame
    tablename: 表名
    """
    username, pass_ = (get_stream_load_config()['username'], get_stream_load_config()['password'])

    df_str = dataframes.apply(
        lambda row: ",".join(map(lambda x: "" if pd.isna(x) else str(x), row)),
        axis=1,
    )  # 填充

    # 用换行符连接每行数据
    result_str = "\n".join(df_str)
    cols = ",".join(dataframes.columns.tolist())  # 自动获取列名，作为数据库的字段名，导入数据和列名一一对应。
    headers = {
        "Content-Type": "text/html; charset=UTF-8",
        "connection": "keep-alive",
        "columns": cols,
        "column_separator": ",",
        "Expect": "100-continue",
    }

    database = "fintech_stat"
    api = ('http://{host}:{port}/api/%s/%s/_stream_load'.format(**get_stream_load_config())) % (database, tablename)
    session = LoadSession()
    session.auth = (username, pass_)

    try:
        response = session.put(url=api, headers=headers, data=result_str.encode("utf-8"))
        json_out = dict(response.json())
        if len(json_out) != 0:
            status = json_out.get("Status", "导入失败")
            message = json_out.get("Message", "无信息")
            rows = json_out.get("NumberTotalRows", "None")
            if status == 'Fail':
                print(f"导入状态：{status} - 信息：{message}")
                return False
            else:
                print(f"导入状态：{status} - 信息：{message} - 成功导入：{rows}条数据")
                return True
        else:
            print("导入失败：无返回信息")
            return False
    except Exception as e:
        print(f"导入异常：{e}")
        return False



def save_progress(goods_code4, tax_id, PROGRESS_FILE):
    """保存进度到文件"""
    with open(PROGRESS_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{goods_code4},{tax_id}\n")


def load_progress(PROGRESS_FILE):
    """加载已处理的进度"""
    processed = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split(',')
                    if len(parts) == 2:
                        processed.add((parts[0], parts[1]))
    return processed


def clear_progress(PROGRESS_FILE):
    """清空进度文件"""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


