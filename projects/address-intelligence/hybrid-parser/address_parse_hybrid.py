import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

import pandas as pd


# 单脚本版本的地址结构化工具。
# 处理顺序固定为：读取 Excel -> 清洗地址 -> 规则解析 -> 关键字段校验 ->
# MiniMax LLM 兜底 -> 导出全量结果与待复核结果。
# 运行时只需要修改这一组参数，不再走命令行。
INPUT_FILE = "地址结构化测试.xlsx"
SHEET_NAME = "Sheet1"
ADDRESS_COLUMN = "地址"
OUTPUT_FILE = "结构化地址_混合版.xlsx"
REVIEW_FILE = "低置信度待复核.xlsx"
MODEL_NAME = "MiniMax-M2.5"
API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("MINIMAX_API_KEY")

STRUCTURED_FIELDS = [
    "raw_address",
    "clean_address",
    "province",
    "city",
    "district",
    "town_or_street",
    "road",
    "road_no",
    "sub_no",
    "poi_or_market",
    "floor",
    "room_or_shop",
    "remark",
    "parse_method",
    "rule_status",
    "rule_reason",
    "needs_review",
]

NOISE_RE = re.compile(r"[\s#$*]+$")
PAREN_RE = re.compile(r"[（(]([^()（）]+)[）)]")
PROVINCE_RE = re.compile(
    r"(北京市|天津市|上海市|重庆市|香港特别行政区|澳门特别行政区|"
    r"新疆维吾尔自治区|内蒙古自治区|广西壮族自治区|宁夏回族自治区|西藏自治区|"
    r"[^省特别行政区自治区]+省)"
)
CITY_RE = re.compile(r"(.+?市|.+?自治州|.+?地区|.+?盟)")
DISTRICT_RE = re.compile(r"(.+?区|.+?县|.+?旗|.+?市)")
TOWN_RE = re.compile(r"(.+?镇|.+?乡|.+?街道|.+?苏木|.+?街)")
# road 只允许中文/字母/括号，不允许阿拉伯数字进入 road，避免把“193号”拆成“19 + 3号”。
ROAD_RE = re.compile(
    r"(?P<road>[\u4e00-\u9fa5A-Za-z（）()]+?(?:大道|路|街|巷|里|道)[\u4e00-\u9fa5A-Za-z（）()]*)"
    r"(?P<road_no>\d+(?:-\d+)?号)"
    r"(?P<sub_no>之[一二三四五六七八九十\dA-Za-z]+)?"
)
POI_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()]{2,}"
    r"(?:广场|中心|市场|商场|商城|交易中心|商业城|皮具城|城|大厦|大楼|公寓|"
    r"园区|工业园|科技园|物流园|花园|园|苑|小区|酒店|港|馆|站|医院|学校|大学|中学|小学))"
)
ROAD_ONLY_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z（）()]+(?:大道|路|街|巷|里|道)[\u4e00-\u9fa5A-Za-z（）()]*$")
FLOOR_PATTERNS = [
    re.compile(r"(地下[一二三四五六七八九十\d]+层)"),
    re.compile(r"(负[一二三四五六七八九十\d]+层)"),
    re.compile(r"(首层)"),
    re.compile(r"([一二三四五六七八九十\d]+楼)"),
    re.compile(r"([一二三四五六七八九十\d]+层)"),
]
ROOM_PATTERNS = [
    re.compile(
        r"((?:[A-Za-z]?\d+(?:-\d+)?(?:室|房|铺|档|号铺|商铺))"
        r"(?:[、，,；;/][A-Za-z]?\d+(?:-\d+)?(?:室|房|铺|档|号铺|商铺))*)"
    ),
    re.compile(r"((?:第)?[A-Za-z]?\d+(?:-\d+)?)$"),
    re.compile(r"([A-Za-z]\d+(?:-\d+)?)"),
]


