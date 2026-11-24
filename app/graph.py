"""LangGraph 编排：需求理解 → 预检 → 选品 → 报价核算 → 规则校验 → 文案 → 审核 →（返工 / 输出 / 不可行）。"""
from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from . import agents
from .checks import (allowed_fruits, check_tiers, near_expiry_ratio, precheck, priority_ids,
                     tune_quantities)
from .inventory import load_inventory
from .llm import LLMError, start_usage
from .pricing import price_plan, quote_text
from .rules import (HEALTH_DISCLAIMER, reconcile_exclusions, HEALTH_KEYWORDS, TIER_PACKAGING, TIERS, min_shelf_days,
                    normalize_occasion, tier_targets)

MAX_REWORK = 2  # 不通过最多打回重做 2 次（共 3 次搭配）


class State(TypedDict, total=False):
    text: str
    req: dict
    targets: dict
    allowed_ids: list[str]
    prio_ids: list[str]
    attempts: int
    feedback: list[str]
    tiers: list[dict]
    rule_problems: list[str]
    per_tier: dict
    review: dict
    status: str
    infeasible_reasons: list[str]
    suggestions: list[str]
    trace: list[dict]


def _trace(state: State, node: str, info: Any) -> list[dict]:
    return (state.get("trace") or []) + [{"node": node, "info": info}]


# ---------- 节点 ----------
def node_parse(state: State) -> State:
    raw = agents.parse_request(state["text"])
    inv = load_inventory()
    missing = list(raw.get("missing_info") or [])
    qty = raw.get("quantity")
    try:
        qty = int(qty) if qty else None
    except (TypeError, ValueError):
        qty = None
    if not qty:
        qty = 1
        if not any("份" in m for m in missing):
            missing.append("份数（暂按 1 份）")
    budget = raw.get("budget_per_box")
    try:
        budget = float(budget) if budget not in (None, "", 0) else None
    except (TypeError, ValueError):
        budget = None
    if budget is None and not any("预算" in m for m in missing):
        missing.append("每份预算")
    missing = [m for m in missing if not ("预算" in m and budget is not None)]
    required = []
    for r in raw.get("required_items") or []:
        if isinstance(r, dict) and r.get("sku_id") in inv:
            try:
                required.append({"sku_id": r["sku_id"], "qty": float(r.get("qty") or 1)})
            except (TypeError, ValueError):
                pass
    req = {
        "raw_text": state["text"],
        "occasion": normalize_occasion(raw.get("occasion")),
        "quantity": qty,
        "budget_per_box": budget,
        "budget_type": raw.get("budget_type") if budget is not None else None,
        "event_in_days": raw.get("event_in_days") if isinstance(raw.get("event_in_days"), int) else None,
        "exclude_skus": [s for s in (raw.get("exclude_skus") or []) if s in inv],
        "required_items": required,
        "wants_premium": bool(raw.get("wants_premium")),
        "preferences": [p for p in (raw.get("preferences") or []) if isinstance(p, str)],
        "missing_info": missing,
    }
    # 顾客点名不要的商品：用名称核对模型输出，补漏（"别放梨"）并去掉误判（梨≠凤梨）
    req["exclude_skus"] = reconcile_exclusions(state["text"], req["exclude_skus"])
    req["min_shelf_days"] = min_shelf_days(req["event_in_days"])
    targets = tier_targets(budget, req["budget_type"])
    return {"req": req, "targets": targets, "attempts": 0, "feedback": [],
            "trace": _trace(state, "需求理解", req)}


def node_precheck(state: State) -> State:
    reasons, suggestions = precheck(state["req"], state["targets"])
    allowed, _ = allowed_fruits(state["req"])
    info = {"可用水果": [i.sku_id for i in allowed], "不可行原因": reasons}
    out: State = {"allowed_ids": [i.sku_id for i in allowed],
                  "prio_ids": sorted(priority_ids(allowed)),
                  "trace": _trace(state, "可行性预检", info)}
    if reasons:
        out.update(status="infeasible", infeasible_reasons=reasons, suggestions=suggestions)
    return out


