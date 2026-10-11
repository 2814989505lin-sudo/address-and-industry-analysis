from __future__ import annotations

import os
import re
import unicodedata

from sqlalchemy import create_engine, text


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

DB_TARGET=os.environ['SR_TARGET_DSN']

# 源表和目标表名称固定为本次任务指定值，不做额外参数化。
SOURCE_TABLE = "fintech_stat.company_address_yuexiu_ly"
SOURCE_COMPANY_TABLE = "fintech_data_lake.jwy_ods__mongo_pazxdb__company_base__si"
CLEAN_TABLE = "fintech_stat.company_address_yuexiu_clean_ly"
ROUGH_TABLE = "fintech_stat.company_address_yuexiu_rough_ly"

# 批量解析时的进度打印间隔。
PROGRESS_PRINT_INTERVAL = 100

# 批量写库时每批提交的行数。
INSERT_BATCH_SIZE = 500

# 输入字段名。
INPUT_COLUMN_CREDIT_NO = "credit_no"
INPUT_COLUMN_COMPANY_NAME = "company_name"
INPUT_COLUMN_COMPANY_ADDRESS = "company_address"

# 结构化输出字段。
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

# 最终结果表的基础字段顺序。
RESULT_FIELDS = [
    INPUT_COLUMN_CREDIT_NO,
    INPUT_COLUMN_COMPANY_NAME,
    INPUT_COLUMN_COMPANY_ADDRESS,
] + STRUCTURED_FIELDS

# rough 表比 clean 表多一个原因字段。
ROUGH_REASON_FIELD = "rough_reasons"

# 下面这个映射专门用于 SQL 插入。
# `additional_information` 直接作为统一字段名，Python 和 SQL 都共用这套命名。
DB_PARAM_NAME_MAP = {
    "credit_no": "credit_no",
    "company_name": "company_name",
    "company_address": "company_address",
    "province": "province",
    "city": "city",
    "district": "district",
    "town_street": "town_street",
    "road_name": "road_name",
    "road_no": "road_no",
    "entity_name": "entity_name",
    "additional_information": "additional_information",
    "rough_reasons": "rough_reasons",
}

# 道路后缀词表，直接沿用现有解析脚本的口径。
ROAD_SUFFIXES = [
    "路",
    "街",
    "道",
    "巷",
    "弄",
    "大道",
    "支路",
]

# 园区 / 楼宇等实体后缀词表，直接沿用现有解析与 rough 规则脚本。
COMPOUND_SUFFIXES = [
    "城",
    "港",
    "园",
    "大厦",
    "广场",
    "中心",
    "科技园",
    "工业园",
    "都",
    "市场",
    "办公楼",
    "经济区",
    "开发区",
    "经济社团",
    "花园",
    "队",
    "经济社",
    "村",
    "社区",
    "场地",
]

PROVINCE_ALIAS_PREFIXES = [
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "香港", "澳门",
]

ROAD_NAME_LONG_THRESHOLD = 12

COMPOUND_BAD_TOKENS = [
    "房",
    "室",
    "层",
    "档口",
    "铺",
    "自编",
    "之一",
    "负",
]

COMPOUND_BAD_STARTS = [
    "仅",
    "及",
    "之",
]

COMPOUND_SINGLE_CHAR_FRAGMENTS = [
    "城",
    "园",
    "港",
    "都",
]

COMPOUND_BAD_ENDS = [
    "仅",
    "及",
    "自编",
    "之一",
    "使用",
]

MULTI_VALUE_SUSPICIOUS_FIELDS = [
    "province",
    "city",
    "district",
    "town_street",
    "road_name",
    "entity_name",
]

CHINESE_NUMERAL_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

INVALID_STATUS_PATTERNS = [
    "%注销%",
    "%吊销%",
    "%撤销%",
    "%停业%",
    "%迁移%",
    "%已废止%",
    "%迁出%",
    "%转企%",
    "%歇业%",
    "%问题%",
    "%吊%",
    "%注%",
    "%除名%",
    "%处罚%",
    "%解散%",
    "%休眠%",
    "%关闭%",
]


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def normalize_input_value(value):
    """把数据库读出来的值统一成便于处理的基础形态。"""
    if value is None:
        return None
    return str(value).strip()


def build_invalid_status_clause(alias="s"):
    """生成异常 company_status 过滤条件，口径与脚本 3.2 保持一致。"""
    conditions = [
        f"COALESCE({alias}.company_status, '') LIKE '{pattern}'"
        for pattern in INVALID_STATUS_PATTERNS
    ]
    return " OR ".join(conditions)


