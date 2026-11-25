"""LangGraph 编排：需求理解 → 预检（代码）→ 选礼盒 → 报价核算 + 规则校验（代码）→ 文案 → 审核 →（返工 1 次 / 输出 / 不可行）。"""
from __future__ import annotations

import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from . import agents
from .checks import (KIND_LABEL, allowed_boxes, allowed_bulk, alternatives_for, arrange_tiers, choose_recommended,
                     assembly_packaging, box_options, check_tiers, describe_option, grade_warning,
                     near_expiry_ratio, precheck, shortlist, tune_assembly)
from .inventory import load_inventory
from .llm import LLMError, start_usage
from .pricing import price_plan, quote_text
from .rules import (ASSEMBLY_PACKAGING, ASSEMBLY_REASON, DURIAN_MSG, HEALTH_DISCLAIMER, HEALTH_KEYWORDS,
                    SKU_ALIASES, TIERS, min_shelf_days, packaging_pref, reconcile_exclusions, scene_of,
                    tier_targets, wants_assembly, wants_durian)

MAX_REWORK = 1  # 审核不通过最多返工 1 次（v1.1 为了 30 秒目标从 2 次降为 1 次）

# 网页进度条的四个步骤
STEP_OF_NODE = {"parse": "理解需求", "precheck": "理解需求", "select": "选礼盒", "price": "核算",
                "copy": "审核", "review": "审核", "rework": "选礼盒", "fail": "审核", "finalize": "审核"}


class State(TypedDict, total=False):
    text: str
    req: dict
    targets: dict
    options: list[dict]
    short: dict
    allowed_bulk: list[str]
    alternatives: list[dict]
    attempts: int
    feedback: list[str]
    picks: dict
    ai_raw: dict
    tiers: list[dict]
    arrange_notes: list[str]
    rule_problems: list[str]
    per_tier: dict
    status: str
    infeasible_reasons: list[str]
    suggestions: list[str]
    trace: list[dict]


def _trace(state: State, node: str, info: Any) -> list[dict]:
    return (state.get("trace") or []) + [{"node": node, "info": info}]


def _map_required(sku: str, assembly: bool) -> str | None:
    """整箱请求把散装 sku 换成同品种整箱礼盒，组装请求反之。"""
    inv = load_inventory()
    it = inv.get(sku)
    if it is None:
        return None
    if (assembly and it.is_bulk) or (not assembly and it.is_box) or sku == "F08":
        return sku
    key = (SKU_ALIASES.get(sku) or [None])[0]
    for other in inv.values():
        if (other.is_bulk if assembly else other.is_box) and (SKU_ALIASES.get(other.sku_id) or [None])[0] == key:
            return other.sku_id
    return None


# ---------------- 节点 ----------------
def node_parse(state: State) -> State:
    text = state["text"]
    raw = agents.parse_request(text)
    missing = [m for m in (raw.get("missing_info") or []) if isinstance(m, str)]
    try:
        qty = int(raw.get("quantity") or 0) or None
    except (TypeError, ValueError):
        qty = None
    if not qty:
        qty = 1
    try:
        budget = float(raw["budget_per_box"]) if raw.get("budget_per_box") not in (None, "", 0) else None
    except (TypeError, ValueError):
        budget = None
    if budget is None and not any("预算" in m for m in missing):
        missing.append("每份预算")
    missing = [m for m in missing if not ("预算" in m and budget is not None) and "份数" not in m]
    assembly = wants_assembly(text)
    required = []
    for r in raw.get("required_items") or []:
        if not isinstance(r, dict):
            continue
        sku = _map_required(str(r.get("sku_id", "")), assembly)
        if sku and sku != "F08" and all(x["sku_id"] != sku for x in required):
            q = 1.0 if not assembly else load_inventory()[sku].step  # 组装：顾客点名的水果只要求"有"
            required.append({"sku_id": sku, "qty": q})
    req = {
        "raw_text": text,
        "occasion": raw.get("occasion") or "未说明",
        "quantity": qty,
        "budget_per_box": budget,
        "budget_type": raw.get("budget_type") if budget is not None else None,
        "event_in_days": raw.get("event_in_days") if isinstance(raw.get("event_in_days"), int) else None,
        "exclude_skus": reconcile_exclusions(text, [s for s in (raw.get("exclude_skus") or []) if isinstance(s, str)]),
        "required_items": required,
        "wants_premium": bool(raw.get("wants_premium")),
        "packaging_pref": packaging_pref(text),
        "preferences": [p for p in (raw.get("preferences") or []) if isinstance(p, str)],
        "missing_info": missing,
        "mode": "assembly" if assembly else "box",
        "wants_durian": wants_durian(text),
    }
    req["scene"] = scene_of(req["occasion"])
    req["min_shelf_days"] = min_shelf_days(req["event_in_days"])
    targets = tier_targets(budget, req["budget_type"])
    return {"req": req, "targets": targets, "attempts": 0, "feedback": [], "trace": _trace(state, "需求理解", req)}