def node_select(state: State) -> State:
    inv = load_inventory()
    allowed = [inv[s] for s in state["allowed_ids"]]
    res = agents.select_products(state["req"], allowed, set(state["prio_ids"]), state["targets"],
                                 TIER_PACKAGING, state.get("feedback") or [])
    by_tier = {t.get("tier"): t for t in (res.get("tiers") or []) if isinstance(t, dict)}
    tiers = []
    for name in TIERS:
        t = by_tier.get(name) or {"items": []}
        items = []
        for i in t.get("items") or []:
            try:
                items.append({"sku_id": str(i["sku_id"]).strip(), "qty": float(i["qty"])})
            except (KeyError, TypeError, ValueError):
                continue
        tiers.append({"tier": name, "items": items, "packaging": TIER_PACKAGING[name], "idea": t.get("idea", "")})
    return {"tiers": tiers, "attempts": state.get("attempts", 0) + 1,
            "trace": _trace(state, "选品搭配", {"第几次": state.get("attempts", 0) + 1,
                                                "方案": [{"tier": t["tier"], "items": t["items"]} for t in tiers]})}


def node_price(state: State) -> State:
    """③ 报价核算（程序）：先在已选商品内微调用量贴近目标价，再计算价格、成本、毛利。"""
    inv = load_inventory()
    q = state["req"]["quantity"]
    allowed_ids, prio = set(state["allowed_ids"]), set(state["prio_ids"])
    tiers = []
    for t in state["tiers"]:
        items = t["items"]
        if items and all(i["sku_id"] in inv and not inv[i["sku_id"]].is_packaging for i in items):
            items = tune_quantities(items, t["packaging"], q, state["targets"][t["tier"]], allowed_ids, prio)
            plan = price_plan(items, t["packaging"], q)
        else:
            plan = price_plan([i for i in items if i["sku_id"] in inv and not inv[i["sku_id"]].is_packaging],
                              t["packaging"], q)
        tiers.append({**t, "items": items, "plan": plan})
    problems, per_tier = check_tiers(state["req"], tiers, state["targets"])
    return {"tiers": tiers, "rule_problems": problems, "per_tier": per_tier,
            "trace": _trace(state, "报价核算+规则校验", {
                "单份价格": {t["tier"]: t["plan"]["unit_price"] for t in tiers}, "问题": problems})}


def node_copy(state: State) -> State:
    res = agents.write_copy(state["req"], state["tiers"])
    by_tier = {t.get("tier"): t for t in (res.get("tiers") or []) if isinstance(t, dict)}
    tiers = [{**t, "intro": (by_tier.get(t["tier"]) or {}).get("intro", ""),
              "blessing": (by_tier.get(t["tier"]) or {}).get("blessing", "")} for t in state["tiers"]]
    return {"tiers": tiers, "trace": _trace(state, "文案", {t["tier"]: [t["intro"], t["blessing"]] for t in tiers})}


def node_review(state: State) -> State:
    problems, per_tier = check_tiers(state["req"], state["tiers"], state["targets"], check_copy=True)
    res = agents.review_meaning(state["req"], state["tiers"])
    by_tier = {t.get("tier"): t for t in (res.get("tiers") or []) if isinstance(t, dict)}
    for name in TIERS:
        r = by_tier.get(name) or {"pass": True}
        per_tier[name]["llm_pass"] = bool(r.get("pass", True))
        per_tier[name]["llm_issues"] = [x for x in (r.get("issues") or []) if isinstance(x, str)]
        per_tier[name]["suggestions"] = [x for x in (r.get("suggestions") or []) if isinstance(x, str)]
        if not per_tier[name]["llm_pass"]:
            problems += [f"{name}款（寓意审核）：{x}" for x in per_tier[name]["llm_issues"] or ["寓意不合适"]]
    return {"rule_problems": problems, "per_tier": per_tier,
            "trace": _trace(state, "审核", {"问题": problems})}


def node_rework(state: State) -> State:
    return {"feedback": state["rule_problems"]}