def is_blank_text(value):
    """判断一个值是否为空文本。"""
    if value is None:
        return True
    return str(value).strip() == ""


def chinese_numeral_to_int(text_value):
    """把简单中文数字转成整数。"""
    if not text_value:
        return None

    if text_value == "十":
        return 10

    if "十" in text_value:
        parts = text_value.split("十")
        tens = CHINESE_NUMERAL_MAP.get(parts[0], 1) if parts[0] else 1
        ones = CHINESE_NUMERAL_MAP.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones

    digits = []
    for char in text_value:
        if char not in CHINESE_NUMERAL_MAP:
            return None
        digits.append(str(CHINESE_NUMERAL_MAP[char]))
    return int("".join(digits))


def normalize_numbers(text_value):
    """统一地址里的中文数字表达。"""

    def replace_chinese_number(match):
        value = chinese_numeral_to_int(match.group(1))
        if value is None:
            return match.group(0)
        return f"{value}{match.group(2)}"

    return re.sub(
        r"([零〇一二三四五六七八九十]+)(街|路|道|巷|弄|号|栋|座|幢|楼|层|室|房|铺|期|区)",
        replace_chinese_number,
        text_value,
    )


def normalize_symbols(text_value):
    """统一地址里的全角符号、括号和空白字符。"""
    normalized_text = unicodedata.normalize("NFKC", text_value)
    normalized_text = normalized_text.replace("（", "(").replace("）", ")")
    normalized_text = normalized_text.replace("；", ";").replace("，", ",").replace("：", ":")
    normalized_text = re.sub(r"\s+", "", normalized_text)
    return normalized_text


def normalize_text(raw_address):
    """对原始地址做标准化，方便后续正则识别。"""
    normalized_text = (raw_address or "").strip()
    normalized_text = normalize_symbols(normalized_text)
    normalized_text = normalize_numbers(normalized_text)
    return normalized_text


def make_part(field_type, text_value, start, end, source):
    """构造一个识别出来的地址片段。"""
    return {
        "field_type": field_type,
        "text": text_value,
        "start": start,
        "end": end,
        "source": source,
    }


def append_part(parts, field_type, text_value, start, end, source):
    """把识别片段追加到列表里。"""
    if start >= end or not text_value:
        return
    parts.append(make_part(field_type, text_value, start, end, source))


def build_empty_result(record):
    """先构造一行空结果，后面再逐个字段回填。"""
    result = {
        "credit_no": record.get("credit_no"),
        "company_name": record.get("company_name"),
        "company_address": record.get("company_address"),
    }
    for field_name in STRUCTURED_FIELDS:
        result[field_name] = None
    return result


def merge_part_texts(parts):
    """把同类片段按出现顺序去重后拼成一个字段值。"""
    if not parts:
        return None

    merged_texts = []
    seen = set()
    for part in sorted(parts, key=lambda item: item["start"]):
        text_value = part["text"]
        if text_value not in seen:
            seen.add(text_value)
            merged_texts.append(text_value)

    if not merged_texts:
        return None
    return ",".join(merged_texts)


def get_field_parts(parts, field_type, min_start=0):
    """获取某个字段类型的全部片段。"""
    return [
        part
        for part in parts
        if part["field_type"] == field_type and part["start"] >= min_start
    ]


def spans_to_text(text_value, spans):
    """根据已消费的区间，拼回剩余文本。"""
    if not spans:
        return text_value

    consumed = [False] * len(text_value)
    for start, end in spans:
        for index in range(max(0, start), min(len(text_value), end)):
            consumed[index] = True

    return "".join(char for index, char in enumerate(text_value) if not consumed[index])


def cleanup_fragment(text_value):
    """清理残余文本两侧的分隔符，避免留下碎片标点。"""
    if text_value is None:
        return None
    cleaned_text = text_value.strip(";,()[]，；、")
    cleaned_text = re.sub(r";{2,}", ";", cleaned_text)
    cleaned_text = cleaned_text.strip()
    return cleaned_text or None


def trim_compound_prefix(compound_text, road_name, road_no):
    """如果实体名前缀带了道路和门牌号，就把这段裁掉。"""
    if not compound_text or not road_name or not road_no:
        return compound_text

    prefix = f"{road_name}{road_no}"
    if not compound_text.startswith(prefix):
        return compound_text

    trimmed_text = compound_text[len(prefix):].lstrip("-,，,;； ")
    return trimmed_text or compound_text


