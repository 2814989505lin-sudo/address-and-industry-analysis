from pathlib import Path

from openpyxl import Workbook, load_workbook


# ------------------------------------------------------------
# 参数定义
# ------------------------------------------------------------

INPUT_FILE_NAME = "output_customer.xlsx"
INPUT_PATH = Path(__file__).with_name(INPUT_FILE_NAME)

SOURCE_FILE_NAME = "广州商圈_20260227.xlsx"
SOURCE_PATH = Path(__file__).with_name(SOURCE_FILE_NAME)

OUTPUT_FILE_NAME = "rough_output_customer.xlsx"
OUTPUT_PATH = Path(__file__).with_name(OUTPUT_FILE_NAME)

CLEAN_OUTPUT_FILE_NAME = "clean_output_customer.xlsx"
CLEAN_OUTPUT_PATH = Path(__file__).with_name(CLEAN_OUTPUT_FILE_NAME)

ROAD_NAME_LONG_THRESHOLD = 12

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
    "场地"
]

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

BUSINESS_AREA_FIELD = "商圈名"

# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def normalize_value(value):
    """把单元格值转成干净字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def split_values(text):
    """把逗号拼接的多值字段拆开。"""
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def contains_obvious_compound_suffix(address):
    """地址中是否出现明显的园区 / 楼宇后缀。"""
    return any(suffix in address for suffix in COMPOUND_SUFFIXES)


def has_suspicious_multi_value(row):
    """不该多值的字段里是否出现了多个值。"""
    for field_name in MULTI_VALUE_SUSPICIOUS_FIELDS:
        values = split_values(row.get(field_name, ""))
        if len(values) > 1:
            return True
    return False


def compound_contains_road(row):
    """entity_name 是否包含 road_name 或 road_name + road_no。"""
    compound_values = split_values(row.get("entity_name", ""))
    road_values = split_values(row.get("road_name", ""))
    road_no_values = split_values(row.get("road_no", ""))

    for compound_value in compound_values:
        for road_value in road_values:
            if road_value and road_value in compound_value:
                return True
            for road_no_value in road_no_values:
                if road_value and road_no_value and f"{road_value}{road_no_value}" in compound_value:
                    return True
    return False


def compound_starts_with_generic_word(row):
    """entity_name 是否以通用实体词开头。"""
    compound_values = split_values(row.get("entity_name", ""))
    if not compound_values:
        return False

    for compound_value in compound_values:
        if any(compound_value.startswith(prefix) for prefix in COMPOUND_SUFFIXES):
            return True

    return False


def has_suspicious_road_name(row):
    """road_name 是否同时包含多个道路后缀。"""
    road_values = split_values(row.get("road_name", ""))
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
    """road_name 是否包含完整的 province、city 或 district。"""
    road_values = split_values(row.get("road_name", ""))
    admin_values = [
        normalize_value(row.get("province", "")),
        normalize_value(row.get("city", "")),
        normalize_value(row.get("district", "")),
    ]

    admin_values = [value for value in admin_values if value]
    if not road_values or not admin_values:
        return False

    for road_value in road_values:
        for admin_value in admin_values:
            if admin_value in road_value:
                return True

    return False


def has_suspicious_entity_name(row):
    """entity_name 是否看起来不像实体名称。"""
    compound_values = split_values(row.get("entity_name", ""))
    if not compound_values:
        return False

    for compound_value in compound_values:
        if compound_value in COMPOUND_SINGLE_CHAR_FRAGMENTS: #判断是否刚好等于列表里的某个单字残片
            return True

        if compound_value in COMPOUND_SUFFIXES: #判断是不是单独的通用后缀词
            return True

        if not any(suffix in compound_value for suffix in COMPOUND_SUFFIXES):#如果一个后缀都不包含，则打入rough
            return True

        if any(token in compound_value for token in COMPOUND_BAD_TOKENS): #包含“房/室等”，则打入rough
            return True

        if compound_value[:1].isdigit(): #第一个字符是不是数字
            return True

        if any(compound_value.startswith(token) for token in COMPOUND_BAD_STARTS): #以前缀残词 `仅/及/之` 开头
            return True

        if any(compound_value.endswith(token) for token in COMPOUND_BAD_ENDS): #以 `仅/及/自编/之一/使用` 结尾
            return True

    return False


def has_suspicious_brackets(row):
    """字段里是否出现左右数量不相等的括号。"""
    check_fields = ["entity_name"]

    for field_name in check_fields:
        value = normalize_value(row.get(field_name, ""))
        if not value:
            continue

        if value.count("(") != value.count(")"):
            return True

    return False


def load_rows():
    """读取 output.xlsx 的全部结果。"""
    workbook = load_workbook(INPUT_PATH, read_only=True, data_only=True)
    worksheet = workbook[workbook.sheetnames[0]]
    rows = worksheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return [], []

    header_list = list(header)
    all_rows = []
    for row in rows:
        row_dict = {}
        for index, field_name in enumerate(header_list):
            row_dict[field_name] = normalize_value(row[index] if index < len(row) else None)
        all_rows.append(row_dict)

    workbook.close()

    if BUSINESS_AREA_FIELD not in header_list:
        business_areas = load_business_areas()
        if len(business_areas) != len(all_rows):
            raise ValueError(
                f"商圈源文件行数与解析结果不一致：source={len(business_areas)} output={len(all_rows)}"
            )
        header_list = [BUSINESS_AREA_FIELD] + header_list
        for row, business_area in zip(all_rows, business_areas):
            row[BUSINESS_AREA_FIELD] = business_area

    return header_list, all_rows


def load_business_areas():
    """按原始输入行顺序读取商圈名。"""
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(f"缺少商圈源文件：{SOURCE_PATH}")

    workbook = load_workbook(SOURCE_PATH, read_only=True, data_only=True)
    worksheet = workbook[workbook.sheetnames[0]]
    rows = worksheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        workbook.close()
        raise ValueError(f"商圈源文件没有表头：{SOURCE_PATH.name}")

    header_list = [normalize_value(value) for value in header]
    if BUSINESS_AREA_FIELD not in header_list:
        workbook.close()
        raise ValueError(f"商圈源文件缺少必要列：{BUSINESS_AREA_FIELD}")

    business_area_index = header_list.index(BUSINESS_AREA_FIELD)
    business_areas = []

    for row in rows:
        business_area = normalize_value(row[business_area_index] if business_area_index < len(row) else None)
        business_areas.append(business_area)

    workbook.close()
    return business_areas


def save_rows(header, rows, output_path):
    """把筛选结果写入指定 Excel 文件。"""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"

    output_header = [field_name for field_name in header if field_name != BUSINESS_AREA_FIELD]
    if BUSINESS_AREA_FIELD in header:
        output_header = [BUSINESS_AREA_FIELD] + output_header
    output_header = output_header + ["rough_reasons"]
    worksheet.append(output_header)

    for row in rows:
        worksheet.append([row.get(field_name, "") for field_name in output_header[:-1]] + [row.get("rough_reasons", "")])

    workbook.save(output_path)


# ------------------------------------------------------------
# 核心逻辑
# ------------------------------------------------------------
def collect_rough_reasons(row):
    """收集一行命中的低精度规则。"""
    reasons = []

    entity_name = normalize_value(row.get("entity_name", ""))
    road_name = normalize_value(row.get("road_name", ""))
    road_no = normalize_value(row.get("road_no", ""))
    district = normalize_value(row.get("district", ""))
    address = normalize_value(row.get("company_address", ""))

    if compound_contains_road(row):
        reasons.append("entity_name包含road_name或road_name+road_no")

    if compound_starts_with_generic_word(row):
        reasons.append("entity_name以通用实体词开头")

    if road_name and len(road_name) > ROAD_NAME_LONG_THRESHOLD:
        reasons.append("road_name很长")

    if has_suspicious_road_name(row):
        reasons.append("road_name同时包含多个道路后缀")

    if road_contains_admin(row):
        reasons.append("road_name包含完整的province或city或district")

    if not road_no or not road_name or not district:
        reasons.append("road_no或road_name或district缺失")

    if not entity_name and contains_obvious_compound_suffix(address):
        reasons.append("entity_name缺失但地址里有明显实体后缀")

    if has_suspicious_entity_name(row):
        reasons.append("entity_name看起来不像实体名称")

    if has_suspicious_brackets(row):
        reasons.append("输出字段里有异常括号")

    if has_suspicious_multi_value(row):
        reasons.append("不该多值的字段出现多个值")

    return reasons


# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------

def main():
    """筛选低精度结果并输出新文件。"""
    header, rows = load_rows()
    rough_rows = []
    clean_rows = []

    for row in rows:
        reasons = collect_rough_reasons(row)
        if reasons:
            row["rough_reasons"] = "；".join(reasons)
            rough_rows.append(row)
        else:
            row["rough_reasons"] = ""
            clean_rows.append(row)

    save_rows(header, rough_rows, OUTPUT_PATH)
    save_rows(header, clean_rows, CLEAN_OUTPUT_PATH)

    print(f"输入文件: {INPUT_PATH.name}")
    print(f"总记录数: {len(rows)}")
    print(f"筛出低精度记录数: {len(rough_rows)}")
    print(f"筛出干净记录数: {len(clean_rows)}")
    print(f"低精度输出文件: {OUTPUT_PATH.name}")
    print(f"干净输出文件: {CLEAN_OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
