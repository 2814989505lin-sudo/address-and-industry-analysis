from __future__ import annotations

from json import dumps
from pathlib import Path
import re
import unicodedata

from openpyxl import Workbook, load_workbook


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

# 测试数据文件名，当前开发和验证都基于这个 Excel 文件。
TEST_DATA_FILE_NAME = "广州商圈_20260227.xlsx"
# 测试数据文件的完整路径，默认与当前脚本放在同一目录下。
TEST_DATA_PATH = Path(__file__).with_name(TEST_DATA_FILE_NAME)
# 测试数据所在的工作表名称。
TEST_DATA_SHEET_NAME = "Sheet1"
# 解析结果输出文件名。
OUTPUT_FILE_NAME = "output_customer.xlsx"
# 解析结果输出路径。
OUTPUT_PATH = Path(__file__).with_name(OUTPUT_FILE_NAME)
# 批量处理时的进度打印间隔，例如 100 表示每处理 100 条打印一次。
PROGRESS_PRINT_INTERVAL = 100

# 公司名称是核心输入之一，主要用于人工排查和结果核对。
INPUT_COLUMN_COMPANY_NAME = "company_name"
# 地址是核心输入之一，是地址结构化解析的主输入文本。
INPUT_COLUMN_COMPANY_ADDRESS = "company_address"

# Excel 中当前包含的全部输入列。
INPUT_COLUMNS = [
    INPUT_COLUMN_COMPANY_NAME,
    INPUT_COLUMN_COMPANY_ADDRESS,
]

# 业务字段名到 Excel 原始列名的映射，避免后续代码中直接写死中文列名。
INPUT_COLUMN_MAP = {
    "company_name": INPUT_COLUMN_COMPANY_NAME,
    "company_address": INPUT_COLUMN_COMPANY_ADDRESS,
}

# 标准结构化输出字段列表。
STRUCTURED_FIELDS = [
    # 省级行政区划，例如“广东省”。
    "province",
    # 地级市或直辖市，例如“广州市”。
    "city",
    # 区、县或县级市，例如“荔湾区”。
    "district",
    # 镇、街道、街镇等次级行政信息，例如“东沙街道”。
    "town_street",
    # 道路名称，例如“东沙大道”。
    "road_name",
    # 门牌号、弄、支路、附号等道路编号信息，例如“16号”“77、79号”“77-79号”“77至79号”。
    "road_no",
    # 园区、大厦、广场、中心等实体名称，例如“国际医药港”。
    "entity_name",
    # 前面字段抽取完后剩余的全部文本。
    "Additional Information",
]

# 最终结果中的附加信息字段，不属于地址实体本身，但用于追踪和评估。
RESULT_META_FIELDS = [
    # 原始公司名称，便于结果回溯和人工核查。
    "company_name",
    # 原始地址文本，便于与结构化结果对照。
    "company_address",
]

# 完整输出字段列表，由原始输入标识字段、结构化字段和结果附加字段共同组成。
RESULT_FIELDS = RESULT_META_FIELDS + STRUCTURED_FIELDS

# 兼容别名，当前与 STRUCTURED_FIELDS 保持一致。
FIELD_NAMES = STRUCTURED_FIELDS

# ------------------------------------------------------------
# 规则定义
# ------------------------------------------------------------
# TODO: 后续补充真实道路后缀词表。
ROAD_SUFFIXES = [
    "路",
    "街",
    "道",
    "巷",
    "弄",
    "大道",
    "支路",
]

# TODO: 后续补充真实园区 / 楼宇后缀词表。
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

# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

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


def chinese_numeral_to_int(text):
    """将简单中文数字转换为整数。"""
    if not text:
        return None

    if text == "十":
        return 10

    if "十" in text:
        parts = text.split("十")
        tens = CHINESE_NUMERAL_MAP.get(parts[0], 1) if parts[0] else 1
        ones = CHINESE_NUMERAL_MAP.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones

    digits = []
    for char in text:
        if char not in CHINESE_NUMERAL_MAP:
            return None
        digits.append(str(CHINESE_NUMERAL_MAP[char]))
    return int("".join(digits))