def node_precheck(state: State) -> State:
    req, targets = state["req"], state["targets"]
    out: State = {}
    if req["wants_durian"]:
        alts = alternatives_for(req, targets, n=3)
        out = {"status": "infeasible", "infeasible_reasons": [DURIAN_MSG],
               "suggestions": ["可以推荐这些整箱礼盒：" + "；".join(describe_option(o) for o in alts)],
               "alternatives": alts}
        return {**out, "trace": _trace(state, "可行性预检", {"榴莲": True})}
    if req["mode"] == "assembly":
        allowed, _ = allowed_bulk(req)
        alts = alternatives_for(req, targets, n=2, mentioned_text=req["raw_text"])
        reasons, sug = precheck(req, targets, [])
        out = {"allowed_bulk": [b.sku_id for b in allowed], "alternatives": alts}
    else:
        allowed, _ = allowed_boxes(req)
        options = box_options(req, allowed)
        reasons, sug = precheck(req, targets, options)
        out = {"options": options, "short": shortlist(req, options, targets)}
    if reasons:
        out.update(status="infeasible", infeasible_reasons=reasons, suggestions=sug)
    info = {"模式": req["mode"], "候选数": len(out.get("options") or out.get("allowed_bulk") or []), "不可行原因": reasons}
    return {**out, "trace": _trace(state, "可行性预检", info)}


def node_select(state: State) -> State:
    req = state["req"]
    attempt = state.get("attempts", 0) + 1
    if req["mode"] == "assembly":
        inv = load_inventory()
        res = agents.select_assembly(req, [inv[s] for s in state["allowed_bulk"]], state["targets"],
                                     ASSEMBLY_PACKAGING, state.get("feedback") or [])
        by_tier = {t.get("tier"): t for t in (res.get("tiers") or []) if isinstance(t, dict)}
        picks = {}
        for name in TIERS:
            items = []
            for i in (by_tier.get(name) or {}).get("items") or []:
                try:
                    items.append({"sku_id": str(i["sku_id"]).strip(), "qty": float(i["qty"])})
                except (KeyError, TypeError, ValueError):
                    continue
            picks[name] = items
    else:
        res = agents.select_boxes(req, state["short"], state["targets"], state.get("feedback") or [])
        by_id = {o["id"]: o for o in state["options"]}
        picks = {t: by_id.get(str((res.get("picks") or {}).get(t, "")).strip()) for t in TIERS}
    return {"picks": picks, "attempts": attempt,
            "trace": _trace(state, "选礼盒", {"第几次": attempt, "AI 选择": {
                t: (p["id"] if isinstance(p, dict) else p) for t, p in picks.items()}})}


