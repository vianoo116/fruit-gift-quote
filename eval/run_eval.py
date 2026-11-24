"""批量评测：逐条运行 data/test_cases.json，统计 PRD 第 7 节及 v1.1 新增指标，写入 eval/results.md。

用法：python eval/run_eval.py [--workers 4] [--only T01,T05]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.graph import run  # noqa: E402
from app.inventory import load_inventory  # noqa: E402
from app.llm import model_name, process_cost  # noqa: E402
from app.rules import ASSEMBLY_PACKAGING, NEAR_EXPIRY_DAYS, tier_targets  # noqa: E402

CASES = ROOT / "data" / "test_cases.json"
OUT_MD = ROOT / "eval" / "results.md"
OUT_RAW = ROOT / "eval" / "raw_results.json"


def recompute_unit_price(tier: dict, assembly: bool) -> float:
    """独立按价格表重算单份价格（不调用 app.pricing）。"""
    inv = load_inventory()
    total = sum(inv[i["sku_id"]].wholesale_price * float(i["qty"]) for i in tier["items"])
    if assembly:
        total += inv[ASSEMBLY_PACKAGING[tier["tier"]]].wholesale_price
    return round(total, 2)


def _in_window(price, tg) -> bool:
    return price is not None and tg["low"] - 1e-6 <= price <= tg["high"] + 1e-6


def _main(tier: dict):
    inv = load_inventory()
    return max((inv[i["sku_id"]] for i in tier["items"]), key=lambda it: it.wholesale_price * 1)


def evaluate_case(case: dict, res: dict) -> dict:
    inv = load_inventory()
    exp = case["expected"]
    ok = res.get("status") == "ok"
    req = res.get("requirement") or {}
    assembly = req.get("mode") == "assembly"
    row = {"id": case["id"], "request": case["request"], "status": res.get("status"), "expected": exp,
           "expected_feasible": exp["feasible"], "elapsed_s": res.get("elapsed_s", 0),
           "cost_usd": res.get("usage", {}).get("cost_usd", 0.0), "attempts": res.get("attempts", 0),
           "mode": req.get("mode"), "calls": res.get("usage", {}).get("calls", 0)}
    row["parsed"] = {"quantity": req.get("quantity"), "budget": req.get("budget_per_box"), "occasion": req.get("occasion")}
    row["parse_ok"] = (req.get("quantity") == exp["quantity"]
                       and (req.get("budget_per_box") or None) == (float(exp["budget_per_box"]) if exp["budget_per_box"] else None))
    bt = req.get("budget_type") if exp["budget_per_box"] else None
    tg = tier_targets(float(exp["budget_per_box"]) if exp["budget_per_box"] else None, bt)["标准"]
    raw = (res.get("ai_raw_prices") or {}).get("标准")
    row["ai_raw_std"] = raw
    row["ai_raw_budget_ok"] = _in_window(raw, tg) if raw is not None else None
    row["durian_in_box"] = 0
    if ok:
        tiers = res["tiers"]
        row["price_checks"] = [abs(recompute_unit_price(t, assembly) - t["unit_price"]) < 0.01
                               and abs(round(t["unit_price"] * req["quantity"], 2) - t["total_price"]) < 0.01 for t in tiers]
        row["stock_checks"] = [all(float(i["qty"]) * req["quantity"] <= inv[i["sku_id"]].stock for i in t["items"]) for t in tiers]
        row["taboo_violations"] = sum(1 for t in tiers for i in t["items"] if i["sku_id"] in exp["must_exclude"])
        row["durian_in_box"] = sum(1 for t in tiers for i in t["items"] if i["sku_id"] == "F08")
        std = next((t for t in tiers if t["tier"] == "标准"), None)
        row["std_price"] = std["unit_price"] if std else None
        row["budget_ok"] = bool(std) and _in_window(std["unit_price"], tg)
        row["all_boxes"] = all(inv[i["sku_id"]].is_box for t in tiers for i in t["items"])
        main = _main(std) if std else None
        row["std_grade"] = main.grade if main else None
        row["std_style"] = main.packaging_style if main else None
        total = sum(inv[i["sku_id"]].wholesale_price * float(i["qty"]) for t in tiers for i in t["items"])
        near = sum(inv[i["sku_id"]].wholesale_price * float(i["qty"]) for t in tiers for i in t["items"]
                   if inv[i["sku_id"]].shelf_days_left <= NEAR_EXPIRY_DAYS)
        row["near_expiry_ratio"] = near / total if total else 0.0
        row["prices"] = {t["tier"]: t["unit_price"] for t in tiers}
        row["margins"] = {t["tier"]: t["gross_margin"] for t in tiers}
        row["tiers_brief"] = {t["tier"]: f"[{t['kind']}] " + "、".join(
            f"{ln['name']}{'' if ln['type'] == '整箱礼盒' and ln['qty'] == 1 else ' ' + format(ln['qty'], 'g') + ln['unit']}"
            for ln in t["lines"]) for t in tiers}
        row["alternatives"] = [o["desc"] for o in (res.get("alternatives") or {}).get("options", [])]
    else:
        row["reasons"] = (res.get("infeasible_reasons") or [res.get("error", "")]) + [f"建议：{x}" for x in res.get("suggestions") or []]
    return row


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%（{a}/{b}）" if b else "—"


def summarize(rows: list[dict]) -> dict:
    ok_rows = [r for r in rows if r["status"] == "ok"]
    price = [c for r in ok_rows for c in r["price_checks"]]
    stock = [c for r in ok_rows for c in r["stock_checks"]]
    exp_feasible = [r for r in rows if r["expected_feasible"]]
    exp_infeasible = [r for r in rows if not r["expected_feasible"]]
    box_cases = [r for r in exp_feasible if r["expected"].get("expect_type") != "组装礼盒"]
    grade_cases = [r for r in rows if r["expected"].get("expect_grade")]
    pack_cases = [r for r in rows if r["expected"].get("expect_packaging")]
    raw_cases = [r for r in exp_feasible if r["ai_raw_budget_ok"] is not None]
    near = [r["near_expiry_ratio"] for r in ok_rows]
    return {
        "报价计算准确率": (pct(sum(price), len(price)), "100%"),
        "预算符合率（最终方案）": (pct(sum(1 for r in exp_feasible if r["status"] == "ok" and r.get("budget_ok")), len(exp_feasible)), "≥ 90%"),
        "AI 原始方案预算符合率（程序调整前）": (pct(sum(1 for r in raw_cases if r["ai_raw_budget_ok"]), len(raw_cases)), "记录"),
        "库存可行率": (pct(sum(stock), len(stock)), "100%"),
        "忌讳违规数": (str(sum(r["taboo_violations"] for r in ok_rows)), "0"),
        "榴莲误入礼盒次数": (str(sum(r["durian_in_box"] for r in rows)), "0"),
        "不可行需求识别率": (pct(sum(1 for r in exp_infeasible if r["status"] != "ok"), len(exp_infeasible)), "≥ 90%"),
        "可行需求误判为不可行": (str(sum(1 for r in exp_feasible if r["status"] != "ok")), "0"),
        "整箱礼盒优先率（非组装请求）": (pct(sum(1 for r in box_cases if r["status"] == "ok" and r["all_boxes"]), len(box_cases)), "100%"),
        "场景档次匹配率": (pct(sum(1 for r in grade_cases if r.get("std_grade") == r["expected"]["expect_grade"]), len(grade_cases)), "记录"),
        "包装偏好匹配率": (pct(sum(1 for r in pack_cases if r.get("std_style") == r["expected"]["expect_packaging"]), len(pack_cases)), "记录"),
        "临期商品占比（≤5 天，按金额）": (f"{sum(near) / len(near) * 100:.1f}%" if near else "—", "记录"),
        "需求解析准确率（份数+预算）": (pct(sum(r["parse_ok"] for r in rows), len(rows)), "记录"),
        "平均生成耗时": (f"{sum(r['elapsed_s'] for r in rows) / len(rows):.1f} 秒", "≤ 30 秒"),
        "超过 30 秒的比例": (pct(sum(1 for r in rows if r["elapsed_s"] > 30), len(rows)), "记录"),
        "平均模型调用次数": (f"{sum(r['calls'] for r in rows) / len(rows):.1f}", "记录"),
        "平均单次成本": (f"${sum(r['cost_usd'] for r in rows) / len(rows):.4f}", "记录"),
    }


def write_md(rows: list[dict], summary: dict, wall: float, workers: int) -> None:
    lines = ["# 评测结果（v1.1）", "",
             f"- 运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 模型：`{model_name()}`（OpenRouter）",
             f"- 用例：`data/test_cases.json` 共 {len(rows)} 条；总费用 ${sum(r['cost_usd'] for r in rows):.4f}；"
             f"总耗时 {wall:.0f} 秒（{workers} 条并行，单条耗时为该条自己的耗时）",
             "", "## 汇总", "", "| 指标 | 结果 | 目标 |", "| --- | --- | --- |"]
    lines += [f"| {k} | {v[0]} | {v[1]} |" for k, v in summary.items()]
    lines += ["", "指标说明：",
              "- 报价计算准确率、库存可行率按「方案档位」统计；评测脚本独立按价格表重算价格。",
              "- 预算符合率：分母是期望可行的需求（含没给预算、按 200 元标准款判断的 T11），被判不可行或标准款不在区间内都算不符合。",
              "- AI 原始方案预算符合率：选礼盒 Agent 给出的标准款（程序兜底替换 / 组装用量微调之前）落在预算区间的比例。",
              "- 整箱礼盒优先率：期望可行、且不是组装请求的用例中，三档全部是整箱礼盒的比例。",
              "- 场景档次 / 包装偏好匹配率：看标准款主礼盒（价格最高的一款）的 grade / packaging_style 是否与用例期望一致。",
              "- 临期商品占比：方案金额中剩余保鲜 ≤ 5 天的商品所占比例。", "",
              "## 每条用例明细", "",
              "| 用例 | 需求 | 期望 | 结果 | 解析（份数/预算/场合） | 标准款单价 | AI原始 | 预算符合 | 三档单价（经济/标准/高档） | 标准款档次/包装 | 最低毛利 | 忌讳违规 | 选礼盒次数 | 模型调用 | 耗时 | 成本 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        p = r["parsed"]
        exp = "可行" if r["expected_feasible"] else "不可行"
        got = {"ok": "可行", "infeasible": "不可行"}.get(r["status"], r["status"] or "错误")
        mark = "✅" if (r["status"] == "ok") == r["expected_feasible"] else "❌"
        raw = f"{r['ai_raw_std']:.0f}" if r["ai_raw_std"] else "—"
        if r["status"] == "ok":
            pr = r["prices"]
            three = " / ".join(f"{pr[t]:.0f}" if t in pr else "—" for t in ("经济", "标准", "高档"))
            cells = [f"{r['std_price']:.2f}" if r["std_price"] else "—", raw, "✅" if r["budget_ok"] else "❌", three,
                     f"{r['std_grade']}/{r['std_style']}", f"{min(r['margins'].values()) * 100:.0f}%", str(r["taboo_violations"])]
        else:
            cells = ["—", raw, "—", "—", "—", "—", "—"]
        slow = " ⚠" if r["elapsed_s"] > 30 else ""
        lines.append(f"| {r['id']} | {r['request']} | {exp} | {mark} {got} | {p['quantity']} / {p['budget']} / {p['occasion']} | "
                     + " | ".join(cells) + f" | {r['attempts']} | {r['calls']} | {r['elapsed_s']:.1f}s{slow} | ${r['cost_usd']:.4f} |")
    lines += ["", "## 方案与不可行原因", ""]
    for r in rows:
        lines.append(f"**{r['id']}** {r['request']}")
        lines.append("")
        if r["status"] == "ok":
            for a in r.get("alternatives") or []:
                lines.append(f"- 先推荐整箱：{a}")
            for t, desc in r["tiers_brief"].items():
                lines.append(f"- {t}：{desc}（{r['prices'][t]:.2f} 元/份）")
        else:
            for x in r.get("reasons", []):
                lines.append(f"- {x}")
        lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    t0 = time.time()

    def one(c):
        res = copy_context().run(run, c["request"])
        print(f"{c['id']} {res.get('status')} {res.get('elapsed_s')}s ${res.get('usage', {}).get('cost_usd', 0):.4f}", flush=True)
        return res

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(one, cases))
    wall = time.time() - t0
    rows = [evaluate_case(c, r) for c, r in zip(cases, results)]
    summary = summarize(rows)
    OUT_RAW.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.only:
        write_md(rows, summary, wall, args.workers)
    for k, v in summary.items():
        print(f"{k}: {v[0]}（目标 {v[1]}）")
    print(f"本次进程累计费用 ${process_cost():.4f}")


if __name__ == "__main__":
    main()