class TerminalLogger:
    """终端日志输出类，用于美化控制台输出"""
    
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.start_time = None
        self.step_start_time = None
        self.stats = {
            "total": 0,
            "unique": 0,
            "rule_accepted": 0,
            "llm_success": 0,
            "llm_failed": 0,
            "needs_review": 0,
        }
    
    def print_header(self):
        """打印程序标题头"""
        print("\n" + "="*80)
        print("🏢 地址结构化处理工具 v1.0".center(80))
        print("="*80)
        print(f"📅 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📁 输入文件: {INPUT_FILE}")
        print(f"📊 工作表: {SHEET_NAME}")
        print(f"🔧 解析模型: {MODEL_NAME}")
        print("="*80 + "\n")
    
    def print_step(self, step_name, step_number, total_steps):
        """打印处理步骤"""
        print(f"\n{'─'*80}")
        print(f"📍 步骤 {step_number}/{total_steps}: {step_name}")
        print(f"{'─'*80}")
    
    def start_step(self):
        """开始一个步骤的计时"""
        self.step_start_time = time.time()
    
    def end_step(self, step_name):
        """结束一个步骤并打印耗时"""
        if self.step_start_time:
            elapsed = time.time() - self.step_start_time
            print(f"✅ {step_name} 完成，耗时: {elapsed:.2f} 秒")
            self.step_start_time = None
    
    def print_progress(self, current, total, description=""):
        """打印进度条"""
        if not self.verbose:
            return
        percent = current / total * 100
        bar_length = 40
        filled_length = int(bar_length * current // total)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)
        sys.stdout.write(f'\r{description} [{bar}] {percent:.1f}% ({current}/{total})')
        if current == total:
            sys.stdout.write('\n')
        sys.stdout.flush()
    
    def print_stats(self):
        """打印统计信息"""
        print("\n" + "="*80)
        print("📊 处理统计".center(80))
        print("="*80)
        print(f"📝 总地址数: {self.stats['total']}")
        print(f"🔑 唯一地址数: {self.stats['unique']}")
        print(f"✅ 规则通过: {self.stats['rule_accepted']} ({self.stats['rule_accepted']/self.stats['unique']*100:.1f}%)")
        print(f"🤖 LLM成功: {self.stats['llm_success']} ({self.stats['llm_success']/self.stats['unique']*100:.1f}%)")
        print(f"⚠️  LLM失败: {self.stats['llm_failed']} ({self.stats['llm_failed']/self.stats['unique']*100:.1f}%)")
        print(f"🔍 待复核: {self.stats['needs_review']} ({self.stats['needs_review']/self.stats['total']*100:.1f}%)")
        
        # 打印处理时间
        if self.start_time:
            elapsed = time.time() - self.start_time
            print(f"⏱️  总耗时: {elapsed:.2f} 秒")
        
        print("="*80 + "\n")
    
    def print_sample_result(self, result, index=0):
        """打印单个结果示例"""
        if not self.verbose or index > 3:  # 只打印前3个示例
            return
        print(f"\n📋 结果示例 {index + 1}:")
        print(f"   原始地址: {result.get('raw_address', '')}")
        print(f"   清洗地址: {result.get('clean_address', '')}")
        print(f"   行政区: {result.get('district', '')}")
        print(f"   道路: {result.get('road', '')} {result.get('road_no', '')}")
        print(f"   解析方式: {result.get('parse_method', '')}")
        print(f"   待复核: {'是' if result.get('needs_review') else '否'}")
    
    def log_rule_parse(self, address, result):
        """记录规则解析结果"""
        if result["rule_status"] == "accepted":
            self.stats["rule_accepted"] += 1
            if self.verbose:
                print(f"  ✅ 规则通过: {address[:50]}... -> {result.get('district', '')} {result.get('road', '')}")
        else:
            if self.verbose:
                print(f"  ⚠️  规则失败: {address[:50]}... -> {result['rule_reason']}")
    
    def log_llm_call(self, address, success=True):
        """记录LLM调用结果"""
        if success:
            self.stats["llm_success"] += 1
            if self.verbose:
                print(f"  🤖 LLM成功: {address[:50]}...")
        else:
            self.stats["llm_failed"] += 1
            if self.verbose:
                print(f"  ❌ LLM失败: {address[:50]}...")


logger = TerminalLogger(verbose=True)


def normalize_parentheses(text):
    """统一中英文括号，减少后续正则处理分支。"""
    return text.replace("（", "(").replace("）", ")")


def clean_raw_text(text):
    """做保守去噪，只清理空格、尾部符号和括号格式，不改动正文语义。"""
    value = "" if pd.isna(text) else str(text)
    value = value.strip()
    value = normalize_parentheses(value)
    value = NOISE_RE.sub("", value)
    value = re.sub(r"\s+", "", value)
    return value


def split_remark(text):
    """保留正文，仅做轻量清洗；备注改为在结构化完成后由残余文本统一回填。"""
    return text.strip(), None


def dedupe_join(values):
    """按出现顺序去重并用中文分号拼接。"""
    ordered = []
    seen = set()
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return "；".join(ordered) if ordered else None


def clean_fragment(text):
    """清理残余文本片段两端噪音，但保留正文内容。"""
    fragment = "" if text is None else str(text)
    fragment = fragment.strip("，,；;、/ ")
    fragment = re.sub(r"^[（(]+|[）)]+$", "", fragment)
    fragment = fragment.strip("，,；;、/ ")
    return fragment or None


def parse_admin_fields(text):
    """按行政区划层级做通用提取，不再依赖固定城市词典。"""
    scope = text
    province = city = district = town = None
    multiple_districts = False

    province_match = PROVINCE_RE.match(scope)
    if province_match:
        province = province_match.group(1)
        scope = scope[province_match.end():]

    city_match = CITY_RE.match(scope)
    if city_match:
        city = city_match.group(1)
        scope = scope[city_match.end():]

    district_matches = [match.group(1) for match in DISTRICT_RE.finditer(scope[:24])]
    district_matches = list(dict.fromkeys(district_matches))
    if district_matches:
        district = district_matches[0]
        multiple_districts = len(district_matches) > 1
        first_district = DISTRICT_RE.match(scope)
        if first_district:
            scope = scope[first_district.end():]

    town_match = TOWN_RE.match(scope)
    if town_match:
        town = town_match.group(1)

    return province, city, district, town, multiple_districts


def strip_leading_admin(text):
    """剥离地址开头的行政区划前缀，降低道路误匹配概率。"""
    body = text
    while True:
        changed = False
        for pattern in (PROVINCE_RE, CITY_RE, DISTRICT_RE, TOWN_RE):
            match = pattern.match(body)
            if match:
                body = body[match.end():]
                changed = True
        if not changed:
            return body


def trim_leading_poi_from_road(road):
    """当 POI 紧挨在道路前面时，裁掉前置 POI，避免 road 被污染。"""
    if not road:
        return road

    matches = list(POI_SUFFIX_RE.finditer(road))
    for match in reversed(matches):
        tail = road[match.end():]
        if tail and ROAD_ONLY_RE.fullmatch(tail):
            return tail
    return road


def parse_road(text):
    """提取道路、门牌号和附号；这是规则层最关键的主体地址解析。"""
    body = strip_leading_admin(text)
    match = ROAD_RE.search(body)
    if not match:
        return None, None, None
    road = trim_leading_poi_from_road(match.group("road"))
    return road, match.group("road_no"), match.group("sub_no")


def parse_poi(text, road=None, road_no=None, sub_no=None):
    """提取 POI，优先识别道路前后的场所名片段。"""
    road_block = "".join(part for part in (road, road_no, sub_no) if part)
    parts = []
    if road_block and road_block in text:
        prefix, suffix = text.split(road_block, 1)
        parts.extend([prefix, suffix])
    else:
        parts.append(text)

    candidates = []
    for part in parts:
        part = strip_leading_admin(part)
        for match in POI_SUFFIX_RE.finditer(part):
            fragment = clean_fragment(match.group(1))
            if fragment:
                candidates.append(fragment)

    return dedupe_join(candidates)


def parse_floor(text):
    """提取楼层信息，兼容首层、中文层数和地下层写法。"""
    for pattern in FLOOR_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def parse_room(text):
    """提取房号、室号、铺位或档口编号。"""
    strong_matches = [match.group(1) for match in ROOM_PATTERNS[0].finditer(text)]
    if strong_matches:
        return dedupe_join(strong_matches)

    weak_matches = []
    for pattern in ROOM_PATTERNS[1:]:
        weak_matches.extend(match.group(1) for match in pattern.finditer(text))
    return dedupe_join(weak_matches)


def build_residual_remark(clean_address, fields, explicit_remark=None):
    """把未被结构化字段覆盖的残余正文回填到 remark，避免信息直接丢失。"""
    residue = clean_address or ""
    candidates = []
    for key in ("province", "city", "district", "town_or_street", "poi_or_market", "road", "road_no", "sub_no", "floor", "room_or_shop"):
        value = fields.get(key)
        if not value:
            continue
        if key == "room_or_shop":
            candidates.extend(filter(None, re.split(r"[；;]", value)))
        else:
            candidates.append(value)

    road_block = "".join(part for part in (fields.get("road"), fields.get("road_no"), fields.get("sub_no")) if part)
    if road_block:
        candidates.append(road_block)

    for value in sorted({str(item).strip() for item in candidates if str(item).strip()}, key=len, reverse=True):
        residue = residue.replace(value, "", 1)

    fragments = []
    for part in re.split(r"[；;]", residue):
        fragment = clean_fragment(part)
        if fragment:
            fragments.append(fragment)

    return dedupe_join([explicit_remark, *fragments])


def build_base_result(raw_address, clean_address, remark):
    """构造统一结果骨架，保证规则层和 LLM 层输出字段一致。"""
    return {
        "raw_address": raw_address,
        "clean_address": clean_address,
        "province": None,
        "city": None,
        "district": None,
        "town_or_street": None,
        "road": None,
        "road_no": None,
        "sub_no": None,
        "poi_or_market": None,
        "floor": None,
        "room_or_shop": None,
        "remark": remark,
        "parse_method": None,
        "rule_status": None,
        "rule_reason": None,
        "needs_review": 0,
    }


def parse_rule(raw_address):
    """执行规则解析，并根据关键字段完整性决定是否进入 LLM fallback。"""
    normalized = clean_raw_text(raw_address)
    clean_address, explicit_remark = split_remark(normalized)
    result = build_base_result(raw_address, clean_address, explicit_remark)

    province, city, district, town, multiple_districts = parse_admin_fields(clean_address)
    road, road_no, sub_no = parse_road(clean_address)
    poi = parse_poi(clean_address, road=road, road_no=road_no, sub_no=sub_no)
    floor = parse_floor(clean_address)
    room = parse_room(clean_address)

    result.update(
        {
            "province": province,
            "city": city,
            "district": district,
            "town_or_street": town,
            "road": road,
            "road_no": road_no,
            "sub_no": sub_no,
            "poi_or_market": poi,
            "floor": floor,
            "room_or_shop": room,
        }
    )
    result["remark"] = build_residual_remark(clean_address, result, explicit_remark)

    noise_flags = []
    if multiple_districts:
        noise_flags.append("multiple_districts_detected")
    if clean_address.count("(") != clean_address.count(")"):
        noise_flags.append("unbalanced_parentheses")
    if re.search(r"[#$*]", clean_address):
        noise_flags.append("has_unparsed_noise")

    reasons = []
    if not road:
        reasons.append("missing_road")
    reasons.extend(noise_flags)

    if reasons:
        result["rule_status"] = "llm_fallback"
        result["rule_reason"] = ";".join(reasons)
    else:
        result["rule_status"] = "accepted"
        result["rule_reason"] = "road extracted by rule"
        result["parse_method"] = "rule"

    return result


def extract_json(text):
    """从模型文本中提取 JSON 对象，兼容 ```json 代码块包裹的返回。"""
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 未返回 JSON 对象")
    return json.loads(content[start : end + 1])


def build_prompt(rule_result):
    """构造单条地址的 LLM 提示词，只传最小必要上下文。"""
    sample = {key: rule_result.get(key) for key in STRUCTURED_FIELDS if key not in {"parse_method", "needs_review"}}
    return f"""
你是中文工商地址结构化抽取器。请根据给定地址输出 JSON。

要求：
1. 只能输出一个 JSON 对象，不要输出解释。
2. 不确定字段请填 null，不要猜测。
3. 保留原始地址中的真实信息，不要编造行政区或门牌号。
4. parse_method 固定输出 "llm"。
5. 规则和你都没法稳定归类的残余文本，必须放进 remark，不能直接丢弃。
6. 字段只允许这些：
raw_address, clean_address, province, city, district, town_or_street, road, road_no, sub_no, poi_or_market, floor, room_or_shop, remark, parse_method

原始地址：
{rule_result["raw_address"]}

清洗后地址：
{rule_result["clean_address"]}

规则解析结果：
{json.dumps(sample, ensure_ascii=False)}

规则失败原因：
{rule_result["rule_reason"]}
""".strip()


def get_anthropic_client():
    """初始化 MiniMax Anthropic 兼容客户端；无依赖或无 key 时允许降级干跑。"""
    try:
        import anthropic
    except ModuleNotFoundError:
        return None, "anthropic_not_installed"

    api_key = API_KEY
    base_url = os.getenv("ANTHROPIC_BASE_URL", os.getenv('ANTHROPIC_BASE_URL', ''))
    if not api_key:
        return None, "missing_anthropic_api_key"

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    return client, None


def call_llm(client, model, rule_result):
    """调用 LLM 做单条地址兜底解析，并把文本结果转成 JSON。"""
    prompt = build_prompt(rule_result)
    response = client.messages.create(
        model=model,
        max_tokens=1200,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    texts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    return extract_json("".join(texts))


def validate_llm_result(result):
    """校验 LLM 是否补齐关键字段，决定能否直接采用结果。"""
    meaningful_keys = [
        "province",
        "city",
        "district",
        "town_or_street",
        "road",
        "road_no",
        "sub_no",
        "poi_or_market",
        "floor",
        "room_or_shop",
        "remark",
    ]
    return [] if any(result.get(key) for key in meaningful_keys) else ["llm_no_extraction"]


def apply_llm_result(rule_result, llm_result):
    """把通过校验的 LLM 结果合并到规则结果中。"""
    merged = dict(rule_result)
    for key in STRUCTURED_FIELDS:
        if key in {"rule_status", "rule_reason", "needs_review"}:
            continue
        value = llm_result.get(key)
        if value not in (None, ""):
            merged[key] = value
    merged["remark"] = build_residual_remark(
        merged.get("clean_address"),
        merged,
        llm_result.get("remark") or rule_result.get("remark"),
    )
    merged["parse_method"] = "llm"
    return merged


def fallback_without_llm(rule_result, reason):
    """在无法调用 LLM 时降级为待复核，保证整批任务不中断。"""
    result = dict(rule_result)
    result["parse_method"] = "rule"
    result["needs_review"] = 1
    result["rule_reason"] = f'{rule_result["rule_reason"]};{reason}'.strip(";")
    return result


def resolve_one(rule_result, client, model):
    """解析单个唯一地址，优先用规则结果，必要时调用 LLM。"""
    if rule_result["rule_status"] == "accepted":
        return rule_result

    if client is None:
        logger.stats["llm_failed"] += 1
        return fallback_without_llm(rule_result, "llm_unavailable")

    try:
        llm_result = call_llm(client, model, rule_result)
        llm_result["parse_method"] = "llm"
        logger.log_llm_call(rule_result["clean_address"], success=True)
    except Exception as exc:
        logger.stats["llm_failed"] += 1
        logger.log_llm_call(rule_result["clean_address"], success=False)
        return fallback_without_llm(rule_result, f"llm_error:{type(exc).__name__}")

    issues = validate_llm_result(llm_result)
    merged_result = apply_llm_result(rule_result, llm_result)
    if issues:
        merged_result["needs_review"] = 1
        merged_result["rule_reason"] = f'{rule_result["rule_reason"]};{";".join(issues)}'.strip(";")
        return merged_result

    merged_result["needs_review"] = 0
    return merged_result


def process(df, address_column, client, model):
    """批量处理整张表：先解析唯一地址，再回填原始数据行。"""
    print("\n🔄 开始处理地址...")
    
    # 第一步：收集所有唯一地址
    print("📝 步骤 1/4: 收集唯一地址...")
    unique_addresses = []
    seen = set()
    address_to_clean_key = {}  # 存储原始地址到清洗键的映射
    for raw_address in df[address_column].fillna("").astype(str):
        # 先计算清洗后的地址作为唯一键
        clean_key = clean_raw_text(raw_address) or raw_address
        address_to_clean_key[raw_address] = clean_key
        if clean_key not in seen:
            seen.add(clean_key)
            unique_addresses.append(raw_address)
    
    logger.stats["unique"] = len(unique_addresses)
    logger.stats["total"] = len(df)
    print(f"   原始数据: {len(df)} 行")
    print(f"   唯一地址: {len(unique_addresses)} 个")
    
    # 第二步：规则解析唯一地址
    print("\n🔧 步骤 2/4: 规则解析...")
    rule_results = {}  # 使用 clean_key 作为键
    for i, raw_address in enumerate(unique_addresses):
        logger.print_progress(i + 1, len(unique_addresses), "   规则解析进度")
        rule_result = parse_rule(raw_address)
        clean_key = rule_result["clean_address"] or rule_result["raw_address"]
        rule_results[clean_key] = rule_result
        logger.log_rule_parse(raw_address, rule_result)
    
    logger.print_progress(len(unique_addresses), len(unique_addresses), "   规则解析进度")
    print(f"\n   规则通过: {logger.stats['rule_accepted']}/{logger.stats['unique']}")
    print(f"   需LLM处理: {logger.stats['unique'] - logger.stats['rule_accepted']}")
    
    # 第三步：LLM 兜底解析
    print("\n🤖 步骤 3/4: LLM 兜底解析...")
    final_results = {}
    llm_candidates = [k for k, v in rule_results.items() if v["rule_status"] != "accepted"]
    
    if llm_candidates:
        print(f"   需要LLM处理: {len(llm_candidates)} 个地址")
        for i, clean_key in enumerate(llm_candidates):
            logger.print_progress(i + 1, len(llm_candidates), "   LLM解析进度")
            rule_result = rule_results[clean_key]
            final_results[clean_key] = resolve_one(rule_result, client, model)
            if final_results[clean_key]["needs_review"]:
                logger.stats["needs_review"] += 1
    else:
        print("   无需LLM处理")
        for clean_key, rule_result in rule_results.items():
            final_results[clean_key] = rule_result
    
    logger.print_progress(len(llm_candidates), len(llm_candidates), "   LLM解析进度")
    
    # 第四步：回填到原始数据
    print("\n📊 步骤 4/4: 回填结果到原始数据...")
    rows = []
    for idx, row in df.iterrows():
        if idx % 100 == 0:
            logger.print_progress(idx, len(df), "   回填进度")
        raw_address = "" if pd.isna(row[address_column]) else str(row[address_column])
        # 使用之前存储的映射来获取正确的 clean_key
        clean_key = address_to_clean_key.get(raw_address)
        if clean_key is None:
            # 如果映射中不存在，重新计算
            clean_key = clean_raw_text(raw_address) or raw_address
        
        # 从 final_results 中获取解析结果
        parsed = final_results.get(clean_key)
        if parsed is None:
            # 如果还是找不到，记录错误并创建一个默认结果
            print(f"\n   ⚠️  警告: 未找到地址的解析结果: {raw_address[:50]}...")
            parsed = parse_rule(raw_address)
            parsed["needs_review"] = 1
            parsed["rule_reason"] = f"{parsed['rule_reason']};missing_in_final_results"
        
        combined = row.to_dict()
        combined.update(parsed)
        rows.append(combined)
    
    logger.print_progress(len(df), len(df), "   回填进度")
    
    result_df = pd.DataFrame(rows)
    review_df = result_df[result_df["needs_review"] == 1].copy()
    
    return result_df, review_df


def main():
    """脚本入口：读取参数，执行处理，并输出全量结果与待复核结果。"""
    logger.print_header()
    logger.start_time = time.time()
    
    # 检查输入文件
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 错误: 输入文件不存在: {INPUT_FILE}")
        return
    
    # 读取 Excel 文件
    print("📂 正在读取 Excel 文件...")
    try:
        df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME)
        print(f"   ✅ 成功读取 {len(df)} 行数据")
    except Exception as e:
        print(f"   ❌ 读取失败: {e}")
        return
    
    if ADDRESS_COLUMN not in df.columns:
        print(f"   ❌ 未找到地址列: {ADDRESS_COLUMN}")
        return
    print(f"   📍 地址列: {ADDRESS_COLUMN}")
    
    # 初始化 LLM 客户端
    print("\n🔌 初始化 LLM 客户端...")
    client, unavailable_reason = get_anthropic_client()
    if unavailable_reason:
        print(f"   ⚠️  LLM 未启用: {unavailable_reason}")
    else:
        print(f"   ✅ LLM 客户端初始化成功，模型: {MODEL_NAME}")
    
    # 处理数据 - 注意这里移除了 cache 参数
    result_df, review_df = process(df, ADDRESS_COLUMN, client, MODEL_NAME)
    
    # 保存结果
    print("\n💿 保存结果...")
    result_df.to_excel(OUTPUT_FILE, index=False)
    print(f"   ✅ 全量结果: {OUTPUT_FILE} ({len(result_df)} 行)")
    
    review_df.to_excel(REVIEW_FILE, index=False)
    print(f"   ✅ 待复核结果: {REVIEW_FILE} ({len(review_df)} 行)")
    
    # 打印统计信息
    logger.print_stats()
    
    # 打印几个结果示例
    print("📋 结果示例 (前3条):")
    for i in range(min(3, len(result_df))):
        logger.print_sample_result(result_df.iloc[i], i)
    
    print("\n✨ 处理完成！")


if __name__ == "__main__":
    main()
