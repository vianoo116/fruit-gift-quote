"""程序规则：候选筛选与排序、不可行预检、组装用量微调、方案校验。能用规则做的都在这里做，减少模型调用。"""
from __future__ import annotations

import math
import re
from itertools import combinations

from .inventory import Item, boxes, bulk_fruits, load_inventory
from .pricing import price_plan
from .rules import (ASSEMBLY_MAX_JIN, ASSEMBLY_MAX_KINDS, ASSEMBLY_MIN_JIN, ASSEMBLY_MIN_KINDS,
                    ASSEMBLY_PACKAGING, DURIAN_SKU, HEALTH_KEYWORDS, HIGH_SUGAR_SPECIES, NEAR_EXPIRY_DAYS, SUGAR_KEYWORDS,
                    TIERS, min_shelf_days, scene_rule, taboo_skus)

KIND_LABEL = {"single": "单款整箱", "main_small": "主礼盒 + 伴手礼小盒", "two_main": "两款整箱组合"}


# ---------------- 通用 ----------------
def excluded_skus(req: dict) -> dict[str, str]:
    out = taboo_skus(req["occasion"])
    out.setdefault(DURIAN_SKU, "榴莲按个零售，不属于礼盒")
    inv = load_inventory()
    for s in req.get("exclude_skus") or []:
        if s in inv:
            out.setdefault(s, "顾客明确不要")
    return out


def _filter(req: dict, items: list[Item], need_per_box: float = 1.0) -> tuple[list[Item], dict[str, str]]:
    q = req["quantity"]
    excl = excluded_skus(req)
    min_shelf = min_shelf_days(req.get("event_in_days"))
    low_sugar = any(k in req.get("raw_text", "") for k in SUGAR_KEYWORDS)
    allowed, reasons = [], {}
    for it in items:
        need = q * (it.step if it.is_bulk else need_per_box)
        if it.sku_id in excl:
            reasons[it.sku_id] = excl[it.sku_id]
        elif low_sugar and any(h in it.name for h in HIGH_SUGAR_SPECIES):
            reasons[it.sku_id] = "顾客要求少糖，避开高糖水果"
        elif it.shelf_days_left < min_shelf:
            reasons[it.sku_id] = f"剩余保鲜 {it.shelf_days_left} 天，不足 {min_shelf} 天"
        elif it.stock < need:
            reasons[it.sku_id] = f"库存 {it.stock:g}{it.unit}，不够 {q} 份"
        else:
            allowed.append(it)
    return allowed, reasons


def allowed_boxes(req: dict):
    return _filter(req, boxes())


def allowed_bulk(req: dict):
    return _filter(req, bulk_fruits())


def is_health(req: dict) -> bool:
    return any(k in req.get("raw_text", "") for k in HEALTH_KEYWORDS)


# ---------------- 整箱礼盒：候选组合 ----------------
def box_options(req: dict, allowed: list[Item]) -> list[dict]:
    """所有合规的「一份」组合：单款整箱、主礼盒 + 伴手礼小盒、两款不同整箱（店主确认可组合，最多 2 款）。"""
    required = {r["sku_id"] for r in req.get("required_items") or []}
    opts = []

    def add(items: list[Item], kind: str):
        skus = {i.sku_id for i in items}
        if not required <= skus:
            return
        main = max(items, key=lambda i: i.wholesale_price)
        opts.append({"items": [{"sku_id": i.sku_id, "qty": 1} for i in sorted(items, key=lambda i: -i.wholesale_price)],
                     "price": round(sum(i.wholesale_price for i in items), 2), "kind": kind, "main": main.sku_id})

    for a in allowed:
        add([a], "single")
    mains = [a for a in allowed if not a.is_small_box]
    smalls = [a for a in allowed if a.is_small_box]
    for m in mains:
        for s in smalls:
            add([m, s], "main_small")
    for a, b in combinations(mains, 2):
        add([a, b], "two_main")
    for i, o in enumerate(sorted(opts, key=lambda o: o["price"])):
        o["id"] = f"O{i + 1}"
    return sorted(opts, key=lambda o: o["price"])


def _pref_style(req: dict) -> tuple[str | None, bool]:
    """返回 (偏好包装风格, 是否顾客亲口说的)。"""
    if req.get("packaging_pref"):
        return req["packaging_pref"], True
    return scene_rule(req["occasion"]).get("packaging"), False