def node_price(state: State) -> State:
    """③ 报价核算（代码）：整箱模式由程序兜底排档；组装模式在已选水果内微调用量；再算价格、成本、毛利并做规则校验。"""
    req, targets, picks = state["req"], state["targets"], state["picks"]
    q = req["quantity"]
    tiers, notes, ai_raw = [], [], {}
    if req["mode"] == "assembly":
        inv = load_inventory()
        allowed = set(state["allowed_bulk"])
        for t in TIERS:
            items = [i for i in picks.get(t) or [] if i["sku_id"] in inv]
            pkg = assembly_packaging(t)
            ai_raw[t] = price_plan(items, q, pkg)["unit_price"] if items else None
            if items:
                items = tune_assembly(items, pkg, q, targets[t], allowed,
                                      {r["sku_id"]: r["qty"] for r in req.get("required_items") or []})
            tiers.append({"tier": t, "items": items, "packaging": pkg, "kind": "组装礼盒",
                          "plan": price_plan(items, q, pkg)})
    else:
        ai_raw = {t: (p["price"] if p else None) for t, p in picks.items()}
        arranged, notes = arrange_tiers(picks, state["options"], targets, req)
        for t in TIERS:
            o = arranged.get(t)
            if o is None:
                continue
            tiers.append({"tier": t, "items": o["items"], "packaging": None, "kind": KIND_LABEL[o["kind"]],
                          "option_id": o["id"], "plan": price_plan(o["items"], q)})
    problems, per_tier = check_tiers(req, tiers, targets, state.get("options"))
    if not any(t["tier"] == "标准" for t in tiers):
        problems.append("没有可用的标准款")
    return {"tiers": tiers, "ai_raw": ai_raw, "arrange_notes": notes, "rule_problems": problems, "per_tier": per_tier,
            "trace": _trace(state, "报价核算+规则校验", {"单份价格": {t["tier"]: t["plan"]["unit_price"] for t in tiers},
                                                    "程序调整": notes, "问题": problems})}


def node_copy(state: State) -> State:
    res = agents.write_copy(state["req"], state["tiers"])
    got = [t for t in (res.get("tiers") or []) if isinstance(t, dict)]
    by_tier = {str(t.get("tier", "")).replace("款", ""): t for t in got}
    if not all(t["tier"] in by_tier for t in state["tiers"]) and len(got) == len(state["tiers"]):
        by_tier = {t["tier"]: g for t, g in zip(state["tiers"], got)}  # 档位名对不上时按顺序对应
    tiers = [{**t, "intro": str((by_tier.get(t["tier"]) or {}).get("intro", "")),
              "blessing": str((by_tier.get(t["tier"]) or {}).get("blessing", ""))} for t in state["tiers"]]
    return {"tiers": tiers, "trace": _trace(state, "文案", {t["tier"]: [t["intro"], t["blessing"]] for t in tiers})}


def node_review(state: State) -> State:
    problems, per_tier = check_tiers(state["req"], state["tiers"], state["targets"], state.get("options"), check_copy=True)
    res = agents.review_meaning(state["req"], state["tiers"])
    by_tier = {t.get("tier"): t for t in (res.get("tiers") or []) if isinstance(t, dict)}
    for t in state["tiers"]:
        name = t["tier"]
        r = by_tier.get(name) or {"pass": True}
        pt = per_tier.setdefault(name, {"issues": [], "warnings": []})
        pt["llm_pass"] = bool(r.get("pass", True))
        pt["llm_issues"] = [x for x in (r.get("issues") or []) if isinstance(x, str)]
        pt["suggestions"] = [x for x in (r.get("suggestions") or []) if isinstance(x, str)]
        if not pt["llm_pass"]:
            problems += [f"{name}款（寓意审核）：{x}" for x in pt["llm_issues"] or ["寓意不合适"]]
    return {"rule_problems": problems, "per_tier": per_tier, "trace": _trace(state, "审核", {"问题": problems})}


def node_rework(state: State) -> State:
    return {"feedback": state["rule_problems"], "trace": _trace(state, "返工", state["rule_problems"])}


def node_fail(state: State) -> State:
    text = " ".join(state.get("rule_problems", []))
    sug = []
    if "库存不足" in text:
        sug.append("减少份数，或换库存充足的礼盒")
    if "单份价格" in text:
        sug.append("调整每份预算")
    if "保鲜" in text:
        sug.append("放宽送达日期要求")
    if not sug:
        sug.append("请店员根据以上原因人工调整")
    reasons = [f"返工 {MAX_REWORK} 次后仍有问题："] + state.get("rule_problems", [])
    return {"status": "infeasible", "infeasible_reasons": reasons, "suggestions": sug, "trace": _trace(state, "不可行", reasons)}


def node_finalize(state: State) -> State:
    return {"status": "ok", "trace": _trace(state, "输出", "审核通过")}


# ---------------- 路由 ----------------
def route_after_precheck(state: State) -> str:
    return "infeasible" if state.get("status") == "infeasible" else "select"


def _route_problems(state: State, ok: str) -> str:
    if not state["rule_problems"]:
        return ok
    return "rework" if state["attempts"] <= MAX_REWORK else "fail"


