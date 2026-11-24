"""LLM Agent：需求理解、选礼盒（整箱 / 组装）、文案、审核。数字计算和规则筛选一律由代码完成，模型只在候选里做选择和写字。"""
from __future__ import annotations

import re

from .inventory import Item, load_inventory
from .llm import chat_json
from .rules import scene_of, scene_rule

OCCASIONS = "公司福利/公司活动/商务送礼/拜访领导/探病/婚庆/满月/乔迁/祝寿/开业/节日慰问/春节走亲/孝敬长辈/送礼/未说明"


def _catalog_brief() -> str:
    return "；".join(f"{it.sku_id} {it.name}" for it in load_inventory().values() if not it.is_packaging)


# ---------------- ① 需求理解 Agent ----------------
PARSE_SYSTEM = """你是水果批发档口的需求理解助手。把顾客的一句话需求解析成 JSON，不要编造顾客没说的信息。
商品清单（G 开头是整箱礼盒，F 开头是散装水果）：{catalog}
只输出 JSON：
{"occasion": "用途，从这些里选最接近的：%s",
 "quantity": 份数（整数；"一个果篮"=1；没说则 null）,
 "budget_per_box": 每份预算（数字，元；没说则 null）,
 "budget_type": "around"（左右/约/预算一份X）或 "max"（以内/不超过/最多）或 null,
 "event_in_days": 距离使用还有几天（"两天后"=2，"下周"=7，"明天"=1；没说或只说节日则 null）,
 "exclude_skus": ["顾客点名不要的商品 sku_id（"梨"只对应梨，不包括凤梨）"],
 "required_items": [{"sku_id": "顾客明确要求每份都必须有的商品；整箱礼盒请求用 G 开头的，组装请求用 F 开头的", "qty": 每份数量（没说则 1）}],
 "wants_premium": 是否明确要高档/上档次/有面子（true/false）,
 "preferences": ["其他偏好，如：喜庆、寓意好、少放特别甜的、放得住"],
 "missing_info": ["缺失的关键信息，如：每份预算、用途、使用日期"]}""" % OCCASIONS


def parse_request(text: str) -> dict:
    return chat_json("需求理解", PARSE_SYSTEM.replace("{catalog}", _catalog_brief()), f"顾客需求：{text}", max_tokens=800)


def _scene_line(req: dict) -> str:
    rule = scene_rule(req["occasion"])
    pref = req.get("packaging_pref") or rule.get("packaging") or "无"
    return (f"场合：{req['occasion']}（按「{scene_of(req['occasion'])}」规则：档次倾向 {'/'.join(rule.get('grades') or ['按预算'])}，"
            f"价值 {rule['value']}，{rule['note']}）；份数 {req['quantity']}；包装偏好：{pref}；"
            f"顾客偏好：{'、'.join(req.get('preferences') or []) or '无'}")


# ---------------- ② 选礼盒 Agent（整箱） ----------------
SELECT_BOX_SYSTEM = """你是水果档口的礼盒推荐员。程序已按库存、忌讳、保鲜期、预算和场景规则筛好每档的候选（已排序，靠前的更匹配）。
请为经济 / 标准 / 高档三档各选一个候选编号：三档不能重复，价格要经济 < 标准 < 高档；标准款优先选落在预算区间内的；
结合场合寓意、档次倾向和包装偏好做判断。只输出 JSON：
{"picks": {"经济": "O3", "标准": "O7", "高档": "O12"}, "reason": "一句话理由"}"""


def _opt_line(o: dict, tg: dict) -> str:
    inv = load_inventory()
    parts = []
    for i in o["items"]:
        it = inv[i["sku_id"]]
        note = f"，寓意{meaning(it)}" if meaning(it) else ""
        parts.append(f"{it.name}（{it.spec}，{it.grade}，{it.packaging_style}{note}，保鲜{it.shelf_days_left}天）")
    flag = "√区间内" if tg["low"] - 1e-6 <= o["price"] <= tg["high"] + 1e-6 else "×区间外"
    return f"{o['id']}：{' + '.join(parts)}，{o['price']:.0f} 元/份 {flag}"


def select_boxes(req: dict, short: dict, targets: dict, feedback: list[str]) -> dict:
    lines = [_scene_line(req)]
    for t, opts in short.items():
        tg = targets[t]
        lines.append(f"【{t}】目标 {tg['target']:.0f} 元（区间 {tg['low']:.0f}–{tg['high']:.0f}）")
        lines += [_opt_line(o, tg) for o in opts]
    if feedback:
        lines.append("【上一版未通过审核，请换候选】" + "；".join(feedback))
    return chat_json("选礼盒", SELECT_BOX_SYSTEM, "\n".join(lines), max_tokens=1500, reasoning=True)