def normalize_numbers(text):
    """统一数字表达。"""
    normalized_text = text

    def replace_chinese_number(match):
        value = chinese_numeral_to_int(match.group(1))
        if value is None:
            return match.group(0)
        return f"{value}{match.group(2)}"

    normalized_text = re.sub(
        r"([零〇一二三四五六七八九十]+)(街|路|道|巷|弄|号|栋|座|幢|楼|层|室|房|铺|期|区)",
        replace_chinese_number,
        normalized_text,
    )
    return normalized_text


def normalize_symbols(text):
    """统一符号、空格、括号和连接符。"""
    normalized_text = unicodedata.normalize("NFKC", text) #把字符串做 Unicode标准化，全角变半角
    normalized_text = normalized_text.replace("（", "(").replace("）", ")") #中文括号换成英文括号
    normalized_text = normalized_text.replace("；", ";").replace("，", ",").replace("：", ":") #中文标点换成英文标点
    normalized_text = re.sub(r"\s+", "", normalized_text) #\s = 空白字符（空格 / tab / 换行）替换成 ‘+’ = 一个或多个 #"广州市 天河区  体育西路"→ "广州市天河区体育西路"
    return normalized_text


def normalize_text(raw_address):
    """对原始地址做标准化处理。"""
    normalized_text = raw_address.strip()
    normalized_text = normalize_symbols(normalized_text)
    normalized_text = normalize_numbers(normalized_text)
    return normalized_text


def make_part(field_type,text,start,end,source):
    """构造一个识别出来的文本片段。"""
    return {
        "field_type": field_type,
        "text": text,
        "start": start,
        "end": end,
        "source": source,
    }


def build_empty_result(company_name, company_address):
    """构造空结果模板。"""
    result = {
        "company_name": company_name,
        "company_address": company_address,
    }
    for field_name in FIELD_NAMES:
        result[field_name] = None
    return result


def append_part(parts,field_type,text,start,end,source):
    """向片段列表追加一个识别结果。"""
    if start >= end or not text:
        return
    parts.append(make_part(field_type, text, start, end, source))


def merge_part_texts(parts):
    """将多个片段文本按出现顺序去重拼接。"""
    if not parts:
        return None

    merged_texts = []
    seen = set()
    for part in sorted(parts, key=lambda item: item["start"]):
        text = part["text"]
        if text not in seen:
            seen.add(text)
            merged_texts.append(text)

    if not merged_texts:
        return None
    return ",".join(merged_texts)


def get_field_parts(parts, field_type, min_start=0):
    """获取某个字段的所有已识别片段。"""
    return [
        part
        for part in parts
        if part["field_type"] == field_type and part["start"] >= min_start
    ]


def spans_to_text(text, spans):
    """根据文本区间拼接剩余字符。"""
    if not spans:
        return text

    consumed = [False] * len(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(text), end)):
            consumed[index] = True

    return "".join(char for index, char in enumerate(text) if not consumed[index])


def cleanup_fragment(text):
    """清理残余文本中的首尾符号。"""
    cleaned_text = text.strip(";,()[]，；、")
    cleaned_text = re.sub(r";{2,}", ";", cleaned_text)
    cleaned_text = cleaned_text.strip()
    return cleaned_text or None


def trim_compound_prefix(compound_text, road_name, road_no):
    """如果园区 / 楼宇名称以前缀包含路名和门牌号，就把前缀裁掉。"""
    if not compound_text or not road_name or not road_no:
        return compound_text

    prefix = f"{road_name}{road_no}"
    if not compound_text.startswith(prefix):
        return compound_text

    trimmed_text = compound_text[len(prefix):].lstrip("-,，,;； ")
    return trimmed_text or compound_text