def node_fail(state: State) -> State:
    reasons = [f"重做 {MAX_REWORK} 次后仍有问题："] + state.get("rule_problems", [])
    sug = []
    text = " ".join(state.get("rule_problems", []))
    if "库存不足" in text:
        sug.append("减少份数，或允许不同礼盒使用不同水果")
    if "单份价格" in text:
        sug.append("调整每份预算，或放宽对水果品种的要求")
    if "保鲜" in text:
        sug.append("放宽送达日期要求")
    if not sug:
        sug.append("请店员根据以上原因人工调整搭配")
    return {"status": "infeasible", "infeasible_reasons": reasons, "suggestions": sug,
            "trace": _trace(state, "不可行", reasons)}


def node_finalize(state: State) -> State:
    return {"status": "ok", "trace": _trace(state, "输出", "审核通过")}


# ---------- 路由 ----------
def route_after_precheck(state: State) -> str:
    return "infeasible" if state.get("status") == "infeasible" else "select"


def route_after_rules(state: State) -> str:
    if not state["rule_problems"]:
        return "copy"
    return "rework" if state["attempts"] <= MAX_REWORK else "fail"


def route_after_review(state: State) -> str:
    if not state["rule_problems"]:
        return "ok"
    return "rework" if state["attempts"] <= MAX_REWORK else "fail"


def build_graph():
    g = StateGraph(State)
    g.add_node("parse", node_parse)
    g.add_node("precheck", node_precheck)
    g.add_node("select", node_select)
    g.add_node("price", node_price)
    g.add_node("copy", node_copy)
    g.add_node("review", node_review)
    g.add_node("rework", node_rework)
    g.add_node("fail", node_fail)
    g.add_node("finalize", node_finalize)
    g.set_entry_point("parse")
    g.add_edge("parse", "precheck")
    g.add_conditional_edges("precheck", route_after_precheck, {"infeasible": END, "select": "select"})
    g.add_edge("select", "price")
    g.add_conditional_edges("price", route_after_rules, {"copy": "copy", "rework": "rework", "fail": "fail"})
    g.add_edge("copy", "review")
    g.add_conditional_edges("review", route_after_review, {"ok": "finalize", "rework": "rework", "fail": "fail"})
    g.add_edge("rework", "select")
    g.add_edge("fail", END)
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


def run(text: str) -> dict:
    """处理一条顾客需求，返回给网页 / 评测使用的结果。"""
    t0 = time.time()
    usage = start_usage()
    try:
        state: State = GRAPH.invoke({"text": text}, {"recursion_limit": 40})
    except LLMError as e:
        return {"status": "error", "request": text, "error": str(e), "usage": usage, "elapsed_s": round(time.time() - t0, 2)}
    req = state.get("req", {})
    notes = []
    if req.get("missing_info"):
        notes.append("缺少信息：" + "、".join(req["missing_info"]) + "（请向顾客确认）")
    if req.get("budget_per_box") is None:
        notes.append("顾客未给预算，按常见档位 100 / 200 / 300 元给方案，优先搭配库存久、需尽快卖掉的水果")
    if any(k in text for k in HEALTH_KEYWORDS):
        notes.append(HEALTH_DISCLAIMER)
    recommended = "高档" if req.get("wants_premium") else "标准"
    out = {
        "status": state.get("status"),
        "request": text,
        "requirement": req,
        "targets": state.get("targets"),
        "attempts": state.get("attempts", 0),
        "notes": notes,
        "trace": state.get("trace", []),
        "usage": usage,
        "elapsed_s": round(time.time() - t0, 2),
    }
    if state.get("status") == "ok":
        tiers = []
        for t in state["tiers"]:
            pt = state["per_tier"].get(t["tier"], {})
            tiers.append({
                "tier": t["tier"],
                "recommended": t["tier"] == recommended,
                "items": t["items"],
                **t["plan"],
                "intro": t.get("intro", ""),
                "blessing": t.get("blessing", ""),
                "review": {"passed": True, "warnings": pt.get("warnings", []),
                           "suggestions": pt.get("suggestions", [])},
                "quote_text": quote_text(t["tier"], t["plan"], req["quantity"], t.get("intro", ""), t.get("blessing", "")),
            })
        out["tiers"] = tiers
        out["near_expiry_ratio"] = round(near_expiry_ratio(state["tiers"]), 3)
    else:
        out["infeasible_reasons"] = state.get("infeasible_reasons", [])
        out["suggestions"] = state.get("suggestions", [])
    return out