def score_option(o: dict, tg: dict, req: dict) -> float:
    """越小越好：先看是否落在目标价区间，再看场景档次、包装风格、寓意、临期清货等加减分。"""
    inv = load_inventory()
    rule = scene_rule(req["occasion"])
    main = inv[o["main"]]
    in_win = tg["low"] - 1e-6 <= o["price"] <= tg["high"] + 1e-6
    s = 0.0 if in_win else 1.0
    s += abs(o["price"] - tg["target"]) / tg["target"] * 0.5
    s += {"single": 0.0, "main_small": 0.04, "two_main": 0.12}[o["kind"]]
    grades = rule.get("grades") or []
    if main.grade in grades:
        s -= 0.08 if grades.index(main.grade) == 0 else 0.04
    if main.grade in (rule.get("avoid_grades") or []):
        s += 0.10
    if req.get("wants_premium") and main.grade == "高档":
        s -= 0.08
    style, explicit = _pref_style(req)
    if style and main.packaging_style == style:
        s -= 0.10 if explicit else 0.05
    skus = [i["sku_id"] for i in o["items"]]
    if any(x in (rule.get("prefer") or []) for x in skus):
        s -= 0.04
    if rule.get("small_box_first") and req["quantity"] >= 20 and any(inv[x].is_small_box for x in skus):
        s -= 0.05
    if main.shelf_days_left <= 7 and main.stock >= 60:  # 临期且库存多，优先清货
        s -= 0.02
    if is_health(req) and any(any(h in inv[x].name for h in HIGH_SUGAR_SPECIES) for x in skus):
        s += 0.12
    return s


def shortlist(req: dict, options: list[dict], targets: dict, k: int = 5) -> dict[str, list[dict]]:
    """每档的候选短名单。「两款整箱组合」只在该档没有单款/主+小礼盒落进区间时才进入候选。"""
    out = {}
    for t in TIERS:
        tg = targets[t]

        def in_win(o):
            return tg["low"] - 1e-6 <= o["price"] <= tg["high"] + 1e-6
        simple_fit = any(in_win(o) for o in options if o["kind"] != "two_main")
        pool = [o for o in options if not (simple_fit and o["kind"] == "two_main")]
        out[t] = sorted(pool, key=lambda o: score_option(o, tg, req))[:k]
    return out


def window_reachable(options: list[dict], tg: dict) -> bool:
    return any(tg["low"] - 1e-6 <= o["price"] <= tg["high"] + 1e-6 for o in options)


def arrange_tiers(picks: dict[str, dict | None], options: list[dict], targets: dict, req: dict) -> tuple[dict, list[str]]:
    """程序兜底：保证标准款尽量落进预算、三档不重复且价格递增；实在没有合适的档位就空缺并说明。"""
    notes = []
    std = picks.get("标准")
    tg = targets["标准"]
    best_dist = min((abs(o["price"] - tg["target"]) for o in options), default=0)
    out_of_win = std is not None and not (tg["low"] - 1e-6 <= std["price"] <= tg["high"] + 1e-6)
    # 区间内有方案却选了区间外的；或区间内本来就没有方案，但 AI 没选最接近预算的
    if std is None or (out_of_win and (window_reachable(options, tg) or abs(std["price"] - tg["target"]) > best_dist + 1e-6)):
        best = shortlist(req, options, targets, k=1)["标准"]
        std = best[0] if best else None
        if std is not None and picks.get("标准") is not None:
            notes.append("标准款：AI 所选不在预算区间，已由程序替换为最匹配的礼盒")
    out = {"标准": std}
    used = {std["id"]} if std else set()
    for t, cmp in (("经济", lambda p: p < std["price"]), ("高档", lambda p: p > std["price"])):
        p = picks.get(t)
        if std is None:
            out[t] = None
            continue
        if p is None or p["id"] in used or not cmp(p["price"]):
            cands = [o for o in options if o["id"] not in used and cmp(o["price"])]
            p = min(cands, key=lambda o: score_option(o, targets[t], req)) if cands else None
            if picks.get(t) is not None:
                notes.append(f"{t}款：AI 所选与其他档重复或价格顺序不对，已由程序调整")
        if p is None:
            notes.append(f"{t}款：库存和价位里没有合适的礼盒，本次只给出其余档位")
        out[t] = p
        if p:
            used.add(p["id"])
    return out, notes