def trim_compound_admin_prefix(compound_text, province, city, district):
    """如果园区 / 楼宇名称以前缀包含省、市、区，就把这些前缀裁掉。"""
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
    """如果道路名前缀包含省、市、区或实体名，就把这些前缀裁掉。"""
    if not road_text:
        return road_text

    trimmed_text = road_text
    for prefix in [province, city, district, entity_name]:
        if prefix and trimmed_text.startswith(prefix):
            trimmed_text = trimmed_text[len(prefix):].lstrip("-,，,;； ")

    return trimmed_text or road_text


def split_road_phase_prefix(road_text):
    """如果道路名前缀带有“第N期 / N期”，就拆出来。"""
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
    """把从 road_name 拆出的期数并回 entity_name。"""
    if not compound_text or not phase_texts or "," in compound_text:
        return compound_text

    merged_text = compound_text
    for phase_text in phase_texts:
        if phase_text and phase_text not in merged_text:
            merged_text = f"{merged_text}{phase_text}"

    return merged_text


def move_compound_direction_to_road(road_text, compound_text):
    """如果 entity_name 以前缀带方向段，就把该前缀挪到 road_name。"""
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


def load_test_records():
    """从测试 Excel 中读取输入记录。"""

    workbook = load_workbook(TEST_DATA_PATH, read_only=True, data_only=True)
    worksheet = workbook[TEST_DATA_SHEET_NAME]
    rows = worksheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []

    header_list = list(header)
    column_index_map = {column_name: index for index, column_name in enumerate(header_list)}

    records = []
    for row in rows:
        company_name = row[column_index_map[INPUT_COLUMN_COMPANY_NAME]]
        company_address = row[column_index_map[INPUT_COLUMN_COMPANY_ADDRESS]]

        if company_name is None or company_address is None:
            continue

        company_name_text = str(company_name).strip()
        company_address_text = str(company_address).strip()
        if not company_name_text or not company_address_text:
            continue

        records.append(
            {
                "company_name": company_name_text,
                "company_address": company_address_text,
            }
        )

    return records


def save_results_to_excel(results):
    """将解析结果写入 Excel 文件。"""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"

    worksheet.append(RESULT_FIELDS)
    for result in results:
        row = [result.get(field_name) for field_name in RESULT_FIELDS]
        worksheet.append(row)

    workbook.save(OUTPUT_PATH)


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------

def extract_parts(normalized_address):
    """从地址中提取所有可识别片段。"""
    parts = []

    cursor = 0 #扫描指针
    province_match = re.match(
        r"((?:[^省]+省)|(?:内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|新疆维吾尔自治区)|(?:香港特别行政区|澳门特别行政区))",
        normalized_address[cursor:],
    )
    if province_match:
        start = cursor + province_match.start()
        end = cursor + province_match.end()
        append_part(parts, "province", province_match.group(1), start, end, "行政区划") #match.group(0)返回整体；match.group(1)返回第一个抓到的内容
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

    compound_suffix_pattern = "|".join(sorted(COMPOUND_SUFFIXES, key=len, reverse=True)) #按长度排序候选后缀
    compound_pattern = re.compile(
        rf"([\u4e00-\u9fa5A-Za-z0-9()\-]{{2,40}}?(?:{compound_suffix_pattern})(?:[A-Za-z0-9一二三四五六七八九十]+期)?(?:[A-Za-z0-9一二三四五六七八九十]+区)?)"
    ) #正则捕获
    for match in compound_pattern.finditer(normalized_address[cursor:]): #遍历所有“园区/楼宇名称”
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
            append_part(
                parts,
                "road_no",
                road_no_match.group(1),
                number_start,
                number_end,
                "道路编号识别",
            )

    #前面是顺序解析，这里是全局扫描，因为这些元素可能出现在任意的位置
    return parts