def trim_compound_sub_address_prefix(compound_text):
    """裁掉实体名前残留的院号/子门牌前缀，保留后面的合法商业体名。"""
    if not compound_text:
        return compound_text

    match = re.match(r"^(?:(?:大院|院)?\d+号)(.+)$", compound_text)
    if not match:
        return compound_text

    trimmed_text = match.group(1).lstrip("-,，,;； ")
    if not trimmed_text:
        return compound_text

    if not any(suffix in trimmed_text for suffix in COMPOUND_SUFFIXES):
        return compound_text

    return trimmed_text


def trim_compound_admin_prefix(compound_text, province, city, district):
    """如果实体名前缀带了省、市、区，也把这些前缀裁掉。"""
    if not compound_text:
        return compound_text

    trimmed_text = compound_text
    admin_prefixes = [province, city, district]
    alias_prefixes = []

    if province:
        alias_prefixes.append(re.sub(r"(省|自治区|特别行政区)$", "", province))
    if city:
        alias_prefixes.append(re.sub(r"市$", "", city))
    alias_prefixes.extend(PROVINCE_ALIAS_PREFIXES)

    for prefix in admin_prefixes + alias_prefixes:
        if prefix and trimmed_text.startswith(prefix):
            trimmed_text = trimmed_text[len(prefix):].lstrip("-,，,;； ")

    return trimmed_text or compound_text


def trim_road_prefix(road_text, province, city, district, entity_name):
    """如果道路名前缀带了行政区或实体名，就把前缀裁掉。"""
    if not road_text:
        return road_text

    trimmed_text = road_text
    for prefix in [province, city, district, entity_name]:
        if prefix and trimmed_text.startswith(prefix):
            trimmed_text = trimmed_text[len(prefix):].lstrip("-,，,;； ")

    return trimmed_text or road_text


def split_road_phase_prefix(road_text):
    """把“第N期 / N期”这类前缀从道路名中拆出来。"""
    if not road_text:
        return None, road_text

    match = re.match(r"^((?:第)?[A-Za-z0-9一二三四五六七八九十]+期)(.+)$", road_text)
    if not match:
        return None, road_text

    phase_text = match.group(1)
    trimmed_road_text = match.group(2).lstrip("-,，,;； ")
    if not trimmed_road_text:
        return None, road_text

    return phase_text, trimmed_road_text


def merge_compound_phase(compound_text, phase_texts):
    """把从 road_name 拆出来的期数并回 entity_name。"""
    if not compound_text or not phase_texts or "," in compound_text:
        return compound_text

    merged_text = compound_text
    for phase_text in phase_texts:
        if phase_text and phase_text not in merged_text:
            merged_text = f"{merged_text}{phase_text}"

    return merged_text


def move_compound_direction_to_road(road_text, compound_text):
    """把实体名前缀里的“南段/北段”等方向词移回道路名。"""
    if not road_text or not compound_text or "," in compound_text:
        return road_text, compound_text

    match = re.match(r"^(南段|北段|东段|西段|中段)(.+)$", compound_text)
    if not match:
        return road_text, compound_text

    direction_text = match.group(1)
    trimmed_compound_text = match.group(2).lstrip("-,，,;； ")
    if not trimmed_compound_text:
        return road_text, compound_text

    if not road_text.endswith(direction_text):
        road_text = f"{road_text}{direction_text}"

    return road_text, trimmed_compound_text


def split_values(text_value):
    """把逗号拼接的多值字段拆开。"""
    if not text_value:
        return []
    return [item.strip() for item in str(text_value).split(",") if item.strip()]


def contains_obvious_compound_suffix(address):
    """地址中是否出现明显的楼宇/园区后缀。"""
    return any(suffix in (address or "") for suffix in COMPOUND_SUFFIXES)


def has_suspicious_multi_value(row):
    """不应为多值的字段里，如果出现多个值，就判为 rough。"""
    for field_name in MULTI_VALUE_SUSPICIOUS_FIELDS:
        values = split_values(row.get(field_name))
        if len(values) > 1:
            return True
    return False


def compound_contains_road(row):
    """如果 entity_name 吃进了 road_name 或 road_name+road_no，说明切分边界有问题。"""
    compound_values = split_values(row.get("entity_name"))
    road_values = split_values(row.get("road_name"))
    road_no_values = split_values(row.get("road_no"))

    for compound_value in compound_values:
        for road_value in road_values:
            if road_value and road_value in compound_value:
                return True
            for road_no_value in road_no_values:
                if road_value and road_no_value and f"{road_value}{road_no_value}" in compound_value:
                    return True
    return False