def alternatives_for(req: dict, targets: dict, n: int = 2, mentioned_text: str = "") -> list[dict]:
    """推荐可代替的整箱礼盒（组装请求 / 榴莲请求时使用）。顾客提到的水果优先。"""
    allowed, _ = allowed_boxes({**req, "required_items": []})
    opts = box_options({**req, "required_items": []}, allowed)
    tg = targets["标准"]

    def s(o):
        hit = sum(1 for i in o["items"] if mentioned_text and _mentions_species(mentioned_text, i["sku_id"]))
        return score_option(o, tg, req) - 1.2 * hit  # 顾客点名的水果优先，其次才看价格
    picked = []
    for o in sorted(opts, key=s):
        if all(o["main"] != p["main"] for p in picked):
            picked.append(o)
        if len(picked) >= n:
            break
    return picked


def _mentions_species(text: str, sku: str) -> bool:
    from .rules import mentions
    return mentions(text, sku)


def describe_option(o: dict) -> str:
    inv = load_inventory()
    return " + ".join(f"{inv[i['sku_id']].name}（{inv[i['sku_id']].spec}）" for i in o["items"]) + f"，{o['price']:.0f} 元/份"


# ---------------- 不可行预检 ----------------
def precheck(req: dict, targets: dict, options: list[dict]) -> tuple[list[str], list[str]]:
    inv = load_inventory()
    q = req["quantity"]
    reasons, suggestions = [], []
    std_high = targets["标准"]["high"]
    min_shelf = min_shelf_days(req.get("event_in_days"))
    excl = excluded_skus(req)

    for r in req.get("required_items") or []:
        it = inv[r["sku_id"]]
        need = float(r["qty"]) * q
        if it.stock < need:
            reasons.append(f"{it.name}库存只有 {it.stock:g}{it.unit}，{q} 份需要 {need:g}{it.unit}，库存不够")
            suggestions.append(f"如果每份都要{it.name}，最多能做 {int(it.stock // float(r['qty']))} 份；或只在部分礼盒里放")
        if it.shelf_days_left < min_shelf:
            reasons.append(f"{it.name}剩余保鲜只有 {it.shelf_days_left} 天，不满足送达后至少 {min_shelf} 天的要求")
        if r["sku_id"] in excl and r["sku_id"] != DURIAN_SKU:
            reasons.append(f"{it.name}不能用：{excl[r['sku_id']]}")
        unit = it.wholesale_price * float(r["qty"])
        if unit > std_high:
            reasons.append(f"{it.name}{it.wholesale_price:g} 元一{it.unit}，已超过每份预算上限 {std_high:.0f} 元")
            suggestions.append(f"每份预算提高到 {math.ceil(unit / 10) * 10} 元以上，或换成其他礼盒")
    if reasons:
        return reasons, suggestions

    if req.get("mode") == "assembly":
        allowed, _ = allowed_bulk(req)
        jin = sorted([b for b in allowed if b.unit == "斤"], key=lambda b: b.wholesale_price)
        if len(allowed) < ASSEMBLY_MIN_KINDS:
            reasons.append("可用于组装的散装水果不足 3 种")
        elif len(jin) >= 3:
            cheapest = inv["P01"].wholesale_price + jin[0].wholesale_price * 2 + jin[1].wholesale_price + jin[2].wholesale_price
            if cheapest > std_high:
                reasons.append(f"组装礼盒最少 3 种水果、4 斤加包装，单份至少 {cheapest:.2f} 元，超过预算上限 {std_high:.2f} 元")
                suggestions.append("改选整箱礼盒，或提高预算")
        return reasons, suggestions

    if not options:
        reasons.append("没有满足忌讳、保鲜期和库存要求的整箱礼盒")
        suggestions.append("减少份数或放宽日期要求")
    elif options[0]["price"] > std_high:
        o = options[0]
        reasons.append(f"预算偏低：最便宜的礼盒（{describe_option(o)}）也要 {o['price']:.0f} 元，超过预算上限 {std_high:.2f} 元")
        suggestions.append(f"把每份预算提高到 {math.ceil(o['price'])} 元以上，可选{describe_option(o)}")
    return reasons, suggestions


# ---------------- 组装礼盒：用量微调 ----------------
def _weight(items: list[dict]) -> float:
    inv = load_inventory()
    return sum(inv[i["sku_id"]].weight_jin * float(i["qty"]) for i in items)


