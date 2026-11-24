"""业务规则表（来自经营者访谈，见 docs/scene_rules.md）。全部写死在代码里，由程序校验和排序，不依赖模型判断。"""
from __future__ import annotations

# ---------------- 场景规则表 ----------------
# grades：该场景偏好的档次（越靠前越好）；packaging：默认包装风格；taboo：不能用的商品；
# prefer：寓意好、优先推荐的商品；small_box_first：份数多时优先伴手礼小盒
SCENES: dict[str, dict] = {
    "商务送礼": {"grades": ["高档", "精品"], "value": "高，看起来要贵重", "packaging": "大气",
             "taboo": {}, "prefer": ["G01", "G02", "G08", "G15"], "avoid_grades": ["普通"],
             "note": "进口、精品优先；不推荐普通档"},
    "探病": {"grades": ["普通", "精品"], "value": "一般、实惠即可", "packaging": "实在",
           "taboo": {"G14": "梨=离，探病不宜", "F13": "梨=离，探病不宜"}, "prefer": ["G06", "G09", "F04"],
           "note": "不送梨；优先寓意平安（苹果、柚子）"},
    "婚庆": {"grades": ["普通", "精品"], "value": "性价比高", "packaging": "精美",
           "taboo": {"G14": "梨=离，婚庆不宜", "F13": "梨=离，婚庆不宜"}, "prefer": ["G19", "G20", "G13", "F22"],
           "small_box_first": True, "note": "不送梨；份数多时优先伴手礼小盒；寓意吉利、甜蜜、早生贵子"},
    "公司福利": {"grades": ["普通", "精品"], "value": "性价比高", "packaging": "实在",
             "taboo": {}, "prefer": ["G04", "G06", "G09", "G10"], "note": "选大众都能接受的；量大时注意库存"},
    "节日送礼": {"grades": ["精品", "普通"], "value": "中等", "packaging": "大气",
             "taboo": {}, "prefer": ["G04", "G05", "G06", "G09", "G19"], "note": "吉利寓意（橙、橘、柚、苹果）"},
    "祝寿": {"grades": ["精品", "高档"], "value": "中高", "packaging": "大气",
           "taboo": {}, "prefer": ["G06", "G09", "G12", "G08"], "note": "寓意平安、长寿"},
    "开业": {"grades": ["精品", "普通"], "value": "中等", "packaging": "大气",
           "taboo": {}, "prefer": ["G16", "G04", "G05"], "note": "旺来（凤梨）、心想事成（橙）"},
    "乔迁": {"grades": ["普通", "精品"], "value": "中等", "packaging": "实在",
           "taboo": {"G14": "梨=离，乔迁不宜", "F13": "梨=离，乔迁不宜"}, "prefer": ["G06", "G09"],
           "note": "不送梨；寓意平安"},
    "未说明": {"grades": [], "value": "按预算", "packaging": None, "taboo": {}, "prefer": [],
            "note": "先按预算给三档，并提示询问用途"},
}

# 需求理解 Agent 输出的场合 → 规则表里的场景（表外场景按最接近的场景处理，见 README）
OCCASION_TO_SCENE: dict[str, str] = {
    "商务送礼": "商务送礼", "拜访领导": "商务送礼", "求人办事": "商务送礼", "客户": "商务送礼",
    "探病": "探病", "看望病人": "探病", "住院": "探病",
    "婚庆": "婚庆", "结婚": "婚庆", "婚礼": "婚庆", "回礼": "婚庆", "伴手礼": "婚庆", "满月": "婚庆",
    "公司福利": "公司福利", "公司活动": "公司福利", "员工": "公司福利", "年会": "公司福利",
    "春节走亲": "节日送礼", "节日送礼": "节日送礼", "节日慰问": "节日送礼", "孝敬长辈": "节日送礼", "送礼": "节日送礼",
    "祝寿": "祝寿", "寿": "祝寿",
    "开业": "开业",
    "乔迁": "乔迁", "搬家": "乔迁", "新居": "乔迁",
}
# 满月回礼参照婚庆伴手礼的档次和包装，但「不送梨」只对婚庆/探病/乔迁生效
SCENE_TABOO_EXEMPT = {"满月"}


