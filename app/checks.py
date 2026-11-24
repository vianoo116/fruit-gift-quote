"""程序规则：可用商品筛选、不可行预检、价格微调、方案校验。"""
from __future__ import annotations

import math
import re

from .inventory import Item, fruits, load_inventory
from .pricing import price_plan
from .rules import (MIN_FRUIT_KINDS, MIN_FRUIT_WEIGHT_JIN, NEAR_EXPIRY_DAYS, TIER_PACKAGING, TIERS,
                    min_shelf_days, taboo_skus)


def excluded_skus(req: dict) -> dict[str, str]:
    """忌讳表 + 顾客明确不要的商品。"""
    out = taboo_skus(req["occasion"])
    inv = load_inventory()
    for s in req.get("exclude_skus") or []:
        if s in inv:
            out.setdefault(s, "顾客明确不要")
    return out


def allowed_fruits(req: dict) -> tuple[list[Item], dict[str, str]]:
    """返回可用水果，以及每个被排除商品的原因。"""
    q = req["quantity"]
    excl = excluded_skus(req)
    min_shelf = min_shelf_days(req.get("event_in_days"))
    allowed, reasons = [], {}
    for it in fruits():
        if not it.is_gift_fruit:
            reasons[it.sku_id] = "不适合做礼盒"
        elif it.sku_id in excl:
            reasons[it.sku_id] = excl[it.sku_id]
        elif it.shelf_days_left < min_shelf:
            reasons[it.sku_id] = f"剩余保鲜 {it.shelf_days_left} 天，不足 {min_shelf} 天"
        elif it.stock < q * it.step:
            reasons[it.sku_id] = f"库存 {it.stock:g}{it.unit} 不够 {q} 份"
        else:
            allowed.append(it)
    return allowed, reasons


def priority_ids(allowed: list[Item]) -> set[str]:
    """优先清库存：剩余保鲜天数短（≤7 天）且库存较多（≥80）的水果。"""
    return {it.sku_id for it in allowed if it.shelf_days_left <= 7 and it.stock >= 80}


def _min_box_price(allowed: list[Item]) -> float | None:
    """最便宜的合规礼盒价格（标准包装 + 两种最便宜的按斤水果凑够 3 斤）。"""
    inv = load_inventory()
    jin = sorted([it for it in allowed if it.unit == "斤"], key=lambda i: i.wholesale_price)
    if len(jin) < MIN_FRUIT_KINDS:
        return None
    return inv["P01"].wholesale_price + jin[0].wholesale_price * 2 + jin[1].wholesale_price * 1