def assemble_fields(parts):
    """把已识别片段组装成最终字段。"""
    resolved_fields = {field_name: None for field_name in STRUCTURED_FIELDS}
    consumed_spans = []
    road_phase_texts = []

    for field_name in ["province", "city", "district", "town_street"]:
        field_parts = get_field_parts(parts, field_name)
        if field_parts:
            resolved_fields[field_name] = merge_part_texts(field_parts)
            consumed_spans.extend((part["start"], part["end"]) for part in field_parts)

    min_start = 0
    for field_name in ["district", "town_street"]:
        field_parts = get_field_parts(parts, field_name)
        if field_parts:
            min_start = max(min_start, max(part["end"] for part in field_parts)) #之后不用再回头到行政区前面去找了

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
            trimmed_road_parts.append(
                {
                    **part,
                    "text": trimmed_text,
                }
            )

        resolved_fields["road_name"] = merge_part_texts(trimmed_road_parts)
        consumed_spans.extend((part["start"], part["end"]) for part in road_parts)

        #只找出现在路名后面的门牌号
        road_no_parts = get_field_parts(parts, "road_no", min_start)
        if road_no_parts:
            resolved_fields["road_no"] = merge_part_texts(road_no_parts)
            consumed_spans.extend((part["start"], part["end"]) for part in road_no_parts)

    compound_parts = get_field_parts(parts, "entity_name", min_start)
    if compound_parts:
        trimmed_compound_parts = []
        for part in compound_parts:
            trimmed_text = trim_compound_prefix(
                part["text"],
                resolved_fields["road_name"],
                resolved_fields["road_no"],
            )
            trimmed_text = trim_compound_admin_prefix(
                trimmed_text,
                resolved_fields["province"],
                resolved_fields["city"],
                resolved_fields["district"],
            )
            trimmed_compound_parts.append(
                {
                    **part,
                    "text": trimmed_text,
                }
            )
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


def split_residual(normalized_address, resolved_fields, parts):
    """将剩余文本写入 Additional Information。"""
    consumed_spans = resolved_fields.get("_consumed_spans", [])
    residual_text = spans_to_text(normalized_address, consumed_spans)
    residual_text = cleanup_fragment(residual_text) or ""

    if not residual_text:
        return None

    remaining_fragments = [cleanup_fragment(fragment) for fragment in re.split(r"[;,]", residual_text)]
    remaining_fragments = [fragment for fragment in remaining_fragments if fragment]
    return ",".join(remaining_fragments) if remaining_fragments else None


def parse_one(company_name, company_address):
    """解析单条地址。"""
    result = build_empty_result(company_name, company_address)
    normalized_address = normalize_text(company_address)
    parts = extract_parts(normalized_address)
    resolved_fields = assemble_fields(parts)
    additional_information = split_residual(normalized_address, resolved_fields, parts)

    for field_name in STRUCTURED_FIELDS:
        if field_name in resolved_fields and not field_name.startswith("_"):
            result[field_name] = resolved_fields[field_name]

    result["Additional Information"] = additional_information
    return result


def parse_batch(records):
    """批量解析地址。"""
    results = []
    total_count = len(records)

    if total_count:
        print(f"开始解析，共 {total_count} 条记录。")

    for index, record in enumerate(records, start=1):
        company_name = record.get("company_name", "").strip()
        company_address = record.get("company_address", "").strip()
        if not company_name or not company_address:
            continue
        results.append(parse_one(company_name, company_address))

        if (
            index % PROGRESS_PRINT_INTERVAL == 0
            or index == total_count
        ):
            print(f"解析进度: {index}/{total_count}")

    return results


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    """程序入口。"""
    records = load_test_records()
    results = parse_batch(records)
    save_results_to_excel(results)

    print(f"输入文件: {TEST_DATA_PATH.name}")
    print(f"工作表: {TEST_DATA_SHEET_NAME}")
    print(f"读取记录数: {len(records)}")
    print(f"输出结果数: {len(results)}")
    print(f"输出文件: {OUTPUT_PATH.name}")

    if results:
        print("首条结果示例:")
        print(dumps(results[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