def compound_starts_with_generic_word(row):
    """如果实体名直接以通用后缀开头，通常只是一个残片。"""
    compound_values = split_values(row.get("entity_name"))
    if not compound_values:
        return False

    for compound_value in compound_values:
        if any(compound_value.startswith(prefix) for prefix in COMPOUND_SUFFIXES):
            return True

    return False


def has_suspicious_road_name(row):
    """如果一个 road_name 同时带多个道路后缀，通常是错误拼接。"""
    road_values = split_values(row.get("road_name"))
    road_suffixes = ["大道", "支路", "路", "街", "道", "巷", "弄"]

    for road_value in road_values:
        matched_suffixes = []
        remaining_text = road_value

        for suffix in road_suffixes:
            if suffix in remaining_text:
                matched_suffixes.append(suffix)
                remaining_text = remaining_text.replace(suffix, "")

        if len(matched_suffixes) >= 2:
            return True

    return False


def road_contains_admin(row):
    """如果 road_name 吃进了 province/city/district，说明边界偏大。"""
    road_values = split_values(row.get("road_name"))
    admin_values = [
        normalize_input_value(row.get("province")),
        normalize_input_value(row.get("city")),
        normalize_input_value(row.get("district")),
    ]
    admin_values = [value for value in admin_values if value]

    if not road_values or not admin_values:
        return False

    for road_value in road_values:
        for admin_value in admin_values:
            if admin_value in road_value:
                return True

    return False


def collect_district_field_rough_reasons(row):
    """按单个 district 值检查新增 rough 规则。"""
    reasons = []
    district_values = split_values(row.get("district"))

    for district_value in district_values:
        if not district_value.endswith("区"):
            reasons.append("district不是以区结尾")
            break

    for district_value in district_values:
        if len(district_value) < 3 or len(district_value) > 4:
            reasons.append("district长度异常")
            break

    return reasons


def collect_road_name_field_rough_reasons(row):
    """按单个 road_name 值检查新增 rough 规则。"""
    reasons = []
    road_values = split_values(row.get("road_name"))

    for road_value in road_values:
        if len(road_value) < 3:
            reasons.append("road_name长度小于3")
            break

    for road_value in road_values:
        if re.match(r"^[0-9]", road_value):
            reasons.append("road_name以阿拉伯数字开头")
            break

    for road_value in road_values:
        if road_value.endswith("通道"):
            reasons.append("road_name以通道结尾")
            break

    return reasons


def collect_entity_name_field_rough_reasons(row):
    """按单个 entity_name 值检查新增 rough 规则。"""
    reasons = []
    compound_values = split_values(row.get("entity_name"))

    for compound_value in compound_values:
        if len(compound_value) < 3:
            reasons.append("entity_name长度小于3")
            break

    for compound_value in compound_values:
        if compound_value.startswith(("(", "（")):
            reasons.append("entity_name以括号开头")
            break

    for compound_value in compound_values:
        if re.match(r"^[A-Za-z]", compound_value):
            reasons.append("entity_name以英文字母开头")
            break

    for compound_value in compound_values:
        if compound_value.startswith("的"):
            reasons.append("entity_name以的开头")
            break

    for compound_value in compound_values:
        if compound_value.endswith("办公楼"):
            reasons.append("entity_name以办公楼结尾")
            break

    return reasons


def has_suspicious_entity_name(row):
    """按现有规则判断 entity_name 是否看起来不像一个有效实体名。"""
    compound_values = split_values(row.get("entity_name"))
    if not compound_values:
        return False

    for compound_value in compound_values:
        if compound_value in COMPOUND_SINGLE_CHAR_FRAGMENTS:
            return True
        if compound_value in COMPOUND_SUFFIXES:
            return True
        if not any(suffix in compound_value for suffix in COMPOUND_SUFFIXES):
            return True
        if any(token in compound_value for token in COMPOUND_BAD_TOKENS):
            return True
        if compound_value[:1].isdigit():
            return True
        if any(compound_value.startswith(token) for token in COMPOUND_BAD_STARTS):
            return True
        if any(compound_value.endswith(token) for token in COMPOUND_BAD_ENDS):
            return True

    return False


def has_suspicious_brackets(row):
    """如果实体名括号不成对，通常说明切分截断了。"""
    value = normalize_input_value(row.get("entity_name"))
    if not value:
        return False
    return value.count("(") != value.count(")")


def build_insert_payload(row, include_rough_reasons):
    """把结果行转成数据库插入参数字典。"""
    payload = {}
    for field_name in RESULT_FIELDS:
        payload[DB_PARAM_NAME_MAP[field_name]] = row.get(field_name)

    if include_rough_reasons:
        payload[DB_PARAM_NAME_MAP[ROUGH_REASON_FIELD]] = row.get(ROUGH_REASON_FIELD)

    return payload


