import re

from openpyxl import load_workbook


def normalize_text(address: str) -> str:
    """对原始工商注册地址做最小化标准清洗，但不改变核心截断规则。"""
    # 统一先转成字符串并去掉首尾空白，避免 Excel 单元格里有意外空格。
    text = str(address).strip()

    # 删除中英文括号中的备注内容，例如“（自主申报）”“(A座)”。
    # 这里采用非贪婪匹配，只删除单段括号内容，不跨段吞掉正文。
    text = re.sub(r"（.*?）", "", text)
    text = re.sub(r"\(.*?\)", "", text)

    # 删除这次规则里明确指定的说明性词语。
    # 这些内容不属于“市/区/路/号”的主体信息，会干扰后续统一截断。
    text = text.replace("仅限办公用途", "")
    text = text.replace("仅限办公", "")
    text = text.replace("自主申报", "")

    # 删除空格、换行、制表符，避免地址里出现格式噪声。
    text = re.sub(r"[\r\n\t ]+", "", text)

    # 将“111-115单号”这类扩展门牌先规整为“111号”。
    # 这是因为当前规则要求“只保留基础号”，不保留区间号和单/双号说明。
    text = re.sub(r"(\d+)-\d+单号", r"\1号", text)
    return text


def truncate_to_basic_house_no(address: str) -> str:
    """将地址严格截断到首个基础门牌号“号”为止。"""
    # 先做统一清洗，再进行门牌截断。
    text = normalize_text(address)

    # 使用最短匹配，确保命中第一个“数字+号”。
    # 例如：
    # - “天河路240号之一...” -> 命中“240号”
    # - “体育西路109号高盛大厦13A室” -> 命中“109号”
    match = re.match(r"^(.*?)(\d+)号", text)
    if not match:
        # 如果整条地址里没有“号”，则按既定规则原样返回清洗后的文本，
        # 不做猜测补全，也不额外置空。
        return text

    # 输出只保留到基础门牌号，不保留“号”之后的任何内容。
    return f"{match.group(1)}{match.group(2)}号"


def process_workbook(input_path: str, output_path: str) -> None:
    """读取 Excel 中的 company_address 列，新增 clean_address 列并输出新文件。"""
    # 加载原始工作簿，并固定使用第一个 sheet。
    workbook = load_workbook(input_path)
    worksheet = workbook[workbook.sheetnames[0]]

    # 读取首行表头，用于定位输入列。
    headers = [cell.value for cell in worksheet[1]]
    if "company_address" not in headers:
        # 如果缺少目标列，明确报错，不静默跳过。
        raise ValueError("Missing required column: company_address")

    # 确定原始地址列的位置，并在最后新增结果列 clean_address。
    source_col = headers.index("company_address") + 1
    target_col = len(headers) + 1
    worksheet.cell(row=1, column=target_col, value="clean_address")

    # 从第二行开始逐条处理地址。
    for row_idx in range(2, worksheet.max_row + 1):
        value = worksheet.cell(row=row_idx, column=source_col).value

        # 空值直接写空字符串；非空值按“严格截断到基础号”的规则清洗。
        cleaned = "" if value in (None, "") else truncate_to_basic_house_no(str(value))
        worksheet.cell(row=row_idx, column=target_col, value=cleaned)

    # 输出到新文件，避免覆盖原始 Excel。
    workbook.save(output_path)


if __name__ == "__main__":
    # 固定读取当前目录下的测试.xlsx，并输出测试_cleaned.xlsx。
    process_workbook("广州商圈_20260227.xlsx", "广州商圈_20260227_cleaned.xlsx")