def tune_assembly(items: list[dict], packaging_sku: str, quantity: int, target: dict, allowed_ids: set[str],
                  min_qty: dict[str, float] | None = None) -> list[dict]:
    """在已选水果内按半斤/一个微调用量，让单份价格落进目标区间，同时保持 4–8 斤。不换品。"""
    inv = load_inventory()
    items = [{"sku_id": i["sku_id"], "qty": float(i["qty"])} for i in items]
    min_qty = min_qty or {}

    def can_dec(i):  # 减完仍大于 0，且不少于顾客要求的量
        return i["qty"] - inv[i["sku_id"]].step >= max(min_qty.get(i["sku_id"], 0), inv[i["sku_id"]].step) - 1e-9

    def price(its):
        return price_plan(its, quantity, packaging_sku)["unit_price"]

    for _ in range(80):
        p, w = price(items), _weight(items)
        if target["low"] <= p <= target["high"] and ASSEMBLY_MIN_JIN <= w <= ASSEMBLY_MAX_JIN:
            break
        if p < target["low"] or w < ASSEMBLY_MIN_JIN:
            cands = [i for i in items if i["sku_id"] in allowed_ids
                     and (i["qty"] + inv[i["sku_id"]].step) * quantity <= inv[i["sku_id"]].stock
                     and w + inv[i["sku_id"]].weight_jin * inv[i["sku_id"]].step <= ASSEMBLY_MAX_JIN]
            if not cands and p < target["low"]:
                # 重量已到上限但价格不够：先减一份最便宜（按斤算）的水果腾出重量，下一轮再加贵的
                cheap = [i for i in items if can_dec(i)]
                if not cheap:
                    break
                c = min(cheap, key=lambda i: inv[i["sku_id"]].wholesale_price / inv[i["sku_id"]].weight_jin)
                pricey = max(items, key=lambda i: inv[i["sku_id"]].wholesale_price / inv[i["sku_id"]].weight_jin)
                if c is pricey or (pricey["qty"] + inv[pricey["sku_id"]].step) * quantity > inv[pricey["sku_id"]].stock:
                    break
                c["qty"] -= inv[c["sku_id"]].step
                continue
            if not cands:
                break
            if p < target["low"]:  # 价格不够：加单价高的，少占重量
                c = max(cands, key=lambda i: inv[i["sku_id"]].wholesale_price / inv[i["sku_id"]].weight_jin)
            else:  # 重量不够：加便宜的
                c = min(cands, key=lambda i: inv[i["sku_id"]].wholesale_price)
            c["qty"] += inv[c["sku_id"]].step
        else:  # 价格过高或超重：减
            cands = [i for i in items if can_dec(i)
                     and w - inv[i["sku_id"]].weight_jin * inv[i["sku_id"]].step >= ASSEMBLY_MIN_JIN]
            if not cands:
                break
            if p > target["high"]:
                c = max(cands, key=lambda i: inv[i["sku_id"]].wholesale_price)
            else:
                c = min(cands, key=lambda i: inv[i["sku_id"]].wholesale_price / inv[i["sku_id"]].weight_jin)
            c["qty"] -= inv[c["sku_id"]].step
    return items


# ---------------- 方案校验 ----------------
DIGIT_RE = re.compile(r"[0-9０-９]|元|块钱|￥|¥")