def chunk_rows(rows, chunk_size):
    """把大列表切成固定大小的小批次，便于分批写库。"""
    for index in range(0, len(rows), chunk_size):
        yield rows[index:index + chunk_size]


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------

def load_source_records():
    """读取脚本 1 的输入数据，并前置套用脚本 3.2 的清理逻辑。"""
    engine = create_engine(DB_TARGET)
    invalid_status_clause = build_invalid_status_clause("s")
    sql = text(
        f"""
        SELECT
            credit_no,
            company_name,
            company_address
        FROM (
            SELECT
                t.credit_no,
                t.company_name,
                t.company_address,
                ROW_NUMBER() OVER (
                    PARTITION BY t.credit_no
                    ORDER BY
                        CASE
                            WHEN TRIM(COALESCE(t.company_name, '')) <> ''
                             AND TRIM(COALESCE(s.company_name, '')) <> ''
                             AND t.company_name = s.company_name THEN 0
                            ELSE 1
                        END,
                        CASE
                            WHEN TRIM(COALESCE(t.company_address, '')) <> ''
                             AND TRIM(COALESCE(s.company_address, '')) <> ''
                             AND t.company_address = s.company_address THEN 0
                            ELSE 1
                        END,
                        CASE WHEN s.update_time IS NULL THEN 1 ELSE 0 END,
                        s.update_time DESC,
                        t.company_name,
                        t.company_address
                ) AS target_rn
            FROM {SOURCE_TABLE} t
            INNER JOIN (
                SELECT
                    credit_no,
                    company_name,
                    company_address,
                    update_time
                FROM (
                    SELECT
                        s.credit_no,
                        s.company_name,
                        s.company_address,
                        s.update_time,
                        s.id,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.credit_no
                            ORDER BY
                                CASE WHEN s.update_time IS NULL THEN 1 ELSE 0 END,
                                s.update_time DESC,
                                CASE WHEN s.id IS NULL THEN 1 ELSE 0 END,
                                s.id DESC
                        ) AS source_rn
                    FROM {SOURCE_COMPANY_TABLE} s
                    INNER JOIN (
                        SELECT DISTINCT credit_no
                        FROM {SOURCE_TABLE}
                        WHERE credit_no IS NOT NULL
                          AND TRIM(credit_no) <> ''
                    ) tc
                        ON s.credit_no = tc.credit_no
                    WHERE s.credit_no IS NOT NULL
                      AND TRIM(s.credit_no) <> ''
                      AND COALESCE(s.use_flag, 1) = 0
                      AND NOT ({invalid_status_clause})
                ) latest_valid_source
                WHERE source_rn = 1
            ) s
                ON t.credit_no = s.credit_no
            WHERE t.credit_no IS NOT NULL
              AND TRIM(t.credit_no) <> ''
        ) filtered_target
        WHERE target_rn = 1
        """
    )

    records = []
    total_rows = 0

    with engine.connect() as connection:
        result = connection.execute(sql)
        for row in result.mappings():
            total_rows += 1
            records.append(
                {
                    "credit_no": normalize_input_value(row.get("credit_no")),
                    "company_name": normalize_input_value(row.get("company_name")),
                    "company_address":normalize_input_value(row.get("company_address")),
                }
            )

    return records, total_rows


