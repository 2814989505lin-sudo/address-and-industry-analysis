import argparse
import contextlib
import io
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text
import time
"""
脚本2：生成产业链企业宽表（{industry}_chain_enter_attr_{year}）

输入：
- --industry 产业链名称
- --year 年份

处理：
1) 从脚本1输出的 cls + 映射表取基础数据
2) 计算企业开票/节点关联/产业关联指标
3) 补充工商税务信息
4) 先删后写目标宽表（幂等重跑）
"""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.env_config import load_dotenv, get_target_engine, get_source_engine
from utils.starrocks_deal import load_data_batch
from utils.tax_id_utils import get_region_code_name


SALES_SI_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_sales__tmp"
SALES_ITEM_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_sales_item__goods_codes_fill_202408_202509"
INCOME_SI_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_income__si"
INCOME_ITEM_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_income_item__goods_codes_fill_202408_202509"
COMPANY_BASIC_VIEW = "fintech_data_lake.jwy_ods__jwy_dwh__company_basic_dimension_info__v"

# 六类发票类型口径：
# - trade_type：对应交易数据中的进/销项
# - tax_side：计算金额时税号落在 from 还是 to 侧
# - code4_fixed：行业主类型是否固定 code4_rate=1（沿旧逻辑）
INVOICE_RULES = {
    "sales": {"trade_type": "sales", "tax_side": "from", "code4_fixed": False},
    "income": {"trade_type": "income", "tax_side": "to", "code4_fixed": False},
    "industry_sales": {"trade_type": "sales", "tax_side": "from", "code4_fixed": True},
    "industry_income": {"trade_type": "income", "tax_side": "to", "code4_fixed": True},
    # 按旧脚本行为：expand 类型不映射到 sales/income，结果通常为0
    "industry_sales_expand": {"trade_type": "industry_sales_expand", "tax_side": "to", "code4_fixed": False},
    "industry_income_expand": {"trade_type": "industry_income_expand", "tax_side": "to", "code4_fixed": False},
}

AMOUNT_RANGES = [
    (0, 1000000, "0-100万"),
    (1000000, 5000000, "100万-500万"),
    (5000000, 10000000, "500万-1000万"),
    (10000000, 50000000, "1000万-5000万"),
    (50000000, 100000000, "5000万-1亿"),
    (100000000, 500000000, "1亿-5亿"),
    (500000000, 1000000000, "5亿-10亿"),
    (1000000000, 10000000000, "10亿-100亿"),
    (10000000000, float("inf"), "100亿以上"),
]

TRADE_TAX_CHUNK_SIZE = 2000
TRADE_CODE_CHUNK_SIZE = 2000
ENRICH_QUERY_CHUNK_SIZE = 2000
METRIC_STAGE_BATCH_SIZE = 5000
METRIC_CACHE_FLUSH_BATCHES = 50
ENRICH_STAGE_BATCH_SIZE = 2000
ENRICH_CACHE_FLUSH_BATCHES = 20
WRITE_BATCH_SIZE = 50000
WRITE_PROGRESS_FLUSH_BATCHES = 10
WRITE_MAX_RETRIES = 10
WRITE_RETRY_DELAY_SECONDS = 0
STATE_DIR = PROJECT_ROOT / "code" / "runtime_state"
LOG_DIR = PROJECT_ROOT / "code" / "runtime_logs"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_progress_path(industry, year):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"script2_{industry}_{year}.json"


def get_log_path(industry, year):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"script2_resume_{industry}_{year}.log"


def get_metric_cache_path(industry, year):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"script2_metric_{industry}_{year}.pkl"


def get_full_cache_path(industry, year):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"script2_full_{industry}_{year}.pkl"


def init_progress(industry, year):
    return {
        "chain_name": industry,
        "year": int(year),
        "metric_done_count": 0,
        "metric_done_keys": [],
        "enrich_done_tax_ids": [],
        "write_done_keys": [],
        "processing_stage": "metric",
        "last_error": "",
        "last_update": now_str(),
    }


def load_progress(progress_path, industry, year):
    if not progress_path.exists():
        return init_progress(industry, year)

    with open(progress_path, "r", encoding="utf-8") as f:
        progress = json.load(f)

    defaults = init_progress(industry, year)
    for k, v in defaults.items():
        progress.setdefault(k, v)

    progress["chain_name"] = industry
    progress["year"] = int(year)
    return progress


def save_progress(progress_path, progress):
    progress["last_update"] = now_str()
    tmp_path = progress_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, progress_path)


