import argparse
import json
import os
import sys
import uuid
import io
import contextlib
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

import time
"""
脚本1：构建产业链 cls + 企业上链映射 + 节点统计

输入：
- --industry 产业链名称（用于拼接目标表名）
- --year 年份
- --excel 人工维护的产业链 Excel

输出：
1) industry_chain_cls_{industry}
2) industry_chain_code_enterprise_map_dev_{industry}_{year}
"""

PROJECT_ROOT = Path(__file__).resolve().parents[1] #根目录
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT)) #检查根目录是否存在，没有则新建

from utils.env_config import load_dotenv, get_target_engine, get_source_engine
from utils.starrocks_deal import load_data_batch


COMPANY_BASIC_VIEW = "fintech_data_lake.jwy_ods__jwy_dwh__company_basic_dimension_info__v"
SALES_ITEM_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_sales_item__goods_codes_fill_202408_202509"
INCOME_ITEM_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_income_item__goods_codes_fill_202408_202509"
SALES_SI_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_sales__tmp"
INCOME_SI_TABLE = "fintech_data_lake.jwy_ods__jwy_tax__t_invoice_income__si"
STATE_DIR = PROJECT_ROOT / "code" / "runtime_state"
LOG_DIR = PROJECT_ROOT / "code" / "runtime_logs"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_map_log_path(industry, year):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"script1_map_resume_{industry}_{year}.log"


def log_map_event(log_path, message):
    """map断点重跑最小日志：控制台 + 文件追加。"""
    ts_msg = f"{now_str()} | {message}"
    print(ts_msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(ts_msg + "\n")


def get_progress_path(industry, year):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"script1_{industry}_{year}.json"


def init_progress(industry, year, total_map_codes):
    return {
        "chain_name": industry,
        "year": int(year),
        "total_map_codes": int(total_map_codes),
        "completed_map_codes": [],
        "processing_map_code": None,
        "map_code_details": {},
        "last_run_time": now_str(),
    }


def save_progress(progress_path, progress):
    progress["last_run_time"] = now_str()
    tmp_path = progress_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, progress_path)


def load_progress(progress_path, industry, year, total_map_codes):
    if not progress_path.exists():
        return init_progress(industry, year, total_map_codes)

    with open(progress_path, "r", encoding="utf-8") as f:
        progress = json.load(f)

    defaults = init_progress(industry, year, total_map_codes)
    for k, v in defaults.items():
        progress.setdefault(k, v)

    progress["chain_name"] = industry
    progress["year"] = int(year)
    progress["total_map_codes"] = int(total_map_codes)
    return progress


def quote_table(name):
    """统一给表名加反引号，避免中文或特殊字符表名在 SQL 中报错。"""
    return f"`{name}`"