def extract_parts(normalized_address):
    """从标准化后的地址里提取全部可识别片段。"""
    parts = []
    cursor = 0

    # 行政区采用顺序识别。
    # 这样可以尽量减少“后面的同名片段被误当成行政区”的问题。
    province_match = re.match(
        r"((?:[^省]+省)|(?:内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|新疆维吾尔自治区)|(?:香港特别行政区|澳门特别行政区))",
        normalized_address[cursor:],
    )
    if province_match:
        start = cursor + province_match.start()
        end = cursor + province_match.end()
        append_part(parts, "province", province_match.group(1), start, end, "行政区划")
        cursor = end

    city_match = re.match(r"([^市]{2,12}市)", normalized_address[cursor:])
    if city_match:
        start = cursor + city_match.start()
        end = cursor + city_match.end()
        append_part(parts, "city", city_match.group(1), start, end, "行政区划")
        cursor = end

    district_match = re.match(r"([^区县市]{1,12}(?:区|县|市))", normalized_address[cursor:])
    if district_match:
        start = cursor + district_match.start()
        end = cursor + district_match.end()
        append_part(parts, "district", district_match.group(1), start, end, "行政区划")
        cursor = end

    town_match = re.match(r"([\u4e00-\u9fa5]{1,8}(?:镇|街道|街))", normalized_address[cursor:])
    if town_match:
        start = cursor + town_match.start()
        end = cursor + town_match.end()
        append_part(parts, "town_street", town_match.group(1), start, end, "镇街识别")
        cursor = end

    # 园区 / 楼宇实体和道路采用全局扫描。
    # 原因是它们在真实地址中不一定严格出现在同一个相对位置。
    compound_suffix_pattern = "|".join(sorted(COMPOUND_SUFFIXES, key=len, reverse=True))
    compound_pattern = re.compile(
        rf"([\u4e00-\u9fa5A-Za-z0-9()\-]{{2,40}}?(?:{compound_suffix_pattern})(?:[A-Za-z0-9一二三四五六七八九十]+期)?(?:[A-Za-z0-9一二三四五六七八九十]+区)?)"
    )
    for match in compound_pattern.finditer(normalized_address[cursor:]):
        start = cursor + match.start()
        end = cursor + match.end()
        append_part(parts, "entity_name", match.group(1), start, end, "园区楼宇识别")

    road_suffix_pattern = "|".join(sorted(ROAD_SUFFIXES, key=len, reverse=True))
    road_pattern = re.compile(rf"([\u4e00-\u9fa5A-Za-z0-9()\-]{{1,20}}?(?:{road_suffix_pattern}))")
    for match in road_pattern.finditer(normalized_address[cursor:]):
        start = cursor + match.start()
        end = cursor + match.end()
        road_text = match.group(1)
        append_part(parts, "road_name", road_text, start, end, "道路识别")

        road_no_match = re.match(
            r"([0-9A-Za-z]+(?:(?:[、,，/\-]|至)[0-9A-Za-z]+)*(?:号|档))",
            normalized_address[end:],
        )
        if road_no_match:
            number_start = end + road_no_match.start()
            number_end = end + road_no_match.end()
            append_part(parts, "road_no", road_no_match.group(1), number_start, number_end, "道路编号识别")

    return parts


def assemble_fields(parts):
    """把识别出来的片段组装成最终结构化字段。"""
    resolved_fields = {field_name: None for field_name in STRUCTURED_FIELDS}
    consumed_spans = []
    road_phase_texts = []

    # 先把顺序明确的行政区层级填上。
    for field_name in ["province", "city", "district", "town_street"]:
        field_parts = get_field_parts(parts, field_name)
        if field_parts:
            resolved_fields[field_name] = merge_part_texts(field_parts)
            consumed_spans.extend((part["start"], part["end"]) for part in field_parts)

    # 道路和门牌号只从行政区后面继续找，避免回头误扫到前面的文字。
    min_start = 0
    for field_name in ["district", "town_street"]:
        field_parts = get_field_parts(parts, field_name)
        if field_parts:
            min_start = max(min_start, max(part["end"] for part in field_parts))

    road_parts = get_field_parts(parts, "road_name", min_start)
    if road_parts:
        entity_name_prefix = None
        compound_prefix_parts = get_field_parts(parts, "entity_name", min_start)
        if compound_prefix_parts:
            entity_name_prefix = merge_part_texts(compound_prefix_parts)

        trimmed_road_parts = []
        for part in road_parts:
            trimmed_text = trim_road_prefix(
                part["text"],
                resolved_fields["province"],
                resolved_fields["city"],
                resolved_fields["district"],
                entity_name_prefix,
            )
            if entity_name_prefix:
                phase_text, trimmed_text = split_road_phase_prefix(trimmed_text)
                if phase_text and phase_text not in road_phase_texts:
                    road_phase_texts.append(phase_text)
            trimmed_road_parts.append({**part, "text": trimmed_text})

        resolved_fields["road_name"] = merge_part_texts(trimmed_road_parts)
        consumed_spans.extend((part["start"], part["end"]) for part in road_parts)

        road_no_parts = get_field_parts(parts, "road_no", min_start)
        if road_no_parts:
            resolved_fields["road_no"] = merge_part_texts(road_no_parts)
            consumed_spans.extend((part["start"], part["end"]) for part in road_no_parts)

    # 再组装实体名，并把前缀里的省市区/道路号等裁掉。
    compound_parts = get_field_parts(parts, "entity_name", min_start)
    if compound_parts:
        trimmed_compound_parts = []
        for part in compound_parts:
            trimmed_text = trim_compound_prefix(
                part["text"],
                resolved_fields["road_name"],
                resolved_fields["road_no"],
            )
            trimmed_text = trim_compound_sub_address_prefix(trimmed_text)
            trimmed_text = trim_compound_admin_prefix(
                trimmed_text,
                resolved_fields["province"],
                resolved_fields["city"],
                resolved_fields["district"],
            )
            trimmed_compound_parts.append({**part, "text": trimmed_text})

        resolved_fields["entity_name"] = merge_part_texts(trimmed_compound_parts)
        resolved_fields["entity_name"] = merge_compound_phase(
            resolved_fields["entity_name"],
            road_phase_texts,
        )
        resolved_fields["road_name"], resolved_fields["entity_name"] = move_compound_direction_to_road(
            resolved_fields["road_name"],
            resolved_fields["entity_name"],
        )
        consumed_spans.extend((part["start"], part["end"]) for part in compound_parts)

    resolved_fields["_consumed_spans"] = consumed_spans
    return resolved_fields