def check_tiers(req: dict, tiers: list[dict], targets: dict, options: list[dict] | None = None,
                check_copy: bool = False) -> tuple[list[str], dict]:
    """程序规则校验。返回 (硬性问题列表, 每档 {issues, warnings})。"""
    inv = load_inventory()
    q = req["quantity"]
    excl = excluded_skus(req)
    min_shelf = min_shelf_days(req.get("event_in_days"))
    assembly = req.get("mode") == "assembly"
    problems, per_tier = [], {}
    for t in tiers:
        name, items = t["tier"], t["items"]
        issues, warnings = [], []
        skus = [i["sku_id"] for i in items]
        if len(set(skus)) != len(skus):
            issues.append("同一商品重复出现")
        for i in items:
            it = inv.get(i["sku_id"])
            if it is None:
                issues.append(f"商品 {i['sku_id']} 不在库存表中")
                continue
            if i["sku_id"] == DURIAN_SKU:
                issues.append("榴莲按个零售，不能放进礼盒")
            elif i["sku_id"] in excl:
                issues.append(f"{it.name}不能用：{excl[i['sku_id']]}")
            if assembly and not it.is_bulk:
                issues.append(f"{it.name}不是可组装的散装水果")
            if not assembly and not it.is_box:
                issues.append(f"{it.name}不是整箱礼盒")
            if float(i["qty"]) <= 0:
                issues.append(f"{it.name}用量必须大于 0")
            if float(i["qty"]) * q > it.stock:
                issues.append(f"{it.name}库存不足：需要 {float(i['qty']) * q:g}{it.unit}，库存 {it.stock:g}{it.unit}")
            if it.shelf_days_left < min_shelf:
                issues.append(f"{it.name}剩余保鲜 {it.shelf_days_left} 天，不足 {min_shelf} 天")
        if any(s not in inv for s in skus):
            per_tier[name] = {"issues": issues, "warnings": warnings}
            problems += [f"{name}款：{x}" for x in issues]
            continue
        if assembly:
            kinds = len(set(skus))
            if not ASSEMBLY_MIN_KINDS <= kinds <= ASSEMBLY_MAX_KINDS:
                issues.append(f"组装礼盒要 {ASSEMBLY_MIN_KINDS}–{ASSEMBLY_MAX_KINDS} 种水果，现在 {kinds} 种")
            w = _weight(items)
            if not ASSEMBLY_MIN_JIN - 1e-6 <= w <= ASSEMBLY_MAX_JIN + 1e-6:
                issues.append(f"单盒重量要 {ASSEMBLY_MIN_JIN:g}–{ASSEMBLY_MAX_JIN:g} 斤，现在约 {w:.1f} 斤")
            species = [inv[s].species for s in skus]
            if len(set(species)) != len(species):
                issues.append("同一种水果不能放两个规格")
        else:
            if len(items) > 2:
                issues.append("每份最多 2 款整箱礼盒")
            if any(float(i["qty"]) != 1 for i in items):
                issues.append("整箱礼盒每份每款 1 箱/盒")
        for r in req.get("required_items") or []:
            got = sum(float(i["qty"]) for i in items if i["sku_id"] == r["sku_id"])
            if got < float(r["qty"]) - 1e-6:
                issues.append(f"缺少顾客要求的{inv[r['sku_id']].name}")
        plan, tg = t["plan"], targets[name]
        if not (tg["low"] - 1e-6 <= plan["unit_price"] <= tg["high"] + 1e-6):
            gap = plan["unit_price"] - tg["target"]
            msg = f"单份价格 {plan['unit_price']:.2f} 元，与目标价 {tg['target']:.0f} 元相差 {gap:+.0f} 元"
            # 标准款：只有库存里确实有落进预算的方案时才算硬性问题（店主确认：预算优先，凑不上就选最接近的并提示）
            reachable = assembly or (options is not None and window_reachable(options, tg))
            (issues if name == "标准" and reachable else warnings).append(msg)
        if plan["margin_warning"]:
            warnings.append(f"毛利率 {plan['gross_margin'] * 100:.0f}% 低于 20% 底线")
        if check_copy:
            for field in ("intro", "blessing"):
                if DIGIT_RE.search(t.get(field, "") or ""):
                    issues.append(f"文案中出现了数字或价格：{t.get(field)}")
        per_tier[name] = {"issues": issues, "warnings": warnings}
        problems += [f"{name}款：{x}" for x in issues]
    return problems, per_tier


def grade_warning(req: dict, tiers: list[dict]) -> str | None:
    """预算优先：场景要求的档次在预算内够不上时提示（店主确认）。"""
    inv = load_inventory()
    rule = scene_rule(req["occasion"])
    want = (rule.get("grades") or [None])[0]
    if req.get("wants_premium"):
        want = "高档"
    if not want or want == "普通" or req.get("mode") == "assembly":
        return None
    std = next((t for t in tiers if t["tier"] == "标准"), None)
    if std is None:
        return None
    main = max(std["items"], key=lambda i: inv[i["sku_id"]].wholesale_price)
    order = {"普通": 0, "精品": 1, "高档": 2}
    if order.get(inv[main["sku_id"]].grade, 0) >= order[want]:
        return None
    cheapest = min((b for b in boxes() if order.get(b.grade, 0) >= order[want]), key=lambda b: b.wholesale_price)
    return f"该场景建议{want}礼盒，预算内的标准款档次偏低；预算提高到约 {cheapest.wholesale_price:.0f} 元可选{cheapest.name}"


def near_expiry_ratio(tiers: list[dict]) -> float:
    """方案金额中，剩余保鲜 ≤ 5 天的商品所占比例。"""
    inv = load_inventory()
    total = near = 0.0
    for t in tiers:
        for i in t["items"]:
            amt = inv[i["sku_id"]].wholesale_price * float(i["qty"])
            total += amt
            near += amt if inv[i["sku_id"]].shelf_days_left <= NEAR_EXPIRY_DAYS else 0
    return near / total if total else 0.0


def assembly_packaging(tier: str) -> str:
    return ASSEMBLY_PACKAGING[tier]