def build_graph():
    g = StateGraph(State)
    for name, fn in [("parse", node_parse), ("precheck", node_precheck), ("select", node_select), ("price", node_price),
                     ("copy", node_copy), ("review", node_review), ("rework", node_rework), ("fail", node_fail),
                     ("finalize", node_finalize)]:
        g.add_node(name, fn)
    g.set_entry_point("parse")
    g.add_edge("parse", "precheck")
    g.add_conditional_edges("precheck", route_after_precheck, {"infeasible": END, "select": "select"})
    g.add_edge("select", "price")
    g.add_conditional_edges("price", lambda s: _route_problems(s, "copy"), {"copy": "copy", "rework": "rework", "fail": "fail"})
    g.add_edge("copy", "review")
    g.add_conditional_edges("review", lambda s: _route_problems(s, "ok"), {"ok": "finalize", "rework": "rework", "fail": "fail"})
    g.add_edge("rework", "select")
    g.add_edge("fail", END)
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


def run(text: str, on_progress: Callable[[str, str], None] | None = None) -> dict:
    """处理一条顾客需求。on_progress(step, node) 在每个节点完成时回调（网页进度条用）。"""
    t0 = time.time()
    usage = start_usage()
    state: State = {"text": text}
    try:
        for update in GRAPH.stream({"text": text}, {"recursion_limit": 30}, stream_mode="updates"):
            for node, delta in update.items():
                if delta:
                    state.update(delta)
                if on_progress:
                    on_progress(STEP_OF_NODE.get(node, node), node)
    except LLMError as e:
        return {"status": "error", "request": text, "error": str(e), "usage": usage, "elapsed_s": round(time.time() - t0, 2)}
    return build_output(text, state, usage, time.time() - t0)


def build_output(text: str, state: State, usage: dict, elapsed: float) -> dict:
    req = state.get("req", {})
    notes = []
    if req.get("missing_info"):
        notes.append("缺少信息：" + "、".join(req["missing_info"]) + "（请向顾客确认）")
    if req.get("budget_per_box") is None and not req.get("wants_durian"):
        notes.append("顾客未给预算，按常见价位 100 / 200 / 300 元给方案，优先推荐库存久、需尽快卖掉的礼盒")
    if req.get("scene") == "未说明" and not req.get("wants_durian"):
        notes.append("用途未说明，请询问顾客送礼对象，以便调整档次和寓意")
    if any(k in text for k in HEALTH_KEYWORDS):
        notes.append(HEALTH_DISCLAIMER)
    out = {
        "status": state.get("status"), "request": text, "requirement": req, "targets": state.get("targets"),
        "attempts": state.get("attempts", 0), "ai_raw_prices": state.get("ai_raw"), "notes": notes,
        "trace": state.get("trace", []), "usage": usage, "elapsed_s": round(elapsed, 2),
    }
    alts = state.get("alternatives") or []
    if req.get("mode") == "assembly" and alts:
        out["alternatives"] = {"reason": ASSEMBLY_REASON, "options": [
            {"desc": describe_option(o), "price": o["price"], "items": o["items"]} for o in alts]}
    if state.get("status") == "ok":
        recommended, rec_note = choose_recommended(state["tiers"], req)
        if rec_note:
            notes.append("（内部）" + rec_note)
        gw = grade_warning(req, state["tiers"])
        if gw:
            notes.append(gw)
        notes += state.get("arrange_notes") or []
        tiers = []
        for t in state["tiers"]:
            pt = state["per_tier"].get(t["tier"], {})
            tiers.append({
                "tier": t["tier"], "kind": t["kind"], "recommended": t["tier"] == recommended, "items": t["items"],
                **t["plan"], "intro": t.get("intro", ""), "blessing": t.get("blessing", ""),
                "review": {"passed": True, "warnings": pt.get("warnings", []), "suggestions": pt.get("suggestions", [])},
                "quote_text": quote_text(t["tier"], t["plan"], req["quantity"], t.get("intro", ""), t.get("blessing", "")),
            })
        out["tiers"] = tiers
        out["near_expiry_ratio"] = round(near_expiry_ratio(state["tiers"]), 3)
    else:
        out["infeasible_reasons"] = state.get("infeasible_reasons", [])
        out["suggestions"] = state.get("suggestions", [])
    return out