def split_residual(normalized_address, resolved_fields):
    """把未消费掉的剩余文本写入 additional_information。"""
    consumed_spans = resolved_fields.get("_consumed_spans", [])
    residual_text = spans_to_text(normalized_address, consumed_spans)
    residual_text = cleanup_fragment(residual_text) or ""

    if not residual_text:
        return None

    remaining_fragments = [cleanup_fragment(fragment) for fragment in re.split(r"[;,]", residual_text)]
    remaining_fragments = [fragment for fragment in remaining_fragments if fragment]
    return ",".join(remaining_fragments) if remaining_fragments else None


def parse_one(record):
    """解析单条地址。"""
    result = build_empty_result(record)
    normalized_address = normalize_text(record.get("company_address"))
    parts = extract_parts(normalized_address)
    resolved_fields = assemble_fields(parts)
    additional_information = split_residual(normalized_address, resolved_fields)

    for field_name in STRUCTURED_FIELDS:
        if field_name in resolved_fields and not field_name.startswith("_"):
            result[field_name] = resolved_fields[field_name]

    result["additional_information"] = additional_information
    return result


def parse_batch(records):
    """批量解析全部地址，并打印进度。"""
    results = []
    total_count = len(records)

    if total_count:
        print(f"开始解析，共 {total_count} 条记录。")

    for index, record in enumerate(records, start=1):
        results.append(parse_one(record))

        if index % PROGRESS_PRINT_INTERVAL == 0 or index == total_count:
            print(f"解析进度: {index}/{total_count}")

    return results


def collect_rough_reasons(row):
    """按现有 Excel 版口径收集 rough 命中原因。"""
    reasons = []

    entity_name = normalize_input_value(row.get("entity_name"))
    road_name = normalize_input_value(row.get("road_name"))
    road_no = normalize_input_value(row.get("road_no"))
    district = normalize_input_value(row.get("district"))
    address = normalize_input_value(row.get("company_address"))

    if compound_contains_road(row):
        reasons.append("entity_name包含road_name或road_name+road_no")

    if compound_starts_with_generic_word(row):
        reasons.append("entity_name以通用实体词开头")

    reasons.extend(collect_district_field_rough_reasons(row))

    if road_name and len(road_name) > ROAD_NAME_LONG_THRESHOLD:
        reasons.append("road_name很长")

    reasons.extend(collect_road_name_field_rough_reasons(row))

    if has_suspicious_road_name(row):
        reasons.append("road_name同时包含多个道路后缀")

    if road_contains_admin(row):
        reasons.append("road_name包含完整的province或city或district")

    if not road_no or not road_name or not district:
        reasons.append("road_no或road_name或district缺失")

    if not entity_name and contains_obvious_compound_suffix(address):
        reasons.append("entity_name缺失但地址里有明显实体后缀")

    reasons.extend(collect_entity_name_field_rough_reasons(row))

    if has_suspicious_entity_name(row):
        reasons.append("entity_name看起来不像实体名称")

    if has_suspicious_brackets(row):
        reasons.append("输出字段里有异常括号")

    if has_suspicious_multi_value(row):
        reasons.append("不该多值的字段出现多个值")

    return reasons


def split_clean_and_rough(rows, verbose=True):
    """把解析结果拆成 clean 和 rough 两类，并打印进度。"""
    clean_rows = []
    rough_rows = []
    total_count = len(rows)
    
    if verbose and total_count:
        print(f"开始分类 clean/rough，共 {total_count} 条记录。")
    
    for index, row in enumerate(rows, start=1):
        reasons = collect_rough_reasons(row)
        if reasons:
            row[ROUGH_REASON_FIELD] = "；".join(reasons)
            rough_rows.append(row)
        else:
            row[ROUGH_REASON_FIELD] = ""
            clean_rows.append(row)
        
        if verbose and (index % PROGRESS_PRINT_INTERVAL == 0 or index == total_count):
            print(f"分类进度: {index}/{total_count} (clean: {len(clean_rows)}, rough: {len(rough_rows)})")
    
    if verbose:
        print(f"分类完成: clean {len(clean_rows)} 条, rough {len(rough_rows)} 条")
    
    return clean_rows, rough_rows


