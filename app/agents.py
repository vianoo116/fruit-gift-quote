"""四个 LLM Agent：需求理解、选品搭配、文案、审核（寓意）。数字计算一律不交给模型。"""
from __future__ import annotations

import json

from .inventory import Item, load_inventory
from .llm import chat_json

OCCASIONS = "公司福利/公司活动/商务送礼/探病/婚庆/乔迁/祝寿/满月/开业/节日慰问/春节走亲/孝敬长辈/朋友送礼/未说明"


def _catalog_brief() -> str:
    rows = []
    for it in load_inventory().values():
        if it.is_packaging:
            continue
        rows.append(f"{it.sku_id} {it.name}（{it.unit}）")
    return "；".join(rows)


# ---------------- ① 需求理解 Agent ----------------
PARSE_SYSTEM = f"""你是水果批发档口的需求理解助手。把顾客的一句话需求解析成 JSON，不要编造顾客没说的信息。
商品清单：{{catalog}}

只输出如下 JSON：
{{{{
  "occasion": "用途/场合，从这些里选最接近的：{OCCASIONS}",
  "quantity": 份数（整数；顾客说"一个果篮"就是 1；没说则 null）,
  "budget_per_box": 每份预算（数字，单位元；没说则 null）,
  "budget_type": "around"（左右/大概/约/预算一份X） 或 "max"（以内/不超过/最多/封顶） 或 null,
  "event_in_days": 距离使用/送出还有几天（整数，"两天后"=2，"下周"=7，"明天"=1，"过年/中秋"等节日但未给具体日期则 null；没说则 null）,
  "exclude_skus": ["顾客明确点名不要/不爱吃的商品 sku_id（只按名称精确对应，例如\"梨\"只对应皇冠梨，不包括凤梨）"],
  "required_items": [{{{{"sku_id": "顾客明确要求每份必须放的商品", "qty": 每份数量（按商品单位，没说数量则 1）}}}}],
  "wants_premium": 顾客是否明确要高档/上档次/有面子（true/false）,
  "preferences": ["其他偏好，如：喜庆、寓意好、少放特别甜的、放得住、吉利"],
  "missing_info": ["缺失的关键信息，如：每份预算、份数、使用日期"]
}}}}"""


def parse_request(text: str) -> dict:
    system = PARSE_SYSTEM.replace("{catalog}", _catalog_brief())
    return chat_json("需求理解", system, f"顾客需求：{text}", max_tokens=1200)


# ---------------- ② 选品搭配 Agent ----------------
SELECT_SYSTEM = """你是水果礼盒搭配师。只能从【可用商品】里选，生成经济 / 标准 / 高档三档礼盒方案。
规则：
1. 只能使用【可用商品】中的 sku_id；每份用量不得超过该商品的"每份最多"。
2. 每档 3–5 种水果为宜（至少 2 种，最多 6 种），水果合计至少 3 斤（按个/盒卖的商品按约重计）。斤为单位的用量取 0.5 的倍数，个/盒取整数。
3. 优先使用标记为【优先】的商品（库存多、剩余保鲜天数短，需要尽快卖掉）。
4. 每档的单份价格（= 各水果批发单价×用量之和 + 包装价）要尽量接近该档目标价，并落在允许区间内。价格由程序复核，你只需大致估算。
5. 顾客要求必放的商品必须放进每一档，数量不少于要求。
6. 搭配要符合场合寓意与顾客偏好；高档档位用更贵、更有面子的水果。
只输出 JSON：
{"tiers": [{"tier": "经济", "items": [{"sku_id": "F02", "qty": 2}], "idea": "一句话搭配思路"}, {"tier": "标准", ...}, {"tier": "高档", ...}]}"""


def _fruit_line(it: Item, quantity: int, priority: bool) -> str:
    max_per_box = int(it.stock / max(quantity, 1) / it.step) * it.step
    tag = "【优先】" if priority else ""
    note = f"，寓意：{it.meaning_note}" if it.meaning_note else ""
    return (f"{it.sku_id} {it.name}{tag}：批发价 {it.wholesale_price} 元/{it.unit}，"
            f"每份最多 {max_per_box:g}{it.unit}，剩余保鲜 {it.shelf_days_left} 天{note}")