def scene_of(occasion: str | None) -> str:
    if not occasion:
        return "未说明"
    for key, scene in OCCASION_TO_SCENE.items():
        if key in occasion:
            return scene
    return "未说明"


def scene_rule(occasion: str | None) -> dict:
    return SCENES[scene_of(occasion)]


def taboo_skus(occasion: str | None) -> dict[str, str]:
    if occasion and any(x in occasion for x in SCENE_TABOO_EXEMPT):
        return {}
    return dict(scene_rule(occasion)["taboo"])


# ---- 包装偏好：顾客原话里的关键词 → 包装风格 ----
PACKAGING_KEYWORDS = {
    "大气": ["大气", "有面子", "上档次", "体面", "气派"],
    "实在": ["实在", "量多", "划算", "实惠"],
    "精美": ["好看", "精致", "精美", "漂亮"],
}


def packaging_pref(text: str) -> str | None:
    for style, words in PACKAGING_KEYWORDS.items():
        if any(w in text for w in words):
            return style
    return None


ASSEMBLY_KEYWORDS = ["组装", "混装", "自己搭配", "自己配", "搭配几样", "拼一个", "拼个"]


def wants_assembly(text: str) -> bool:
    return any(w in text for w in ASSEMBLY_KEYWORDS)


# ---- 三档定价（店主确认：经济 0.8 / 标准 1.0 / 高档 1.3 倍预算）----
TIERS = ["经济", "标准", "高档"]
TIER_RATIO = {"经济": 0.8, "标准": 1.0, "高档": 1.3}
DEFAULT_TIER_PRICES = {"经济": 100.0, "标准": 200.0, "高档": 300.0}  # 没给预算时
BUDGET_TOLERANCE = 0.10
MARGIN_FLOOR = 0.20

# 组装礼盒包装：经济、标准用标准包装，高档用高档包装
ASSEMBLY_PACKAGING = {"经济": "P01", "标准": "P01", "高档": "P02"}


def tier_targets(budget: float | None, budget_type: str | None) -> dict[str, dict]:
    """每档的目标单份价格及允许区间。标准款：预算 ±10%；「以内」时为预算的 90%–100%。"""
    out = {}
    if budget is None:
        for t, p in DEFAULT_TIER_PRICES.items():
            out[t] = {"target": p, "low": round(p * 0.9, 2), "high": round(p * 1.1, 2)}
        return out
    for t in TIERS:
        target = budget * TIER_RATIO[t]
        low, high = target * (1 - BUDGET_TOLERANCE), target * (1 + BUDGET_TOLERANCE)
        if t == "标准" and budget_type == "max":
            target, low, high = budget * 0.95, budget * 0.9, budget
        if t == "经济" and budget_type == "max":
            high = min(high, budget * 0.9)
        out[t] = {"target": round(target, 2), "low": round(low, 2), "high": round(high, 2)}
    return out


# ---- 保鲜期（店主确认：距使用日 + 2 天；没说日期默认 1 天内送达，即 ≥ 3 天）----
SHELF_BUFFER_DAYS = 2
DEFAULT_EVENT_IN_DAYS = 1
NEAR_EXPIRY_DAYS = 5


def min_shelf_days(event_in_days: int | None) -> int:
    d = DEFAULT_EVENT_IN_DAYS if event_in_days is None else max(0, int(event_in_days))
    return d + SHELF_BUFFER_DAYS


# ---- 组装礼盒规则：每档 3–5 种、单盒 4–8 斤、同一种水果不能放两个规格 ----
ASSEMBLY_MIN_KINDS, ASSEMBLY_MAX_KINDS = 3, 5
ASSEMBLY_MIN_JIN, ASSEMBLY_MAX_JIN = 4.0, 8.0
ASSEMBLY_REASON = "整箱原包装礼盒更新鲜、更体面、运输中不易磕碰"