def create_result_tables():
    """创建 clean / rough 结果表。"""
    engine = create_engine(DB_TARGET)

    clean_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {CLEAN_TABLE} (
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
    DUPLICATE KEY(credit_no)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    rough_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {ROUGH_TABLE} (
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
        rough_reasons VARCHAR(1000) COMMENT '进入rough的原因'
    ) ENGINE=OLAP
    DUPLICATE KEY(credit_no)
    DISTRIBUTED BY HASH(credit_no) BUCKETS 10
    PROPERTIES ("replication_num" = "1");
    """

    with engine.begin() as connection:
        connection.execute(text(clean_table_sql))
        connection.execute(text(rough_table_sql))


def insert_rows(table_name, rows, include_rough_reasons, verbose=True):
    """把结果分批写入指定目标表，并打印进度。"""
    if not rows:
        if verbose:
            print(f"表 {table_name} 没有数据需要写入。")
        return
    
    total_count = len(rows)
    
    column_sql_parts = [
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
    value_sql_parts = [
        ":credit_no",
        ":company_name",
        ":company_address",
        ":province",
        ":city",
        ":district",
        ":town_street",
        ":road_name",
        ":road_no",
        ":entity_name",
        ":additional_information",
    ]

    if include_rough_reasons:
        column_sql_parts.append("rough_reasons")
        value_sql_parts.append(":rough_reasons")

    insert_sql = text(
        f"""
        INSERT INTO {table_name} (
            {", ".join(column_sql_parts)}
        ) VALUES (
            {", ".join(value_sql_parts)}
        )
        """
    )

    engine = create_engine(DB_TARGET)
    
    if verbose:
        print(f"开始写入表 {table_name}，共 {total_count} 条记录，批次大小: {INSERT_BATCH_SIZE}")
    
    batch_num = 0
    rows_written = 0
    
    with engine.begin() as connection:
        for batch in chunk_rows(rows, INSERT_BATCH_SIZE):
            batch_num += 1
            payload = [build_insert_payload(row, include_rough_reasons) for row in batch]
            connection.execute(insert_sql, payload)
            rows_written += len(batch)
            
            if verbose:
                print(f"表 {table_name} 写入进度: {rows_written}/{total_count} (批次 {batch_num})")
    
    if verbose:
        print(f"表 {table_name} 写入完成，共写入 {rows_written} 条记录。")


def count_rows(table_name):
    """统计目标表当前行数，用于跑完后的简单校验。"""
    engine = create_engine(DB_TARGET)
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT COUNT(1) AS row_count FROM {table_name}"))
        row = result.mappings().first()
        return int(row["row_count"]) if row else 0

# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------
def main():
    """程序入口。"""

    records, total_rows = load_source_records()
    parsed_rows = parse_batch(records)
    clean_rows, rough_rows = split_clean_and_rough(parsed_rows)

    create_result_tables()
    insert_rows(CLEAN_TABLE, clean_rows, include_rough_reasons=False)
    insert_rows(ROUGH_TABLE, rough_rows, include_rough_reasons=True)

    clean_count = count_rows(CLEAN_TABLE)
    rough_count = count_rows(ROUGH_TABLE)

    print(f"源表: {SOURCE_TABLE}")
    print(f"总读取行数: {total_rows}")
    print(f"有效输入行数: {len(records)}")
    print(f"clean 结果数: {len(clean_rows)}")
    print(f"rough 结果数: {len(rough_rows)}")
    print(f"clean 表实际行数: {clean_count}")
    print(f"rough 表实际行数: {rough_count}")
    print(f"clean 表: {CLEAN_TABLE}")
    print(f"rough 表: {ROUGH_TABLE}")

    if len(records) != len(clean_rows) + len(rough_rows):
        raise ValueError("clean 和 rough 的总数与有效输入行数不一致。")

    if clean_count != len(clean_rows):
        raise ValueError("clean 表写入行数与内存结果不一致。")

    if rough_count != len(rough_rows):
        raise ValueError("rough 表写入行数与内存结果不一致。")


if __name__ == "__main__":
    main()