def process_excel(excel_path, sheet_name):
    """把产业链 Excel 解析为 cls 表结构（逻辑对齐 1_industry_chain_cls.py）。"""
    df = pd.read_excel(excel_path, dtype=str, sheet_name=sheet_name)
    records = []

    def _valid(v): #检查值是否非空且不是空白字符串
        return pd.notna(v) and str(v).strip() != ""

    def _last_node(row): #从node的列开始，找到第一个非空值,获取产业链的末端节点
        for i in range(9, 0, -1):
            col = f"node{i}"
            if col in row and _valid(row[col]):
                return str(row[col]).strip()
        raise ValueError(f"没有找到有效的节点：{row}")

    def _node_path(row): #正序拼接（node1→node2→node3→...→node9），保留空值
        parts = []
        for i in range(1, 10):
            col = f"node{i}"
            if col in row and _valid(row[col]):
                parts.append(str(row[col]).strip())
        return ">".join(parts) if parts else None

    def _map_code_name(row):#收集code1-4和对应的name1-4
        code_parts, name_parts = [], []
        for i in range(1, 5):
            code_col = f"code{i}"
            name_col = f"name{i}"
            if pd.notna(row.get(code_col)) and str(row.get(code_col)).strip() != "":
                code_parts.append(str(row[code_col]).strip())
                name_parts.append(str(row[name_col]).strip())
        return "".join(code_parts), "-".join(name_parts)

    for _, row in df.iterrows():
        if not pd.notna(row["chain_name"]):
            break

        map_code, map_name = _map_code_name(row)

        records.append(
            {
                "id": str(uuid.uuid4()),
                "last_node_type": _last_node(row),
                "chain_name": row["chain_name"],
                "node_path": _node_path(row),
                "node_type": row["node_type"],
                "map_code": str(map_code),
                "map_name": map_name,
                "data_source": row["source"],
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    return pd.DataFrame(records)


def ensure_tables(engine, cls_table, map_table):
    """初始化脚本1输出的2张表。"""
    ddl_cls = f"""
    CREATE TABLE IF NOT EXISTS {quote_table(cls_table)} (
      id VARCHAR(50) NOT NULL,
      last_node_type VARCHAR(100) NULL,
      chain_name VARCHAR(50) NULL,
      node_path VARCHAR(200) NULL,
      node_type VARCHAR(50) NULL,
      map_code VARCHAR(50) NULL,
      map_name VARCHAR(500) NULL,
      data_source VARCHAR(50) NULL,
      update_time DATETIME NULL
    ) ENGINE=OLAP
    DUPLICATE KEY(id)
    DISTRIBUTED BY HASH(id) BUCKETS 8
    PROPERTIES ("replication_num" = "3");
    """

    ddl_map = f"""
    CREATE TABLE IF NOT EXISTS {quote_table(map_table)} (
      map_code VARCHAR(50) NOT NULL,
      tax_id VARCHAR(50) NOT NULL,
      invoice_type VARCHAR(100) NOT NULL,
      year VARCHAR(20) NOT NULL,
      id VARCHAR(100) NOT NULL,
      chain_name VARCHAR(255) NOT NULL,
      update_time DATETIME NOT NULL
    ) ENGINE=OLAP
    PRIMARY KEY(map_code, tax_id, invoice_type, year)
    DISTRIBUTED BY HASH(map_code, tax_id, invoice_type) BUCKETS 4
    PROPERTIES (
      "replication_num" = "3",
      "in_memory" = "false",
      "storage_format" = "DEFAULT"
    );
    """

    with engine.begin() as conn:
        conn.execute(text(ddl_cls))
        conn.execute(text(ddl_map))


def _chunked(items, size=1000):
    """按固定大小分块，避免 IN 语句过长。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def query_tax_ids_by_prefix(engine_source, table_name, map_code, start_ym, end_ym):
    """商品分类口径：通过 goods_code 前缀匹配企业税号。"""
    sql = f"""
    SELECT DISTINCT tax_id
    FROM {table_name}
    WHERE goods_code LIKE :prefix
      AND year_mon BETWEEN :start_ym AND :end_ym
      AND tax_id IS NOT NULL
      AND tax_id != ''
    """
    df = pd.read_sql(text(sql), engine_source, params={"prefix": f"{map_code}%", "start_ym": start_ym, "end_ym": end_ym})
    return df["tax_id"].dropna().astype(str).tolist() if not df.empty else []


def query_tax_ids_by_industry_join(engine_source, table_name, tax_column, map_code, start_ym, end_ym):
    """行业分类口径：直接在交易表与企业行业维表 JOIN，减少大IN分批查询。"""
    if len(map_code) == 6:
        industry_filter = """
          c.industry_l1_code = :l1
          AND c.industry_l2_code = :l2
          AND c.industry_l3_code = :l3
        """
        params = {"l1": map_code[0], "l2": map_code[1:3], "l3": map_code[3:6]}
    elif len(map_code) == 10:
        industry_filter = """
          c.industry_l1_code = :l1
          AND c.industry_l2_code = :l2
          AND c.industry_l3_code = :l3
          AND c.industry_l4_code = :l4
        """
        params = {"l1": map_code[0], "l2": map_code[1:3], "l3": map_code[3:6], "l4": map_code[6:10]}
    else:
        return []

    sql = f"""
    SELECT DISTINCT t.{tax_column} AS tax_id
    FROM {table_name} t
    INNER JOIN {COMPANY_BASIC_VIEW} c
      ON c.credit_no = t.{tax_column}
    WHERE {industry_filter}
      AND t.year_mon BETWEEN :start_ym AND :end_ym
      AND t.{tax_column} IS NOT NULL
      AND t.{tax_column} != ''
      AND c.credit_no IS NOT NULL
      AND c.credit_no != ''
    """
    params["start_ym"] = start_ym
    params["end_ym"] = end_ym
    df = pd.read_sql(text(sql), engine_source, params=params)
    return df["tax_id"].dropna().astype(str).tolist() if not df.empty else []


def build_mapping_for_row(row, year, engine_source):
    """按单个 map_code 构建上链记录，用于断点重跑。"""
    chain_name = str(row["chain_name"])
    map_code = str(row["map_code"]).strip()
    data_source = str(row.get("data_source") or "")
    start_ym = f"{year}-01"
    end_ym = f"{year}-12"
    update_time = now_str()
    out = []

    if not map_code:
        return pd.DataFrame(columns=["id", "chain_name", "map_code", "tax_id", "invoice_type", "year", "update_time"])

    if data_source == "商品分类":
        sales_ids = query_tax_ids_by_prefix(engine_source, SALES_ITEM_TABLE, map_code, start_ym, end_ym)
        income_ids = query_tax_ids_by_prefix(engine_source, INCOME_ITEM_TABLE, map_code, start_ym, end_ym)
        for tax_id in sales_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "sales",
                    "year": str(year),
                    "update_time": update_time,
                }
            )
        for tax_id in income_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "income",
                    "year": str(year),
                    "update_time": update_time,
                }
            )
    elif data_source == "国民经济行业分类":
        sales_ids = query_tax_ids_by_industry_join(
            engine_source, SALES_ITEM_TABLE, "tax_id", map_code, start_ym, end_ym
        )
        income_ids = query_tax_ids_by_industry_join(
            engine_source, INCOME_ITEM_TABLE, "tax_id", map_code, start_ym, end_ym
        )
        sales_expand_ids = query_tax_ids_by_industry_join(
            engine_source, SALES_SI_TABLE, "buyer_tax_no_decrypt", map_code, start_ym, end_ym
        )
        income_expand_ids = query_tax_ids_by_industry_join(
            engine_source, INCOME_SI_TABLE, "saler_tax_no", map_code, start_ym, end_ym
        )

        for tax_id in sales_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "industry_sales",
                    "year": str(year),
                    "update_time": update_time,
                }
            )
        for tax_id in income_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "industry_income",
                    "year": str(year),
                    "update_time": update_time,
                }
            )
        for tax_id in sales_expand_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "industry_sales_expand",
                    "year": str(year),
                    "update_time": update_time,
                }
            )
        for tax_id in income_expand_ids:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "chain_name": chain_name,
                    "map_code": map_code,
                    "tax_id": tax_id,
                    "invoice_type": "industry_income_expand",
                    "year": str(year),
                    "update_time": update_time,
                }
            )

    if not out:
        return pd.DataFrame(columns=["id", "chain_name", "map_code", "tax_id", "invoice_type", "year", "update_time"])

    return pd.DataFrame(out)


def delete_map_code_rows(engine_target, map_table, map_code, year):
    with engine_target.begin() as conn:
        conn.execute(
            text(f"DELETE FROM {quote_table(map_table)} WHERE map_code = :map_code AND year = :year"),
            {"map_code": map_code, "year": str(year)},
        )

def _sql_insert_fallback(engine_target, df, table_name, batch_size=2000):
    """当 stream load 失败时，回退为 SQL 批量插入。"""
    if df.empty:
        return

    cols = list(df.columns)
    fields = ", ".join([f"`{c}`" for c in cols])
    placeholders = ", ".join([f":{c}" for c in cols])
    insert_sql = text(f"INSERT INTO {quote_table(table_name)} ({fields}) VALUES ({placeholders})")

    records = []
    for rec in df.to_dict(orient="records"):
        clean_rec = {}
        for k, v in rec.items():
            if pd.isna(v):
                clean_rec[k] = None
            else:
                clean_rec[k] = v
        records.append(clean_rec)

    for i in range(0, len(records), batch_size):
        with engine_target.begin() as conn:
            conn.execute(insert_sql, records[i : i + batch_size])

def write_map_df_stream(
    df,
    table_name,
    batch_size=500,
    max_retries=10,
    retry_delay=0,
    max_consecutive_failed_batches=5,
    cooldown_seconds=120,
):
    if df.empty:
        return {"success": True, "inserted_rows": 0, "failed_batches": []}

    consecutive_failed_batches = 0
    inserted_rows = 0
    failed_batches = []

    for i in range(0, len(df), batch_size):
        chunk = df.iloc[i:i + batch_size].copy()
        batch_no = i // batch_size + 1
        ok = False

        for r in range(max_retries):
            try:
                ok = call_stream_load_with_unified_log(
                    chunk=chunk,
                    table_name=table_name,
                    batch_no=batch_no,
                    retry_no=r + 1,
                    max_retries=max_retries,
                )
                if ok:
                    break
            except Exception as e:
                print(f"[stream] 批次{batch_no} 尝试{r+1}/{max_retries} 异常 | {e}")

            if r < max_retries - 1:
                print(f"[stream] 批次{batch_no} 将在 {retry_delay}s 后重试")
                time.sleep(retry_delay)

        if ok:
            consecutive_failed_batches = 0
            inserted_rows += len(chunk)
            continue

        consecutive_failed_batches += 1
        failed_batches.append(batch_no)
        print(f"[stream] 批次{batch_no} 最终失败，连续失败批次={consecutive_failed_batches}")

        if consecutive_failed_batches >= max_consecutive_failed_batches:
            print(f"[stream] 触发熔断：暂停 {cooldown_seconds}s 后继续")
            time.sleep(cooldown_seconds)
            consecutive_failed_batches = 0

    return {
        "success": len(failed_batches) == 0,
        "inserted_rows": inserted_rows,
        "failed_batches": failed_batches,
    }


def call_stream_load_with_unified_log(chunk, table_name, batch_no, retry_no, max_retries):
    """
    调用 stream load，并把 utils 原始输出统一成脚本内格式。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = load_data_batch(chunk, table_name)

    raw = buf.getvalue().strip()
    if ok:
        print(
            f"[stream] 批次{batch_no} 尝试{retry_no}/{max_retries} 成功 "
            f"(rows={len(chunk)})"
        )
    else:
        # 只取最后一行异常/失败摘要，避免日志过长
        last_line = raw.splitlines()[-1] if raw else "未知错误"
        print(
            f"[stream] 批次{batch_no} 尝试{retry_no}/{max_retries} 失败 "
            f"(rows={len(chunk)}) | {last_line}"
        )
    return ok


def main():
    start_time = time.time()
    """主流程：建表 -> cls -> 映射 -> 统计。"""
    parser = argparse.ArgumentParser(description="脚本1：构建cls + 企业上链 + 节点统计")
    parser.add_argument("--industry", required=True, help="目标产业链名称，例如：增材制造")
    parser.add_argument("--year", required=True, type=int, help="目标年份，例如：2025")
    parser.add_argument("--excel", required=True, help="产业链Excel绝对路径")
    parser.add_argument("--sheet", default="Sheet1", help="Excel sheet名称")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT)

    cls_table = f"industry_chain_cls_{args.industry}_ly"
    map_table = f"industry_chain_code_enterprise_map_dev_{args.industry}_{args.year}_ly"
    progress_path = get_progress_path(args.industry, args.year)
    map_log_path = get_map_log_path(args.industry, args.year)

    engine_target = get_target_engine()
    engine_source = get_source_engine()


    try:
        ensure_tables(engine_target, cls_table, map_table)

        cls_df = process_excel(Path(args.excel), args.sheet) #获取cls表的数据
        if cls_df.empty:
            raise RuntimeError("Excel解析后无有效cls数据")

        # cls阶段不做断点：每次正常重写
        print(f"[脚本1] cls记录数: {len(cls_df)}")
        cls_start = time.time()
        with engine_target.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {quote_table(cls_table)}"))
        _sql_insert_fallback(engine_target, cls_df, cls_table)
        print(f"[耗时] cls_stage: {time.time() - cls_start:.2f} 秒")

        cls_unique = cls_df[["chain_name", "map_code", "data_source"]].drop_duplicates().reset_index(drop=True)
        progress = load_progress(progress_path, args.industry, args.year, len(cls_unique))
        save_progress(progress_path, progress)

        completed_set = set(progress.get("completed_map_codes", []))
        to_process_df = cls_unique[~cls_unique["map_code"].astype(str).isin(completed_set)].reset_index(drop=True)
        map_stage_start = time.time()
        map_batch_size = 500
        log_map_event(
            map_log_path,
            f"[map_stage] 已完成={len(completed_set)}，待处理={len(to_process_df)}，总数={len(cls_unique)}",
        )

        for idx, row in to_process_df.iterrows():
            one_map_start = time.time()
            map_code = str(row["map_code"]).strip()
            if not map_code:
                progress["map_code_details"]["<空>"] = {
                    "status": "failed",
                    "success_time": "",
                    "error": "map_code为空",
                    "inserted": 0,
                }
                save_progress(progress_path, progress)
                log_map_event(map_log_path, "[map_stage] map_code=<空>，跳过")
                continue

            log_map_event(map_log_path, f"[map_stage] 开始 {idx + 1}/{len(to_process_df)} map_code={map_code}")
            progress["processing_map_code"] = map_code
            progress["map_code_details"].setdefault(map_code, {})
            save_progress(progress_path, progress)

            try:
                map_df = build_mapping_for_row(row, args.year, engine_source)
                total_rows = len(map_df)
                total_batches = (total_rows + map_batch_size - 1) // map_batch_size if total_rows else 0
                log_map_event(
                    map_log_path,
                    f"[map_stage] map_code={map_code} 待写入行数={total_rows}，预计批次={total_batches}（batch_size={map_batch_size}）",
                )
                delete_map_code_rows(engine_target, map_table, map_code, args.year)
                write_res = write_map_df_stream(
                    map_df,
                    map_table,
                    batch_size=map_batch_size,
                    max_retries=10,
                    retry_delay=0,
                    max_consecutive_failed_batches=5,
                    cooldown_seconds=120,
                )
                if not write_res["success"]:
                    raise RuntimeError(f"stream load失败批次: {write_res['failed_batches']}")

                progress["map_code_details"][map_code] = {
                    "status": "success",
                    "success_time": now_str(),
                    "inserted": int(write_res["inserted_rows"]),
                    "error": "",
                }
                if map_code not in completed_set:
                    progress["completed_map_codes"].append(map_code)
                    completed_set.add(map_code)
                log_map_event(
                    map_log_path,
                    f"[map_stage] 成功 map_code={map_code}，写入={write_res['inserted_rows']}，耗时={time.time() - one_map_start:.2f}秒",
                )
            except Exception as e:
                progress["map_code_details"][map_code] = {
                    "status": "failed",
                    "success_time": "",
                    "inserted": 0,
                    "error": str(e)[:500],
                }
                log_map_event(
                    map_log_path,
                    f"[map_stage] 失败 map_code={map_code}，error={str(e)[:200]}，耗时={time.time() - one_map_start:.2f}秒",
                )
            finally:
                progress["processing_map_code"] = None
                save_progress(progress_path, progress)
        print(f"[耗时] map_stage: {time.time() - map_stage_start:.2f} 秒")

        failed_map_codes = [
            code for code, detail in progress.get("map_code_details", {}).items() if detail.get("status") == "failed"
        ]
        log_map_event(
            map_log_path,
            f"[map_stage] 汇总 完成={len(progress.get('completed_map_codes', []))} 失败={len(failed_map_codes)}",
        )

        print("[脚本1] 完成")
        print(f"  1) {cls_table}")
        print(f"  2) {map_table}")
    except Exception as e:
        log_map_event(map_log_path, f"[脚本1] 失败: {str(e)[:300]}")
        raise

    finally:
        total_seconds = time.time() - start_time
        print(f"【耗时】脚本总耗时：{total_seconds:.2f}秒（约{total_seconds/60:.2f}分钟）")
        engine_target.dispose()
        engine_source.dispose()


if __name__ == "__main__":
    main()

#脚本一按照map_code,tax_id,invoice_type和year去重