def precheck(req: dict, targets: dict) -> tuple[list[str], list[str]]:
    """明显不可行的需求直接拦下，返回 (原因, 替代建议)。"""
    inv = load_inventory()
    q = req["quantity"]
    reasons, suggestions = [], []
    allowed, excl_reasons = allowed_fruits(req)
    std_high = targets["标准"]["high"]
    min_shelf = min_shelf_days(req.get("event_in_days"))

    req_price = 0.0
    for r in req.get("required_items") or []:
        it = inv.get(r["sku_id"])
        if it is None:
            continue
        need = float(r["qty"]) * q
        if need > it.stock:
            reasons.append(f"{it.name}库存只有 {it.stock:g}{it.unit}，{q} 份每份 {r['qty']:g}{it.unit} 需要 {need:g}{it.unit}，库存不够")
            max_boxes = int(it.stock // float(r["qty"]))
            suggestions.append(f"如果每份必须放{r['qty']:g}{it.unit}{it.name}，最多能做 {max_boxes} 份；或改为部分礼盒放{it.name}")
        if it.shelf_days_left < min_shelf:
            reasons.append(f"{it.name}剩余保鲜只有 {it.shelf_days_left} 天，不满足送达后至少 {min_shelf} 天的要求")
        if not it.is_gift_fruit:
            reasons.append(f"{it.name}不适合做礼盒")
        if r["sku_id"] in excluded_skus(req):
            reasons.append(f"{it.name}与场合忌讳冲突：{excluded_skus(req)[r['sku_id']]}")
        req_price += it.wholesale_price * float(r["qty"])
    if req_price and req_price + inv["P01"].wholesale_price > std_high:
        need = req_price + inv["P01"].wholesale_price
        reasons.append(f"必放商品加标准包装单份已需 {need:.2f} 元，超过预算上限 {std_high:.2f} 元")
        suggestions.append(f"把每份预算提高到 {math.ceil(need * 1.2)} 元左右，或减少必放商品的数量")

    min_price = _min_box_price(allowed)
    if min_price is None:
        reasons.append("可用水果不足两种（受忌讳、保鲜期或库存限制）")
        suggestions.append("减少份数或放宽日期要求")
    elif min_price > std_high:
        reasons.append(f"预算偏低：标准包装 {inv['P01'].wholesale_price:g} 元加最基本的 3 斤水果，单份至少要 {min_price:.2f} 元，超过预算上限 {std_high:.2f} 元")
        suggestions.append(f"把每份预算提高到 {math.ceil(min_price / 10) * 10} 元以上；或不要礼盒包装，改为散装水果袋")
    return reasons, suggestions


def _weight(items: list[dict]) -> float:
    inv = load_inventory()
    return sum(inv[i["sku_id"]].weight_jin * float(i["qty"]) for i in items)


def tune_quantities(items: list[dict], packaging_sku: str, quantity: int, target: dict,
                    allowed_ids: set[str], prio: set[str]) -> list[dict]:
    """程序微调用量，让单份价格落入目标区间（模型估价不准时兜底）。只调整已选商品的数量，不换品。"""
    inv = load_inventory()
    items = [{"sku_id": i["sku_id"], "qty": float(i["qty"])} for i in items]

    def price(its):
        return price_plan(its, packaging_sku, quantity)["unit_price"]

    for _ in range(120):
        p = price(items)
        if target["low"] <= p <= target["high"]:
            break
        if p < target["low"]:
            # 加量：优先加临期优先品，其次单价合适的
            cands = [i for i in items if i["sku_id"] in allowed_ids
                     and (i["qty"] + inv[i["sku_id"]].step) * quantity <= inv[i["sku_id"]].stock
                     and p + inv[i["sku_id"]].wholesale_price * inv[i["sku_id"]].step <= target["high"]]
            if not cands:
                cands = [i for i in items if i["sku_id"] in allowed_ids
                         and (i["qty"] + inv[i["sku_id"]].step) * quantity <= inv[i["sku_id"]].stock]
                if not cands:
                    break
            cands.sort(key=lambda i: (i["sku_id"] not in prio, -inv[i["sku_id"]].wholesale_price * inv[i["sku_id"]].step
                                      if p + inv[i["sku_id"]].wholesale_price * inv[i["sku_id"]].step <= target["high"] else 1e9))
            c = cands[0]
            c["qty"] += inv[c["sku_id"]].step
        else:
            # 减量：先减最贵的、且减完仍满足最低配置的
            cands = []
            for i in items:
                step = inv[i["sku_id"]].step
                if i["qty"] - step <= 0:
                    continue
                trial = [dict(x) for x in items]
                for x in trial:
                    if x["sku_id"] == i["sku_id"]:
                        x["qty"] -= step
                if _weight(trial) >= MIN_FRUIT_WEIGHT_JIN:
                    cands.append(i)
            if not cands:
                break
            cands.sort(key=lambda i: -inv[i["sku_id"]].wholesale_price * inv[i["sku_id"]].step)
            # 选一个减完后最接近区间的
            best = None
            for c in cands:
                newp = p - inv[c["sku_id"]].wholesale_price * inv[c["sku_id"]].step
                if newp >= target["low"]:
                    best = c
                    break
            c = best or cands[0]
            c["qty"] -= inv[c["sku_id"]].step
    return items


DIGIT_RE = re.compile(r"[0-9０-９]|元|块钱|￥|¥")


def check_tiers(req: dict, tiers: list[dict], targets: dict, check_copy: bool = False) -> tuple[list[str], dict]:
    """程序规则校验。返回 (硬性问题列表, 每档 {issues, warnings})。"""
    inv = load_inventory()
    q = req["quantity"]
    excl = excluded_skus(req)
    min_shelf = min_shelf_days(req.get("event_in_days"))
    problems: list[str] = []
    per_tier: dict[str, dict] = {}
    for t in tiers:
        name = t["tier"]
        issues, warnings = [], []
        items = t["items"]
        if t["packaging"] != TIER_PACKAGING[name]:
            issues.append(f"包装应为 {TIER_PACKAGING[name]}")
        seen = set()
        for i in items:
            s = i["sku_id"]
            it = inv.get(s)
            if it is None or it.is_packaging:
                issues.append(f"商品 {s} 不在库存表中")
                continue
            if s in seen:
                issues.append(f"{it.name}重复出现")
            seen.add(s)
            if float(i["qty"]) <= 0:
                issues.append(f"{it.name}用量必须大于 0")
            if not it.is_gift_fruit:
                issues.append(f"{it.name}不适合做礼盒")
            if s in excl:
                issues.append(f"{it.name}不能用：{excl[s]}")
            if float(i["qty"]) * q > it.stock:
                issues.append(f"{it.name}库存不足：需要 {float(i['qty']) * q:g}{it.unit}，库存 {it.stock:g}{it.unit}")
            if it.shelf_days_left < min_shelf:
                issues.append(f"{it.name}剩余保鲜 {it.shelf_days_left} 天，不足 {min_shelf} 天")
        if any(i["sku_id"] not in inv for i in items):
            per_tier[name] = {"issues": issues, "warnings": warnings}
            problems += [f"{name}款：{x}" for x in issues]
            continue
        if len(seen) < MIN_FRUIT_KINDS:
            issues.append(f"至少要 {MIN_FRUIT_KINDS} 种水果")
        if _weight(items) < MIN_FRUIT_WEIGHT_JIN - 1e-6:
            issues.append(f"水果合计不足 {MIN_FRUIT_WEIGHT_JIN:g} 斤")
        for r in req.get("required_items") or []:
            got = sum(float(i["qty"]) for i in items if i["sku_id"] == r["sku_id"])
            if got < float(r["qty"]):
                issues.append(f"缺少顾客要求必放的 {inv[r['sku_id']].name} {r['qty']:g}{inv[r['sku_id']].unit}")
        plan = t["plan"]
        tg = targets[name]
        if not (tg["low"] - 1e-6 <= plan["unit_price"] <= tg["high"] + 1e-6):
            msg = f"单份价格 {plan['unit_price']:.2f} 元不在目标区间 {tg['low']:.2f}–{tg['high']:.2f} 元"
            (issues if name == "标准" else warnings).append(msg)
        if plan["margin_warning"]:
            warnings.append(f"毛利率 {plan['gross_margin'] * 100:.0f}% 低于 20% 底线")
        if check_copy:
            for field in ("intro", "blessing"):
                if DIGIT_RE.search(t.get(field, "") or ""):
                    issues.append(f"文案中出现了数字或价格：{t.get(field)}")
        per_tier[name] = {"issues": issues, "warnings": warnings}
        problems += [f"{name}款：{x}" for x in issues]
    prices = {t["tier"]: t["plan"]["unit_price"] for t in tiers if "plan" in t}
    if len(prices) == 3 and not (prices["经济"] < prices["标准"] < prices["高档"]):
        for t in TIERS:
            per_tier.setdefault(t, {"issues": [], "warnings": []})["warnings"].append("三档价格未拉开梯度")
    return problems, per_tier


def near_expiry_ratio(tiers: list[dict]) -> float:
    inv = load_inventory()
    total = near = 0.0
    for t in tiers:
        for i in t["items"]:
            w = inv[i["sku_id"]].weight_jin * float(i["qty"])
            total += w
            if inv[i["sku_id"]].shelf_days_left <= NEAR_EXPIRY_DAYS:
                near += w
    return near / total if total else 0.0