# ---------------- ② 选礼盒 Agent（组装，仅顾客明确要求时） ----------------
SELECT_ASSEMBLY_SYSTEM = """你是水果礼盒搭配师。顾客明确要求组装礼盒，只能从【可用散装水果】里选，生成经济 / 标准 / 高档三档。
规则：每档 3–5 种水果；单盒水果合计 4–8 斤（按个卖的约 0.3 斤/个）；同一种水果不能放两个规格；
斤为单位的用量取 0.5 的倍数，个取整数；顾客点名要的水果每档都要有；单份价格 = 水果批发价×用量之和 + 包装价，尽量接近目标价（程序会复核微调）。
重量上限只有 8 斤，目标价高时要多放车厘子、山竹、葡萄等高单价水果，少放橙子、苹果等低单价水果。
只输出 JSON：{"tiers": [{"tier": "经济", "items": [{"sku_id": "F02", "qty": 2}]}, {"tier": "标准", ...}, {"tier": "高档", ...}]}"""


def _bulk_line(it: Item, quantity: int) -> str:
    max_per_box = int(it.stock / max(quantity, 1) / it.step) * it.step
    note = f"，寓意{meaning(it)}" if meaning(it) else ""
    return f"{it.sku_id} {it.name}：{it.wholesale_price:g} 元/{it.unit}，每份最多 {max_per_box:g}{it.unit}，保鲜 {it.shelf_days_left} 天{note}"


def select_assembly(req: dict, allowed: list[Item], targets: dict, packaging: dict, feedback: list[str]) -> dict:
    inv = load_inventory()
    required = "、".join(inv[r["sku_id"]].name for r in req.get("required_items") or []) or "无"
    lines = [_scene_line(req), f"顾客点名要的水果：{required}"]
    for t, tg in targets.items():
        pkg = inv[packaging[t]]
        lines.append(f"{t}：目标 {tg['target']:.0f} 元（区间 {tg['low']:.0f}–{tg['high']:.0f}），包装 {pkg.name} {pkg.wholesale_price:g} 元已计入")
    lines.append("【可用散装水果】")
    lines += [_bulk_line(it, req["quantity"]) for it in allowed]
    if feedback:
        lines.append("【上一版未通过审核，请修改】" + "；".join(feedback))
    return chat_json("选礼盒", SELECT_ASSEMBLY_SYSTEM, "\n".join(lines), max_tokens=1500)


# ---------------- ④ 文案 Agent ----------------
COPY_SYSTEM = """你是水果档口的文案助手。为每档礼盒写一句方案介绍（突出礼盒与寓意，30 字以内）和一句祝福语（贴合场合，20 字以内）。
严禁出现任何价格、金额、数字（包括阿拉伯数字）和"元""块"等字样。
文案要贴合给出的场合；场合为"未说明"时写通用的吉祥祝福，不要假设婚庆、探病等具体场合。
只输出 JSON：{"tiers": [{"tier": "经济", "intro": "...", "blessing": "..."}, ...]}"""


def meaning(it: Item) -> str:
    """只给模型看寓意本身；去掉库存表里括号中的「适合某场合」备注，避免文案/审核把它当成场合。"""
    return re.sub(r"（[^）]*）", "", it.meaning_note).strip()


def _tier_desc(t: dict) -> str:
    inv = load_inventory()
    names = "、".join(f"{ln['name']}" + (f"（寓意：{meaning(inv[ln['sku_id']])}）" if meaning(inv[ln["sku_id"]]) else "")
                     for ln in t["plan"]["lines"])
    return f"{t['tier']}款礼盒：{names}"


def write_copy(req: dict, tiers: list[dict]) -> dict:
    user = (f"场合：{req['occasion']}；顾客偏好：{'、'.join(req.get('preferences') or []) or '无'}\n"
            f"请按这几档输出（tier 字段照写）：{'、'.join(t['tier'] for t in tiers)}\n" + "\n".join(_tier_desc(t) for t in tiers))
    return chat_json("文案", COPY_SYSTEM, user, max_tokens=800)


# ---------------- ⑤ 审核 Agent（寓意） ----------------
REVIEW_SYSTEM = """你是水果礼盒审核员。价格、档次、预算、库存、忌讳、保鲜期都已由程序按店主规则决定并校验通过（店主规定预算优先：经济款比预算便宜、档次可以低一些，这是正常的），这些你都不要评判。
你只检查：
1. 礼盒寓意是否明显不适合该场合（例如探病、婚庆、乔迁送梨；寓意不吉利的谐音）；
2. 是否明显违背顾客明确提出的偏好（例如"少放特别甜的"却以高糖水果为主）；
3. 介绍和祝福语是否得体、与礼盒一致；场合未说明时，文案不应写成某个具体场合（如婚庆）。
只在问题明确时判定不通过；小建议放进 suggestions，不影响通过。
只输出 JSON：{"tiers": [{"tier": "经济", "pass": true, "issues": [], "suggestions": []}, ...]}"""


def review_meaning(req: dict, tiers: list[dict]) -> dict:
    desc = [f"{_tier_desc(t)}\n  文案-介绍：{t.get('intro', '')}\n  文案-祝福语：{t.get('blessing', '')}" for t in tiers]
    user = f"顾客原话：{req.get('raw_text', '')}\n{_scene_line(req)}\n\n" + "\n".join(desc)
    return chat_json("审核", REVIEW_SYSTEM, user, max_tokens=1200)