def log_resume_event(log_path, message):
    text_msg = f"{now_str()} | {message}"
    print(text_msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(text_msg + "\n")


def make_row_key(df):
    return (
        df["map_code"].fillna("").astype(str).str.strip()
        + "|"
        + df["tax_id"].fillna("").astype(str).str.strip()
        + "|"
        + df["invoice_type"].fillna("").astype(str).str.strip()
    )


def load_cache_df(cache_path):
    if not cache_path.exists():
        return pd.DataFrame()
    return pd.read_pickle(cache_path)


def append_cache_df(cache_path, df, key_col):
    if df.empty:
        return

    if cache_path.exists():
        old_df = pd.read_pickle(cache_path)
        merged = pd.concat([old_df, df], ignore_index=True)
    else:
        merged = df.copy()

    if key_col in merged.columns:
        merged = merged.drop_duplicates(subset=[key_col], keep="last")

    merged.to_pickle(cache_path)


def quote_table(name):
    """统一给表名加反引号，兼容中文表名。"""
    return f"`{name}`"


def get_amount_range(total):
    """按金额区间给企业打标签。"""
    try:
        total = float(total)
    except Exception:
        return "未知区间"
    if total <= 0:
        return "未知区间"
    for low, high, label in AMOUNT_RANGES:
        if low <= total < high:
            return label
    return "未知区间"


def get_invoice_scale(sales_amount):
    """按销项金额口径给企业开票规模分档。"""
    if sales_amount < 5000000:
        return "微型"
    if sales_amount < 20000000:
        return "小型"
    if sales_amount < 100000000:
        return "中型"
    return "大型"


def ensure_target_table(engine_target, table_name):
    """初始化最终宽表结构（不存在则创建）。"""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {quote_table(table_name)} (
      chain_name VARCHAR(50) NOT NULL,
      map_code VARCHAR(50) NOT NULL,
      tax_id VARCHAR(50) NOT NULL,
      invoice_type VARCHAR(200) NOT NULL,
      node1 VARCHAR(100) NULL,
      node2 VARCHAR(100) NULL,
      node3 VARCHAR(100) NULL,
      node_type VARCHAR(50) NULL,
      node_path VARCHAR(200) NULL,
      map_name VARCHAR(500) NULL,
      data_source VARCHAR(50) NULL,
      invoice_amount DECIMAL(18, 2) NULL,
      invoice_amount_range VARCHAR(100) NULL,
      code4_amount DECIMAL(18, 2) NULL,
      code4_rate DECIMAL(18, 4) NULL,
      node_num INT NULL,
      total_code4_amount DECIMAL(18, 2) NULL,
      total_code4_rate DECIMAL(18, 4) NULL,
      company_name VARCHAR(200) NULL,
      register_capital VARCHAR(50) NULL,
      establish_year INT NULL,
      invoice_size VARCHAR(20) NULL,
      tax_rating_level VARCHAR(10) NULL,
      sales_invoice_amount DECIMAL(18, 2) NULL,
      income_invoice_amount DECIMAL(18, 2) NULL,
      sales_abnormal_amt_rate DECIMAL(18, 4) NULL,
      income_abnormal_amt_rate DECIMAL(18, 4) NULL,
      industry1 VARCHAR(100) NULL,
      industry2 VARCHAR(100) NULL,
      industry3 VARCHAR(100) NULL,
      industry4 VARCHAR(100) NULL,
      province VARCHAR(50) NULL,
      province_short VARCHAR(50) NULL,
      city VARCHAR(50) NULL,
      district VARCHAR(50) NULL,
      id VARCHAR(100) NOT NULL,
      update_time DATETIME NULL
    ) ENGINE=OLAP
    DUPLICATE KEY(chain_name, map_code, tax_id, invoice_type)
    DISTRIBUTED BY HASH(map_code, tax_id) BUCKETS 16
    PROPERTIES ("replication_num" = "3");
    """

    with engine_target.begin() as conn:
        conn.execute(text(ddl))


def load_base_df(engine_target, cls_table, map_table, year):
    """读取脚本1产物，拉齐宽表前置字段。"""
    sql = f"""
    SELECT
      cls.chain_name,
      cls.map_code,
      m.tax_id,
      m.invoice_type,
      TRIM(
        CASE
          WHEN INSTR(TRIM(REPLACE(COALESCE(cls.node_path, ''), ' ', '')), '>') = 0
            THEN TRIM(REPLACE(COALESCE(cls.node_path, ''), ' ', ''))
          ELSE SPLIT_PART(TRIM(REPLACE(COALESCE(cls.node_path, ''), ' ', '')), '>', 1)
        END
      ) AS node1,
      TRIM(SPLIT_PART(TRIM(REPLACE(COALESCE(cls.node_path, ''), ' ', '')), '>', 2)) AS node2,
      TRIM(SPLIT_PART(TRIM(REPLACE(COALESCE(cls.node_path, ''), ' ', '')), '>', 3)) AS node3,
      cls.node_type,
      cls.node_path,
      cls.map_name,
      cls.data_source
    FROM {quote_table(cls_table)} cls
    INNER JOIN {quote_table(map_table)} m
      ON cls.chain_name = m.chain_name
     AND cls.map_code = m.map_code
    WHERE m.year = :year
      AND m.tax_id IS NOT NULL AND m.tax_id != ''
      AND m.invoice_type IS NOT NULL AND m.invoice_type != ''
    """
    return pd.read_sql(text(sql), engine_target, params={"year": int(year)})


def _chunked(items, size=500):
    """通用分批迭代器。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _query_trade_amount(
    engine_source,
    trade_type,
    tax_side,
    tax_ids,
    year,
    goods_codes=None,
):#从发票数据中聚合计算特定类型的交易金额
    """从发票主表+明细表直接聚合金额，不落交易明细正式表。"""
    if not tax_ids:
        return pd.DataFrame(columns=["tax_id", "goods_code", "amt"]) #空就直接返回

    # 与旧口径保持一致：只有 sales / income 两类有交易金额来源
    if trade_type not in ("sales", "income"):
        return pd.DataFrame(columns=["tax_id", "goods_code", "amt"])

    tax_ids = [str(x).strip() for x in tax_ids if pd.notna(x) and str(x).strip()]
    if not tax_ids:
        return pd.DataFrame(columns=["tax_id", "goods_code", "amt"])

    code_list = None
    if goods_codes is not None:
        code_list = [str(x).strip() for x in goods_codes if pd.notna(x) and str(x).strip()]
        if not code_list:
            return pd.DataFrame(columns=["tax_id", "goods_code", "amt"])

    date_start = f"{year}-01-01"
    date_end = f"{year}-12-31"
    start_ym = f"{year}-01"
    end_ym = f"{year}-12"

    if trade_type == "sales":
        si_table = SALES_SI_TABLE
        item_table = SALES_ITEM_TABLE
        tax_col = "s.saler_tax_no" #按from侧,谁卖
        counterpart_filter = "s.buyer_tax_no_decrypt IS NOT NULL AND s.buyer_tax_no_decrypt != ''"
    elif trade_type == "income":
        si_table = INCOME_SI_TABLE
        item_table = INCOME_ITEM_TABLE
        tax_col = "s.buyer_tax_no" #按to侧，谁买
        counterpart_filter = "s.saler_tax_no IS NOT NULL AND s.saler_tax_no != ''"
    else:
        return pd.DataFrame(columns=["tax_id", "goods_code", "amt"])

    goods_name_filter = (
        "i.goods_name IS NOT NULL AND i.goods_name != '' AND "
        "i.goods_name NOT IN ("
        "'原价合计', '折扣额合计', '(详见销货清单)', '（详见销货清单）', "
        "'(样见销货清单)', '（样见销货清单）', '详见对应正数发票及清单', '（详见对应正数发票及清单）'"
        ")"
    )

    out = []
    for tax_chunk in _chunked(tax_ids, TRADE_TAX_CHUNK_SIZE):
        tax_ph = ", ".join([f":tax{i}" for i in range(len(tax_chunk))])

        if code_list is not None:
            code_chunks = [code_list] if len(code_list) <= TRADE_CODE_CHUNK_SIZE else list(_chunked(code_list, TRADE_CODE_CHUNK_SIZE))
            for code_chunk in code_chunks:
                code_ph = ", ".join([f":code{i}" for i in range(len(code_chunk))])
                sql = f"""
                SELECT
                  {tax_col} AS tax_id,
                  i.goods_code,
                  SUM(COALESCE(i.detail_amount, 0) + COALESCE(i.tax_amount, 0)) AS amt 
                FROM {si_table} s
                INNER JOIN {item_table} i
                  ON s.id_md5 = i.id_md5
                WHERE {tax_col} IN ({tax_ph})
                  AND i.goods_code IN ({code_ph})
                  AND {counterpart_filter}
                  AND s.invoice_date BETWEEN :date_start AND :date_end
                  AND s.invoice_status IN ('0','3')
                  AND s.invoice_amount > 0
                  AND i.year_mon BETWEEN :start_ym AND :end_ym
                  AND {goods_name_filter}
                GROUP BY {tax_col}, i.goods_code
                """
                params = {f"tax{i}": v for i, v in enumerate(tax_chunk)}
                params.update({f"code{i}": v for i, v in enumerate(code_chunk)})
                params.update({"date_start": date_start, "date_end": date_end, "start_ym": start_ym, "end_ym": end_ym})
                one_df = pd.read_sql(text(sql), engine_source, params=params)
                if not one_df.empty:
                    out.append(one_df)
                #按企业+商品编码分组统计金额
                #原来代码的逻辑是先从si表筛选出有效票，再读item表，过滤无效的good_name，每一行做detail_amt + tax_amt, 算total的时候再sum；现在是直接si内连接item，过滤之后金额直接在里面算sum
        else:
            sql = f"""
            SELECT
              {tax_col} AS tax_id,
              SUM(COALESCE(i.detail_amount, 0) + COALESCE(i.tax_amount, 0)) AS amt
            FROM {si_table} s
            INNER JOIN {item_table} i
              ON s.id_md5 = i.id_md5
            WHERE {tax_col} IN ({tax_ph})
              AND {counterpart_filter}
              AND s.invoice_date BETWEEN :date_start AND :date_end
              AND s.invoice_status IN ('0','3')
              AND s.invoice_amount > 0
              AND i.year_mon BETWEEN :start_ym AND :end_ym
              AND {goods_name_filter}
            GROUP BY {tax_col}
            """
            #只按企业分组统计总金额
            params = {f"tax{i}": v for i, v in enumerate(tax_chunk)}
            params.update({"date_start": date_start, "date_end": date_end, "start_ym": start_ym, "end_ym": end_ym})
            one_df = pd.read_sql(text(sql), engine_source, params=params)
            if not one_df.empty:
                out.append(one_df)

    if not out:
        return pd.DataFrame(columns=["tax_id", "goods_code", "amt"])

    merged = pd.concat(out, ignore_index=True)#合并所有分批查询的结果
    if merged.empty:
        return merged

    if code_list is not None:
        return merged.groupby(["tax_id", "goods_code"], as_index=False)["amt"].sum()
    return merged.groupby(["tax_id"], as_index=False)["amt"].sum()# 有传入goodcode时候就按照tax_id +good_code聚合，没有传入的话就按照tax_id聚合


def build_metric_df(base_df, cls_df, engine_source, year, commodity_codes=None):
    """
    计算脚本2中间指标：
    - invoice_amount / invoice_amount_range
    - code4_amount / code4_rate
    - node_num / total_code4_amount / total_code4_rate
    """
    if base_df.empty:
        return base_df

    df = base_df.copy()
    df["invoice_amount"] = 0.0
    df["code4_amount"] = 0.0
    df["code4_rate"] = 0.0

    #筛选出所有数据来源为"商品分类" 的行，然后提取这些行的 map_code 列，最终得到一个不包含缺失值、由字符串组成的、去重后的列表。
    if commodity_codes is None:
        commodity_codes = (
            cls_df.loc[cls_df["data_source"] == "商品分类", "map_code"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
    processed_parts = []
    rule_types = set(INVOICE_RULES.keys())

    for inv_type, rule in INVOICE_RULES.items(): #遍历发票类型规则
        part = df[df["invoice_type"] == inv_type].copy() #提取当前发票类型的数据子集
        if part.empty:
            continue #跳过空

        part = part.drop(columns=["invoice_amount", "code4_amount", "code4_rate"], errors="ignore")
        part["tax_id"] = part["tax_id"].astype(str)
        part["map_code"] = part["map_code"].astype(str)
        tax_ids = part["tax_id"].dropna().astype(str).unique().tolist() #拿到唯一税号

        print(f"[脚本2] 计算 {inv_type}，税号数={len(tax_ids)}")

        inv_amt_df = _query_trade_amount(
            engine_source,
            trade_type=rule["trade_type"],
            tax_side=rule["tax_side"],
            tax_ids=tax_ids,
            year=year,
            goods_codes=None, #不传入goodcode说明计算企业发票总金额invoice_amount
        )
        if inv_amt_df.empty:
            part["invoice_amount"] = 0.0
        else:
            inv_amt_df = inv_amt_df.copy()
            inv_amt_df["tax_id"] = inv_amt_df["tax_id"].astype(str)
            inv_amt_df["invoice_amount"] = pd.to_numeric(inv_amt_df["amt"], errors="coerce").fillna(0.0)
            inv_amt_df = inv_amt_df.groupby("tax_id", as_index=False)["invoice_amount"].sum()
            part = part.merge(inv_amt_df, on="tax_id", how="left")
            part["invoice_amount"] = pd.to_numeric(part["invoice_amount"], errors="coerce").fillna(0.0)
        part["invoice_amount"] = part["invoice_amount"].round(2) #完成invoice_amount

        if rule["code4_fixed"]:
            # 行业主类型沿旧逻辑：code4 置空，关联度固定 1
            part["code4_amount"] = None
            part["code4_rate"] = 1.0
        else:
            map_codes = part["map_code"].dropna().astype(str).unique().tolist()#获取该part中唯一的mapcode
            code_df = _query_trade_amount(
                engine_source,
                trade_type=rule["trade_type"],
                tax_side=rule["tax_side"],
                tax_ids=tax_ids,
                year=year,
                goods_codes=map_codes, #传入goodcode说明算的是企业在该mapcode上的金额
            )
            if code_df.empty:
                part["code4_amount"] = 0.0
            else:
                code_df = code_df.copy()
                code_df["tax_id"] = code_df["tax_id"].astype(str)
                code_df["map_code"] = code_df["goods_code"].astype(str)
                code_df["code4_amount"] = pd.to_numeric(code_df["amt"], errors="coerce").fillna(0.0)
                code_df = code_df.groupby(["tax_id", "map_code"], as_index=False)["code4_amount"].sum()#如果同一个税号和商品编码出现多条记录，将它们合并为一条，金额累加
                part = part.merge(code_df[["tax_id", "map_code", "code4_amount"]], on=["tax_id", "map_code"], how="left")
                part["code4_amount"] = pd.to_numeric(part["code4_amount"], errors="coerce").fillna(0.0)
            part["code4_amount"] = part["code4_amount"].round(2)
            part["code4_rate"] = np.where(
                part["invoice_amount"] > 0,
                (part["code4_amount"] / part["invoice_amount"]).round(4),
                0.0,
            ) #拿到code4_amount和code4_rate

        processed_parts.append(part)

    other_part = df[~df["invoice_type"].isin(rule_types)].copy() #若还存在其他发票类型，一般是没有的了
    if not other_part.empty:
        other_part = other_part.drop(columns=["invoice_amount", "code4_amount", "code4_rate"], errors="ignore")
        other_part["invoice_amount"] = 0.0
        other_part["code4_amount"] = 0.0
        other_part["code4_rate"] = 0.0
        processed_parts.append(other_part)

    if processed_parts:
        df = pd.concat(processed_parts, ignore_index=False).sort_index() #按行方向拼接起来
    else:
        df["invoice_amount"] = 0.0
        df["code4_amount"] = 0.0
        df["code4_rate"] = 0.0

    df["invoice_amount"] = pd.to_numeric(df["invoice_amount"], errors="coerce").fillna(0.0).round(2)

    # node_num
    node_num_map = (
        df.groupby(["chain_name", "tax_id", "invoice_type"])["map_code"] #按chain_name，tax_id，invoice_type分组
        .nunique() #去重计算
        .rename("node_num")
        .reset_index()
    )
    df = df.merge(node_num_map, on=["chain_name", "tax_id", "invoice_type"], how="left")

    # total_code4_amount
    total_code4 = (
        df[~df["invoice_type"].isin(["industry_sales", "industry_income", "industry_sales_expand", "industry_income_expand"])]#非行业
        .assign(code4_amount_num=lambda x: pd.to_numeric(x["code4_amount"], errors="coerce").fillna(0.0))#创建新列 code4_amount_num，确保是数值且没有空值
        .groupby(["chain_name", "tax_id", "invoice_type"], as_index=False)["code4_amount_num"]
        .sum()
        .rename(columns={"code4_amount_num": "total_code4_amount"})
    )#一般情况

    # 行业类/expand：按产业链商品分类编码全集计算 total_code4_amount
    extra_frames = []
    for inv_type in ["industry_sales", "industry_income", "industry_sales_expand", "industry_income_expand"]:
        part = df[df["invoice_type"] == inv_type] #找到只包含当前发票类型的数据
        if part.empty:
            continue
        tax_ids = part["tax_id"].dropna().astype(str).unique().tolist()#取tax_id
        rule = INVOICE_RULES[inv_type]

        if not commodity_codes:
            continue

        ext_df = _query_trade_amount(
            engine_source,
            trade_type=rule["trade_type"],
            tax_side=rule["tax_side"],
            tax_ids=tax_ids,
            year=year,
            goods_codes=commodity_codes,#需要用所有商品分类编码去查询交易金额
        )
        if ext_df.empty:
            continue

        agg = ext_df.groupby("tax_id", as_index=False)["amt"].sum().rename(columns={"amt": "total_code4_amount"})
        agg["tax_id"] = agg["tax_id"].astype(str)
        chain_key = part[["chain_name", "tax_id"]].drop_duplicates().copy()
        chain_key["tax_id"] = chain_key["tax_id"].astype(str)
        one_extra = chain_key.merge(agg, on="tax_id", how="inner")
        one_extra["invoice_type"] = inv_type
        extra_frames.append(one_extra[["chain_name", "tax_id", "invoice_type", "total_code4_amount"]])

    if extra_frames:
        total_code4 = pd.concat([total_code4, *extra_frames], ignore_index=True)
        total_code4 = total_code4.groupby(["chain_name", "tax_id", "invoice_type"], as_index=False)["total_code4_amount"].sum()

    df = df.merge(total_code4, on=["chain_name", "tax_id", "invoice_type"], how="left")
    df["total_code4_amount"] = pd.to_numeric(df["total_code4_amount"], errors="coerce").fillna(0.0).round(2)

    # total_code4_rate & amount_range
    df["total_code4_rate"] = np.where(
        df["invoice_amount"] > 0,
        (df["total_code4_amount"] / df["invoice_amount"]).round(4),
        np.nan,
    )
    df["invoice_amount_range"] = df["invoice_amount"].apply(get_amount_range)

    return df


def batch_get_invoice_summary(engine_source, tax_ids, year):
    """批量获取企业年度进销项金额及异常金额。"""
    if not tax_ids:
        return {}

    date_start = f"{year}-01-01"
    date_end = f"{year}-12-31"
    out = {tax_id: {"sales_normal": 0.0, "sales_abnormal": 0.0, "income_normal": 0.0, "income_abnormal": 0.0} for tax_id in tax_ids}

    for chunk in _chunked(tax_ids, ENRICH_QUERY_CHUNK_SIZE):
        ph = ", ".join([f":tax{i}" for i in range(len(chunk))])
        params = {f"tax{i}": t for i, t in enumerate(chunk)}
        params.update({"start": date_start, "end": date_end})

        sales_sql = f"""
        SELECT tax_id,
               COALESCE(SUM(CASE WHEN invoice_status = '0' THEN total_amount ELSE 0 END), 0) AS sales_normal,
               COALESCE(SUM(CASE WHEN invoice_status IN ('2','3') THEN ABS(total_amount) ELSE 0 END), 0) AS sales_abnormal
        FROM {SALES_SI_TABLE}
        WHERE tax_id IN ({ph})
          AND invoice_date BETWEEN :start AND :end
        GROUP BY tax_id
        """
        df_sales = pd.read_sql(text(sales_sql), engine_source, params=params)

        income_sql = f"""
        SELECT tax_id,
               COALESCE(SUM(CASE WHEN invoice_status = '0' THEN total_amount ELSE 0 END), 0) AS income_normal,
               COALESCE(SUM(CASE WHEN invoice_status IN ('2','3') THEN ABS(total_amount) ELSE 0 END), 0) AS income_abnormal
        FROM {INCOME_SI_TABLE}
        WHERE tax_id IN ({ph})
          AND invoice_date BETWEEN :start AND :end
        GROUP BY tax_id
        """
        df_income = pd.read_sql(text(income_sql), engine_source, params=params)

        for _, r in df_sales.iterrows():
            tid = str(r["tax_id"])
            if tid in out:
                out[tid]["sales_normal"] = float(r["sales_normal"] or 0.0)
                out[tid]["sales_abnormal"] = float(r["sales_abnormal"] or 0.0)

        for _, r in df_income.iterrows():
            tid = str(r["tax_id"])
            if tid in out:
                out[tid]["income_normal"] = float(r["income_normal"] or 0.0)
                out[tid]["income_abnormal"] = float(r["income_abnormal"] or 0.0)

    return out


def batch_get_company_info(engine_source, tax_ids, year):
    """批量获取企业基础信息（名称、资本、行业、地区等）。"""
    if not tax_ids:
        return {}

    out = {
        tax_id: {
            "company_name": "未知",
            "register_capital": "未知",
            "establish_year": -9999,
            "industry1": "未知",
            "industry2": "未知",
            "industry3": "未知",
            "industry4": "未知",
            "province_short": "未知",
            "city": "未知",
            "district": "未知",
        }
        for tax_id in tax_ids
    }

    for chunk in _chunked(tax_ids, ENRICH_QUERY_CHUNK_SIZE):
        ph = ", ".join([f":tax{i}" for i in range(len(chunk))])
        params = {f"tax{i}": t for i, t in enumerate(chunk)}

        sql = f"""
        SELECT credit_no AS tax_id,
               company_name,
               capital_num,
               establish_date,
               industry_l1_name,
               industry_l2_name,
               industry_l3_name,
               industry_l4_name,
               province_short,
               city,
               district
        FROM {COMPANY_BASIC_VIEW}
        WHERE credit_no IN ({ph})
        """
        df = pd.read_sql(text(sql), engine_source, params=params)

        for _, row in df.iterrows():
            tid = str(row["tax_id"])
            if tid not in out:
                continue
            # 处理成立年份
            establish_year = -9999
            if pd.notna(row.get("establish_date")):
                m = re.search(r"(\d{4})", str(row["establish_date"]))
                if m:
                    y = int(m.group(1))
                    if 1900 <= y <= year:
                        establish_year = y
            # 处理注册资本
            register_capital = "未知"
            if pd.notna(row.get("capital_num")) and str(row.get("capital_num")).strip() != "":
                try:
                    register_capital = f"{round(float(row['capital_num']), 2)} 万人民币"
                except Exception:
                    pass
            # 更新信息
            out[tid] = {
                "company_name": row["company_name"] if pd.notna(row.get("company_name")) else "未知",
                "register_capital": register_capital,
                "establish_year": establish_year,
                "industry1": row["industry_l1_name"] if pd.notna(row.get("industry_l1_name")) else "未知",
                "industry2": row["industry_l2_name"] if pd.notna(row.get("industry_l2_name")) else "未知",
                "industry3": row["industry_l3_name"] if pd.notna(row.get("industry_l3_name")) else "未知",
                "industry4": row["industry_l4_name"] if pd.notna(row.get("industry_l4_name")) else "未知",
                "province_short": row["province_short"] if pd.notna(row.get("province_short")) else "未知",
                "city": row["city"] if pd.notna(row.get("city")) else "未知",
                "district": row["district"] if pd.notna(row.get("district")) else "未知",
            }

    return out


def parse_tax_rating(rating_str, target_year):
    """解析税务评级JSON，提取 target_year 年评级。"""
    if rating_str is None:
        return "未知"

    raw_str = str(rating_str).strip()
    if raw_str == "" or raw_str.lower() in ("nan", "null"):
        return "未知"

    try:
        parsed = json.loads(raw_str)
    except Exception:
        return "未知"

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return "未知"

    for item in parsed:
        if not isinstance(item, dict):
            continue
        year_val = item.get("appraisalYear")
        try:
            year_val = int(year_val)
        except Exception:
            continue
        if year_val != int(target_year):
            continue
        result = item.get("evaluationResult", "未知")
        result_str = str(result) if result is not None else "未知"
        if "不参评" in result_str:
            return "不参评"
        return result_str
    return "未知"


def batch_get_tax_rating(engine_source, tax_ids, year):
    """批量获取税务评级，替代逐税号查询。"""
    if not tax_ids:
        return {}

    end_date = f"{year}-12-31"
    target_year = int(year) - 1
    out = {tax_id: "未知" for tax_id in tax_ids}

    for chunk in _chunked(tax_ids, ENRICH_QUERY_CHUNK_SIZE):
        ph = ", ".join([f":tax{i}" for i in range(len(chunk))])
        params = {f"tax{i}": t for i, t in enumerate(chunk)}
        params["end_date"] = end_date

        sql = f"""
        WITH ranked_task AS (
          SELECT
            tax_code AS tax_id,
            task_id,
            create_time,
            ROW_NUMBER() OVER (PARTITION BY tax_code ORDER BY create_time DESC) AS rn
          FROM fintech_data_lake.jwy_ods__jwy_tax__t_tax_declare_apply_record__si
          WHERE result_code = '000'
            AND cache_flag = 0
            AND tax_code IN ({ph})
            AND create_time <= :end_date
        )
        SELECT r.tax_id, d.rating
        FROM ranked_task r
        LEFT JOIN fintech_data_lake.jwy_ods__mongo_credit__tax_declare_zwy_result__si d
          ON r.tax_id = d.tax_id
         AND r.task_id = d.task_id
        WHERE r.rn = 1
        """
        df = pd.read_sql(text(sql), engine_source, params=params)
        if df.empty:
            continue

        for rec in df.itertuples(index=False):
            tax_id = str(getattr(rec, "tax_id"))
            rating_str = getattr(rec, "rating", None)
            out[tax_id] = parse_tax_rating(rating_str, target_year)

    return out


def enrich_enterprise_info(df, engine_source, year):
    """把企业税务/工商属性补到指标结果上。"""
    if df.empty:
        return df

    unique_tax_ids = df["tax_id"].dropna().astype(str).unique().tolist()
    print(f"[脚本2] 企业信息补充，企业数={len(unique_tax_ids)}")

    invoice_map = batch_get_invoice_summary(engine_source, unique_tax_ids, year) #批量获取销项/进项发票数据
    company_map = batch_get_company_info(engine_source, unique_tax_ids, year) #批量获取工商数据

    print("[脚本2] 税务评级批量查询中...")
    rating_map = batch_get_tax_rating(engine_source, unique_tax_ids, year) #批量获取税务评级

    ext_rows = []
    for tax_id in unique_tax_ids:
        inv = invoice_map.get(tax_id, {})
        comp = company_map.get(tax_id, {})

        sales_normal = float(inv.get("sales_normal", 0.0))
        income_normal = float(inv.get("income_normal", 0.0))
        sales_abnormal = float(inv.get("sales_abnormal", 0.0))
        income_abnormal = float(inv.get("income_abnormal", 0.0))

        sales_abnormal_rate = round(sales_abnormal / sales_normal, 4) if sales_normal > 0 else 0.0
        income_abnormal_rate = round(income_abnormal / income_normal, 4) if income_normal > 0 else 0.0 #计算异常率

        province = "未知"
        _, province_name = get_region_code_name(tax_id)
        if province_name:
            province = province_name

        ext_rows.append(
            {
                "tax_id": tax_id,
                "company_name": comp.get("company_name", "未知"),
                "register_capital": comp.get("register_capital", "未知"),
                "establish_year": int(comp.get("establish_year", -9999)),
                "invoice_size": get_invoice_scale(sales_normal),
                "tax_rating_level": rating_map.get(tax_id, "未知"),
                "sales_invoice_amount": round(sales_normal, 2),
                "income_invoice_amount": round(income_normal, 2),
                "sales_abnormal_amt_rate": sales_abnormal_rate,
                "income_abnormal_amt_rate": income_abnormal_rate,
                "industry1": comp.get("industry1", "未知"),
                "industry2": comp.get("industry2", "未知"),
                "industry3": comp.get("industry3", "未知"),
                "industry4": comp.get("industry4", "未知"),
                "province": province,
                "province_short": comp.get("province_short", "未知"),
                "city": comp.get("city", "未知"),
                "district": comp.get("district", "未知"),
            }
        )

    ext_df = pd.DataFrame(ext_rows)
    return df.merge(ext_df, on="tax_id", how="left")


def prepare_final_df(full_df):
    """补齐最终字段并生成写入数据。"""
    if full_df.empty:
        return full_df

    df = full_df.copy()
    df["row_key"] = make_row_key(df)
    if "id" not in df.columns:
        df["id"] = [str(uuid.uuid4()) for _ in range(len(df))]
    else:
        df["id"] = df["id"].fillna("")
        empty_mask = df["id"].astype(str).str.strip() == ""
        df.loc[empty_mask, "id"] = [str(uuid.uuid4()) for _ in range(int(empty_mask.sum()))]

    df["update_time"] = now_str()

    col_order = [
        "chain_name",
        "map_code",
        "tax_id",
        "invoice_type",
        "node1",
        "node2",
        "node3",
        "node_type",
        "node_path",
        "map_name",
        "data_source",
        "invoice_amount",
        "invoice_amount_range",
        "code4_amount",
        "code4_rate",
        "node_num",
        "total_code4_amount",
        "total_code4_rate",
        "company_name",
        "register_capital",
        "establish_year",
        "invoice_size",
        "tax_rating_level",
        "sales_invoice_amount",
        "income_invoice_amount",
        "sales_abnormal_amt_rate",
        "income_abnormal_amt_rate",
        "industry1",
        "industry2",
        "industry3",
        "industry4",
        "province",
        "province_short",
        "city",
        "district",
        "id",
        "update_time",
        "row_key",
    ]

    for col in col_order:
        if col not in df.columns:
            df[col] = None

    df = df[col_order]
    df = df.drop_duplicates(subset=["row_key"], keep="first")
    return df


def write_final_table_resume(
    engine_target,
    df,
    table_name,
    progress,
    progress_path,
    log_path,
):
    """按 row_key 断点续写目标宽表。"""
    write_done_set = set(progress.get("write_done_keys", []))

    def _call_write_with_retry_log(chunk_payload, batch_no, retry_no, max_retries):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = load_data_batch(chunk_payload, table_name)
        raw = buf.getvalue().strip()
        if ok:
            log_resume_event(
                log_path,
                f"[write_stage] 批次={batch_no} 尝试{retry_no}/{max_retries} 成功 rows={len(chunk_payload)}",
            )
        else:
            last_line = raw.splitlines()[-1] if raw else "未知错误"
            log_resume_event(
                log_path,
                f"[write_stage] 批次={batch_no} 尝试{retry_no}/{max_retries} 失败 rows={len(chunk_payload)} | {last_line}",
            )
        return ok

    pending_df = df[~df["row_key"].isin(write_done_set)].copy() #找到等待写入的数据

    if pending_df.empty:#处理没有待写入的情况
        progress["processing_stage"] = "done"
        progress["last_error"] = ""
        save_progress(progress_path, progress)
        log_resume_event(log_path, "[write_stage] 无待写入记录，跳过")
        return

    total_batches = math.ceil(len(pending_df) / WRITE_BATCH_SIZE)
    log_resume_event(log_path, f"[write_stage] 开始写入，待写={len(pending_df)}，批次={total_batches}")

    for i in range(0, len(pending_df), WRITE_BATCH_SIZE):
        batch_no = i // WRITE_BATCH_SIZE + 1
        chunk = pending_df.iloc[i : i + WRITE_BATCH_SIZE].copy()
        batch_keys = chunk["row_key"].astype(str).tolist()
        payload = chunk.drop(columns=["row_key"], errors="ignore")#删除"row_key"列，因为不需要了
        ok = False
        for r in range(WRITE_MAX_RETRIES):
            try:
                ok = _call_write_with_retry_log(
                    chunk_payload=payload,
                    batch_no=batch_no,
                    retry_no=r + 1,
                    max_retries=WRITE_MAX_RETRIES,
                )
            except Exception as e:
                log_resume_event(
                    log_path,
                    f"[write_stage] 批次={batch_no} 尝试{r+1}/{WRITE_MAX_RETRIES} 异常 | {str(e)[:200]}",
                )
            if ok:
                break
            if r < WRITE_MAX_RETRIES - 1 and WRITE_RETRY_DELAY_SECONDS > 0:
                log_resume_event(
                    log_path,
                    f"[write_stage] 批次={batch_no} 将在 {WRITE_RETRY_DELAY_SECONDS}s 后重试",
                )
                time.sleep(WRITE_RETRY_DELAY_SECONDS)

        if not ok:
            progress["processing_stage"] = "write"
            progress["write_done_keys"] = list(write_done_set)
            progress["last_error"] = f"写入失败: 批次{batch_no}"
            save_progress(progress_path, progress)
            log_resume_event(log_path, f"[write_stage] 失败 批次={batch_no}")
            raise RuntimeError(f"写入失败: {table_name}, 批次 {batch_no}")
        #更新进度
        write_done_set.update(batch_keys)
        progress["processing_stage"] = "write"
        progress["last_error"] = ""
        need_flush = (batch_no % WRITE_PROGRESS_FLUSH_BATCHES == 0) or (batch_no == total_batches)
        if need_flush:
            progress["write_done_keys"] = list(write_done_set)
            save_progress(progress_path, progress)
            log_resume_event(
                log_path,
                f"[write_stage] flush 批次={batch_no}/{total_batches}，累计写入键={len(write_done_set)}",
            )
        log_resume_event(log_path, f"[write_stage] 成功 批次={batch_no}/{total_batches}，写入={len(payload)}")

    progress["processing_stage"] = "done"
    progress["write_done_keys"] = list(write_done_set)
    progress["last_error"] = ""
    save_progress(progress_path, progress)
    log_resume_event(log_path, f"[write_stage] 完成，累计写入键={len(write_done_set)}")


def main():
    start_time = time.time()

    """主流程：读基础 -> 算指标 -> 补企业信息 -> 落表。"""
    parser = argparse.ArgumentParser(description="脚本2：生成产业链企业属性宽表")
    parser.add_argument("--industry", default = "AM", help="目标产业链名称，例如：AM")
    parser.add_argument("--year", default = "2024", type=int, help="目标年份，例如：2024")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT)

    #确认表名，确认路径
    cls_table = f"industry_chain_cls_{args.industry}_ly"
    map_table = f"industry_chain_code_enterprise_map_dev_{args.industry}_{args.year}_ly"
    target_table = f"{args.industry}_chain_enter_attr_{args.year}_ly"
    progress_path = get_progress_path(args.industry, args.year)
    log_path = get_log_path(args.industry, args.year)
    metric_cache_path = get_metric_cache_path(args.industry, args.year)
    full_cache_path = get_full_cache_path(args.industry, args.year)

    engine_target = get_target_engine()
    engine_source = get_source_engine()

    try:
        ensure_target_table(engine_target, target_table)# 确保目标表存在

        progress = load_progress(progress_path, args.industry, args.year) #加载进度
        metric_done_count = len(progress.get("metric_done_keys", []))
        progress["metric_done_count"] = metric_done_count
        save_progress(progress_path, progress) #保存进度
        log_resume_event( #写个开头
            log_path,
            (
                f"[resume] 启动 metric_done={metric_done_count}, "
                f"enrich_done={len(progress.get('enrich_done_tax_ids', []))}, "
                f"write_done={len(progress.get('write_done_keys', []))}"
            ),
        )

        '''进度与缓存不一致时，自动回退对应阶段
        metric_progress_exists = bool(progress.get("metric_done_keys"))
        if metric_progress_exists and not metric_cache_path.exists():
            progress["metric_done_count"] = 0
            progress["metric_done_keys"] = []
            progress["enrich_done_tax_ids"] = []
            progress["write_done_keys"] = []
            log_resume_event(log_path, "[resume] 发现metric缓存缺失，已重置 metric/enrich/write 进度")
            save_progress(progress_path, progress)

        if progress.get("enrich_done_tax_ids") and not full_cache_path.exists():
            progress["enrich_done_tax_ids"] = []
            progress["write_done_keys"] = []
            log_resume_event(log_path, "[resume] 发现full缓存缺失，已重置 enrich/write 进度")
            save_progress(progress_path, progress)
        '''

        base_df = load_base_df(engine_target, cls_table, map_table, args.year) #读脚本1的表，之后去拿到直接需要展示的字段

        base_df = base_df.copy()
        base_df["row_key"] = make_row_key(base_df) #生成唯一行键
        base_df = base_df.drop_duplicates(subset=["row_key"], keep="first").reset_index(drop=True) #去重
        cls_df = base_df[["chain_name", "map_code", "data_source"]].drop_duplicates()
        commodity_codes = (
            cls_df.loc[cls_df["data_source"] == "商品分类", "map_code"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )

        # metric_stage：按 row_key 续跑（以 progress 中 metric_done_keys 为准）
        metric_done_set = set(progress.get("metric_done_keys", []))

        pending_metric_df = base_df[~base_df["row_key"].isin(metric_done_set)].copy() #找到未完成的
        if not pending_metric_df.empty:
            # 同类发票与同税号尽量聚集，减少每批重复计算与重复 SQL 查询
            pending_metric_df = pending_metric_df.sort_values(
                by=["invoice_type", "tax_id", "map_code"],
                kind="mergesort",
            ).reset_index(drop=True)

        if not pending_metric_df.empty: #如果有未完成的
            progress["processing_stage"] = "metric"
            save_progress(progress_path, progress)
            total_metric_batches = math.ceil(len(pending_metric_df) / METRIC_STAGE_BATCH_SIZE)
            metric_buffer = []
            buffered_rows = 0
            log_resume_event(
                log_path,
                f"[metric_stage] 开始，待处理={len(pending_metric_df)}，批次={total_metric_batches}",
            )
            for i in range(0, len(pending_metric_df), METRIC_STAGE_BATCH_SIZE):
                batch_no = i // METRIC_STAGE_BATCH_SIZE + 1 #批次
                part = pending_metric_df.iloc[i : i + METRIC_STAGE_BATCH_SIZE].copy() # 切片获取当前批次数据
                try:
                    metric_part = build_metric_df(part, cls_df, engine_source, args.year, commodity_codes=commodity_codes)

                    #关于断点重跑的操作
                    metric_part["row_key"] = make_row_key(metric_part)
                    metric_buffer.append(metric_part)
                    buffered_rows += len(metric_part)
                    metric_done_set.update(metric_part["row_key"].astype(str).tolist())

                    need_flush = (batch_no % METRIC_CACHE_FLUSH_BATCHES == 0) or (batch_no == total_metric_batches)
                    if need_flush and metric_buffer:
                        metric_chunk = pd.concat(metric_buffer, ignore_index=True)
                        append_cache_df(metric_cache_path, metric_chunk, "row_key")
                        metric_buffer = []
                        flushed_rows = buffered_rows
                        buffered_rows = 0
                        progress["metric_done_count"] = len(metric_done_set)
                        progress["metric_done_keys"] = list(metric_done_set)
                        progress["last_error"] = ""
                        save_progress(progress_path, progress)
                        log_resume_event(
                            log_path,
                            f"[metric_stage] flush 批次={batch_no}/{total_metric_batches}，写缓存={flushed_rows}，累计完成={len(metric_done_set)}",
                        )
                    log_resume_event(
                        log_path,
                        f"[metric_stage] 成功 批次={batch_no}/{total_metric_batches}，记录={len(metric_part)}",
                    )
                except Exception as e:
                    progress["last_error"] = str(e)[:500]
                    save_progress(progress_path, progress)
                    log_resume_event(log_path, f"[metric_stage] 失败 批次={batch_no}，error={str(e)[:200]}")
                    raise
        else:
            progress["metric_done_count"] = len(metric_done_set)
            progress["metric_done_keys"] = list(metric_done_set)
            save_progress(progress_path, progress)
            log_resume_event(log_path, "[metric_stage] 无待处理记录，跳过")

        progress["processing_stage"] = "enrich"
        save_progress(progress_path, progress)
        metric_df = load_cache_df(metric_cache_path)
        if metric_df.empty:
            raise RuntimeError("metric缓存为空，无法进入enrich阶段")
        if "row_key" not in metric_df.columns:
            metric_df["row_key"] = make_row_key(metric_df)
        metric_df = metric_df.copy()
        metric_df["_tax_id_str"] = metric_df["tax_id"].astype(str)

        # enrich_stage：按 tax_id 续跑
        enrich_done_set = set(progress.get("enrich_done_tax_ids", []))
        all_tax_ids = metric_df["_tax_id_str"].dropna().unique().tolist()
        tax_row_idx_map = metric_df.groupby("_tax_id_str", sort=False).indices
        pending_tax_ids = [t for t in all_tax_ids if t not in enrich_done_set]
        if pending_tax_ids:
            progress["processing_stage"] = "enrich"
            save_progress(progress_path, progress)
            total_enrich_batches = math.ceil(len(pending_tax_ids) / ENRICH_STAGE_BATCH_SIZE)
            enrich_buffer = []
            buffered_tax_ids = []
            buffered_rows = 0
            log_resume_event(
                log_path,
                f"[enrich_stage] 开始，待处理税号={len(pending_tax_ids)}，批次={total_enrich_batches}",
            )
            for i in range(0, len(pending_tax_ids), ENRICH_STAGE_BATCH_SIZE):
                batch_no = i // ENRICH_STAGE_BATCH_SIZE + 1
                tax_chunk = pending_tax_ids[i : i + ENRICH_STAGE_BATCH_SIZE]
                try:
                    idx_parts = [tax_row_idx_map[t] for t in tax_chunk if t in tax_row_idx_map]
                    if not idx_parts:
                        continue
                    if len(idx_parts) == 1:
                        row_idx = idx_parts[0]
                    else:
                        row_idx = np.concatenate(idx_parts)
                    metric_part = metric_df.iloc[row_idx].drop(columns=["_tax_id_str"], errors="ignore").copy()
                    enrich_part = enrich_enterprise_info(metric_part, engine_source, args.year)
                    #关于断点重跑的操作
                    enrich_part["row_key"] = make_row_key(enrich_part)
                    enrich_buffer.append(enrich_part)
                    buffered_tax_ids.extend([str(x) for x in tax_chunk])
                    buffered_rows += len(enrich_part)
                    need_flush = (batch_no % ENRICH_CACHE_FLUSH_BATCHES == 0) or (batch_no == total_enrich_batches)
                    if need_flush and enrich_buffer:
                        enrich_chunk = pd.concat(enrich_buffer, ignore_index=True)
                        append_cache_df(full_cache_path, enrich_chunk, "row_key")
                        enrich_done_set.update(buffered_tax_ids)
                        progress["enrich_done_tax_ids"] = list(enrich_done_set)
                        progress["last_error"] = ""
                        save_progress(progress_path, progress)
                        log_resume_event(
                            log_path,
                            f"[enrich_stage] flush 批次={batch_no}/{total_enrich_batches}，写缓存={buffered_rows}，累计税号={len(enrich_done_set)}",
                        )
                        enrich_buffer = []
                        buffered_tax_ids = []
                        buffered_rows = 0
                    log_resume_event(
                        log_path,
                        f"[enrich_stage] 成功 批次={batch_no}/{total_enrich_batches}，税号={len(tax_chunk)}",
                    )
                except Exception as e:
                    progress["last_error"] = str(e)[:500]
                    save_progress(progress_path, progress)
                    log_resume_event(log_path, f"[enrich_stage] 失败 批次={batch_no}，error={str(e)[:200]}")
                    raise
        else:
            log_resume_event(log_path, "[enrich_stage] 无待处理税号，跳过")

        progress["processing_stage"] = "write"
        save_progress(progress_path, progress)
        full_df = load_cache_df(full_cache_path)
        if full_df.empty:
            raise RuntimeError("full缓存为空，无法进入write阶段")

        full_df = prepare_final_df(full_df)
        log_resume_event(log_path, f"[write_stage] 待写总键数={len(full_df)}")
        write_final_table_resume(
            engine_target,
            full_df,
            target_table,
            progress,
            progress_path,
            log_path,
        )
        log_resume_event(log_path, f"[脚本2] 完成: {target_table}")

    finally:
        total_seconds = time.time() - start_time
        print(f"【耗时】脚本总耗时：{total_seconds:.2f}秒（约{total_seconds/60:.2f}分钟）")
        engine_target.dispose()
        engine_source.dispose()


if __name__ == "__main__":
    main()

#代码二按照chain_name,map_code,tax_id,invoice_type去重