def select_products(req: dict, allowed: list[Item], priority_ids: set[str], targets: dict,
                    packaging: dict, feedback: list[str]) -> dict:
    inv = load_inventory()
    q = req["quantity"]
    fruit_lines = "\n".join(_fruit_line(it, q, it.sku_id in priority_ids) for it in allowed)
    tier_lines = "\n".join(
        f"{t}：目标单份价 {v['target']} 元（允许 {v['low']}–{v['high']} 元），包装 {packaging[t]} "
        f"{inv[packaging[t]].name} {inv[packaging[t]].wholesale_price} 元（已计入单份价）"
        for t, v in targets.items())
    required = "、".join(f"{inv[r['sku_id']].name}({r['sku_id']}) 每份 {r['qty']}{inv[r['sku_id']].unit}"
                         for r in req.get("required_items", [])) or "无"
    user = f"""【顾客需求】场合：{req['occasion']}；份数：{q}；偏好：{'、'.join(req.get('preferences') or []) or '无'}；必放：{required}
【三档目标】
{tier_lines}
【可用商品】（已排除忌讳、顾客不要、不适合送礼、保鲜期不够、库存不足的商品）
{fruit_lines}"""
    if feedback:
        user += "\n【上一版未通过审核，请针对以下问题重新搭配】\n" + "\n".join(f"- {f}" for f in feedback)
    return chat_json("选品搭配", SELECT_SYSTEM, user, max_tokens=5000)


# ---------------- ④ 文案 Agent ----------------
COPY_SYSTEM = """你是水果档口的文案助手。为每档礼盒写一句方案介绍（突出水果搭配与寓意，30 字以内）和一句祝福语（贴合场合，20 字以内）。
严禁出现任何价格、金额、数字（包括阿拉伯数字）和"元""块"等字样。
只输出 JSON：{"tiers": [{"tier": "经济", "intro": "...", "blessing": "..."}, ...]}"""


def write_copy(req: dict, tiers: list[dict]) -> dict:
    desc = []
    for t in tiers:
        fruits = "、".join(ln["name"] for ln in t["plan"]["lines"])
        desc.append(f"{t['tier']}款：{fruits}；包装：{t['plan']['packaging']['name']}")
    user = (f"场合：{req['occasion']}；顾客偏好：{'、'.join(req.get('preferences') or []) or '无'}\n"
            + "\n".join(desc))
    return chat_json("文案", COPY_SYSTEM, user, max_tokens=1200)


# ---------------- ⑤ 审核 Agent（寓意） ----------------
REVIEW_SYSTEM = """你是水果礼盒审核员。价格、库存、忌讳规则已由程序校验通过，你只检查：
1. 搭配的寓意是否适合该场合（例如探病不宜梨、婚庆不宜梨；送礼避免寓意不吉利的谐音）；
2. 是否违背顾客明确提出的偏好（例如"少放特别甜的"却放了大量高糖水果）；
3. 介绍和祝福语是否得体、是否与搭配一致。
只在问题明确时判定不通过；小的改进建议放进 suggestions，不影响通过。
只输出 JSON：{"tiers": [{"tier": "经济", "pass": true, "issues": [], "suggestions": []}, ...]}"""


def review_meaning(req: dict, tiers: list[dict]) -> dict:
    inv = load_inventory()
    desc = []
    for t in tiers:
        fruits = "、".join(
            f"{ln['name']}{ln['qty']:g}{ln['unit']}" + (f"（{inv[ln['sku_id']].meaning_note}）" if inv[ln['sku_id']].meaning_note else "")
            for ln in t["plan"]["lines"])
        desc.append(f"{t['tier']}款：{fruits}\n  介绍：{t.get('intro', '')}\n  祝福语：{t.get('blessing', '')}")
    user = (f"顾客原话：{req.get('raw_text', '')}\n场合：{req['occasion']}；偏好：{'、'.join(req.get('preferences') or []) or '无'}\n\n"
            + "\n".join(desc))
    return chat_json("审核", REVIEW_SYSTEM, user, max_tokens=1500)


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