# ---- 榴莲：按个零售，不属于礼盒 ----
DURIAN_SKU = "F08"
DURIAN_MSG = "榴莲按个零售，不放进礼盒；如需要可以单独称重报价"

HEALTH_KEYWORDS = ["糖尿病", "血糖", "控糖", "过敏", "孕妇", "怀孕", "肾病", "忌口"]
HEALTH_DISCLAIMER = "涉及健康/饮食限制的搭配仅供参考，不构成医疗建议，请以医生意见为准。"
# 顾客提到糖尿病/控糖等时，这些高糖水果直接不进候选（仅作参考，不是医疗建议）
HIGH_SUGAR_SPECIES = ["葡萄", "阳光玫瑰", "冬枣", "柿", "蜜瓜", "砂糖橘", "金桔", "山竹", "果冻橙", "耙耙柑", "车厘子", "石榴", "草莓"]
SUGAR_KEYWORDS = ["糖尿病", "血糖", "控糖", "少放特别甜", "不要太甜", "少糖", "低糖"]

# ---- 商品俗称：用于核对「顾客点名不要 / 必须要」的水果 ----
SKU_ALIASES: dict[str, list[str]] = {
    "G01": ["车厘子", "樱桃", "JJ"], "G02": ["车厘子", "樱桃", "JJJ"], "G03": ["葡萄", "阳光玫瑰"],
    "G04": ["脐橙", "橙"], "G05": ["果冻橙", "橙"], "G06": ["苹果"], "G07": ["草莓"], "G08": ["金果", "猕猴桃", "奇异果"],
    "G09": ["柚子", "柚"], "G10": ["耙耙柑"], "G11": ["山竹"], "G12": ["石榴"], "G13": ["冬枣", "枣"],
    "G14": ["梨"], "G15": ["蓝莓"], "G16": ["凤梨", "菠萝"], "G17": ["柿子", "柿"], "G18": ["蜜瓜", "哈密瓜"],
    "G19": ["砂糖橘", "橘子"], "G20": ["金桔"],
    "F01": ["葡萄", "阳光玫瑰"], "F02": ["脐橙", "橙"], "F03": ["砂糖橘", "橘子"], "F04": ["苹果"],
    "F06": ["车厘子", "樱桃"], "F08": ["榴莲"], "F10": ["金果", "猕猴桃", "奇异果"], "F11": ["火龙果"],
    "F13": ["梨"], "F19": ["果冻橙", "橙"], "F20": ["山竹"], "F22": ["冬枣", "枣"],
}
NEGATION_WORDS = ["不要", "别放", "不放", "不爱吃", "不吃", "不喜欢", "忌", "去掉"]


def _clean(text: str, alias: str) -> str:
    return text.replace("凤梨", "") if alias == "梨" else text


def mentions(text: str, sku: str) -> bool:
    """顾客原话里是否提到了这个商品（「梨」不算「凤梨」）。"""
    return any(a in _clean(text, a) for a in SKU_ALIASES.get(sku, []))


def negated(text: str, sku: str) -> bool:
    return any(f"{w}{a}" in _clean(text, a) for a in SKU_ALIASES.get(sku, []) for w in NEGATION_WORDS)


def reconcile_exclusions(text: str, llm_excludes: list[str]) -> list[str]:
    """用名称核对模型输出：补漏（「别放梨」）并去掉误判（梨≠凤梨）。"""
    out = [s for s in llm_excludes if mentions(text, s)]
    for sku in SKU_ALIASES:
        if negated(text, sku) and sku not in out:
            out.append(sku)
    return out


def wants_durian(text: str) -> bool:
    return mentions(text, DURIAN_SKU) and not negated(text, DURIAN_SKU)
