"""业务规则表：忌讳、档位定价、保鲜期、礼盒最低配置。全部写死在代码里，由程序校验，不依赖模型判断。"""
from __future__ import annotations

# ---- 忌讳规则表：场合 -> 不能用的 sku 及原因 ----
TABOO_RULES: dict[str, dict[str, str]] = {
    "探病": {"F13": "梨=离，探病不宜"},
    "婚庆": {"F13": "梨=离，婚庆不宜"},
    "乔迁": {"F13": "梨=离，乔迁不宜"},
}

# 场合别名归一化（需求理解 Agent 输出的场合可能有不同说法）
OCCASION_ALIASES: dict[str, str] = {
    "看望病人": "探病", "住院": "探病", "探望病人": "探病", "慰问病人": "探病",
    "结婚": "婚庆", "婚礼": "婚庆", "喜宴": "婚庆", "订婚": "婚庆",
    "搬家": "乔迁", "乔迁新居": "乔迁", "新居": "乔迁",
}


def normalize_occasion(occasion: str | None) -> str:
    if not occasion:
        return "未说明"
    occasion = occasion.strip()
    if occasion in TABOO_RULES:
        return occasion
    for alias, canon in OCCASION_ALIASES.items():
        if alias in occasion:
            return canon
    for canon in TABOO_RULES:
        if canon in occasion:
            return canon
    return occasion


def taboo_skus(occasion: str) -> dict[str, str]:
    return dict(TABOO_RULES.get(normalize_occasion(occasion), {}))


# ---- 三档定价（店主确认：经济 0.8 / 标准 1.0 / 高档 1.3 倍预算）----
TIERS = ["经济", "标准", "高档"]
TIER_RATIO = {"经济": 0.8, "标准": 1.0, "高档": 1.3}
TIER_PACKAGING = {"经济": "P01", "标准": "P01", "高档": "P02"}
# 没给预算时按常见档位（店主确认：100 / 200 / 300）
DEFAULT_TIER_PRICES = {"经济": 100.0, "标准": 200.0, "高档": 300.0}

BUDGET_TOLERANCE = 0.10  # 预算 ±10%
MARGIN_FLOOR = 0.20  # 毛利率底线


def tier_targets(budget: float | None, budget_type: str | None) -> dict[str, dict]:
    """每档的目标单份价格及允许区间。

    - 标准款：预算 ±10%；顾客说「以内/不超过」时为预算的 90%–100%（店主确认）。
    - 经济、高档：目标价 ±10%，只作为软目标。
    """
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
NEAR_EXPIRY_DAYS = 5  # 剩余保鲜 ≤ 5 天视为临期（用于统计和优先使用）


def min_shelf_days(event_in_days: int | None) -> int:
    d = DEFAULT_EVENT_IN_DAYS if event_in_days is None else max(0, int(event_in_days))
    return d + SHELF_BUFFER_DAYS


# ---- 礼盒最低配置：至少 2 种水果、水果合计至少 3 斤（个/盒按估算重量）----
MIN_FRUIT_KINDS = 2
MIN_FRUIT_WEIGHT_JIN = 3.0

# 健康相关需求：只做参考提示，不做医疗建议
HEALTH_KEYWORDS = ["糖尿病", "血糖", "控糖", "过敏", "孕妇", "怀孕", "肾病", "生病忌口"]
HEALTH_DISCLAIMER = "涉及健康/饮食限制的搭配仅供参考，不构成医疗建议，请以医生意见为准。"


# ---- 商品俗称：用于核对"顾客点名不要的水果" ----
SKU_ALIASES: dict[str, list[str]] = {
    "F01": ["葡萄", "阳光玫瑰"], "F02": ["橙", "脐橙"], "F03": ["砂糖橘", "橘子"], "F04": ["苹果"],
    "F05": ["草莓"], "F06": ["车厘子", "樱桃"], "F07": ["车厘子", "樱桃"], "F08": ["榴莲"],
    "F09": ["凤梨", "菠萝"], "F10": ["金果", "猕猴桃", "奇异果"], "F11": ["火龙果"], "F12": ["猕猴桃", "奇异果"],
    "F13": ["梨"], "F14": ["蜜瓜", "哈密瓜"], "F15": ["西瓜"], "F16": ["香蕉"], "F17": ["沃柑"],
    "F18": ["耙耙柑"], "F19": ["果冻橙", "橙"], "F20": ["山竹"], "F21": ["牛油果"], "F22": ["冬枣", "枣"],
    "F23": ["石榴"], "F24": ["柿子", "柿"], "F25": ["椰青", "椰子"], "F26": ["蓝莓"], "F27": ["柚子", "柚"],
    "F28": ["金桔"], "F29": ["百香果"],
}
NEGATION_WORDS = ["不要", "别放", "不放", "不爱吃", "不吃", "不喜欢", "忌", "去掉"]


def _mentions(text: str, sku: str) -> bool:
    """顾客原话里是否提到了这个商品（"梨"不算"凤梨"）。"""
    for alias in SKU_ALIASES.get(sku, []):
        t = text.replace("凤梨", "") if alias == "梨" else text
        if alias in t:
            return True
    return False


def reconcile_exclusions(text: str, llm_excludes: list[str]) -> list[str]:
    out = [s for s in llm_excludes if _mentions(text, s)]
    if any(w in text for w in NEGATION_WORDS):
        for sku, aliases in SKU_ALIASES.items():
            for alias in aliases:
                t = text.replace("凤梨", "") if alias == "梨" else text
                if any(f"{w}{alias}" in t for w in NEGATION_WORDS) and sku not in out:
                    out.append(sku)
    return out
