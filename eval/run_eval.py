"""批量评测：逐条运行 data/test_cases.json，统计 PRD 第 7 节指标，写入 eval/results.md。

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
from app.rules import NEAR_EXPIRY_DAYS, TIER_PACKAGING, tier_targets  # noqa: E402

CASES = ROOT / "data" / "test_cases.json"
OUT_MD = ROOT / "eval" / "results.md"
OUT_RAW = ROOT / "eval" / "raw_results.json"


def recompute_unit_price(tier: dict) -> float:
    """独立按价格表重算单份价格（不调用 app.pricing）。"""
    inv = load_inventory()
    total = sum(inv[i["sku_id"]].wholesale_price * float(i["qty"]) for i in tier["items"])
    total += inv[TIER_PACKAGING[tier["tier"]]].wholesale_price
    return round(total, 2)


def evaluate_case(case: dict, res: dict) -> dict:
    inv = load_inventory()
    exp = case["expected"]
    feasible_pred = res.get("status") == "ok"
    row = {"id": case["id"], "request": case["request"], "status": res.get("status"),
           "expected_feasible": exp["feasible"], "elapsed_s": res.get("elapsed_s", 0),
           "cost_usd": res.get("usage", {}).get("cost_usd", 0.0), "attempts": res.get("attempts", 0)}
    req = res.get("requirement") or {}
    row["parsed"] = {"quantity": req.get("quantity"), "budget": req.get("budget_per_box"), "occasion": req.get("occasion")}
    row["parse_ok"] = (req.get("quantity") == exp["quantity"]
                       and (req.get("budget_per_box") or None) == (float(exp["budget_per_box"]) if exp["budget_per_box"] else None))
    if feasible_pred:
        tiers = res["tiers"]
        row["price_checks"] = [abs(recompute_unit_price(t) - t["unit_price"]) < 0.01
                               and abs(round(t["unit_price"] * req["quantity"], 2) - t["total_price"]) < 0.01 for t in tiers]
        row["stock_checks"] = [all(float(i["qty"]) * req["quantity"] <= inv[i["sku_id"]].stock for i in t["items"]) for t in tiers]
        row["taboo_violations"] = sum(1 for t in tiers for i in t["items"] if i["sku_id"] in exp["must_exclude"])
        # 预算按测试用例给的预算判断；顾客说"以内"时标准款不得超过预算（店主确认的规则）
        if exp["budget_per_box"]:
            tg = tier_targets(float(exp["budget_per_box"]), req.get("budget_type"))["标准"]
        else:
            tg = tier_targets(None, None)["标准"]
        std = next(t for t in tiers if t["tier"] == "标准")
        row["std_price"] = std["unit_price"]
        row["budget_ok"] = tg["low"] - 1e-6 <= std["unit_price"] <= tg["high"] + 1e-6
        total_w = near_w = 0.0
        for t in tiers:
            for i in t["items"]:
                w = inv[i["sku_id"]].weight_jin * float(i["qty"])
                total_w += w
                near_w += w if inv[i["sku_id"]].shelf_days_left <= NEAR_EXPIRY_DAYS else 0
        row["near_expiry_ratio"] = near_w / total_w if total_w else 0.0
        row["prices"] = {t["tier"]: t["unit_price"] for t in tiers}
        row["margins"] = {t["tier"]: t["gross_margin"] for t in tiers}
        row["tiers_brief"] = {t["tier"]: "、".join(f"{ln['name']}{ln['qty']:g}{ln['unit']}" for ln in t["lines"]) for t in tiers}
    else:
        row["reasons"] = res.get("infeasible_reasons") or [res.get("error", "")]
    return row


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%（{a}/{b}）" if b else "—"


def summarize(rows: list[dict]) -> dict:
    feas_rows = [r for r in rows if r["status"] == "ok"]
    price = [c for r in feas_rows for c in r["price_checks"]]
    stock = [c for r in feas_rows for c in r["stock_checks"]]
    exp_feasible = [r for r in rows if r["expected_feasible"]]
    exp_infeasible = [r for r in rows if not r["expected_feasible"]]
    budget_hits = sum(1 for r in exp_feasible if r["status"] == "ok" and r.get("budget_ok"))
    near = [r["near_expiry_ratio"] for r in feas_rows]
    return {
        "报价计算准确率": (pct(sum(price), len(price)), "100%"),
        "预算符合率": (pct(budget_hits, len(exp_feasible)), "≥ 90%"),
        "库存可行率": (pct(sum(stock), len(stock)), "100%"),
        "忌讳违规数": (str(sum(r["taboo_violations"] for r in feas_rows)), "0"),
        "不可行需求识别率": (pct(sum(1 for r in exp_infeasible if r["status"] != "ok"), len(exp_infeasible)), "≥ 90%"),
        "可行需求误判为不可行": (str(sum(1 for r in exp_feasible if r["status"] != "ok")), "记录"),
        "临期水果占比（≤5 天，按重量）": (f"{sum(near) / len(near) * 100:.1f}%" if near else "—", "记录"),
        "需求解析准确率（份数+预算）": (pct(sum(r["parse_ok"] for r in rows), len(rows)), "记录"),
        "平均生成耗时": (f"{sum(r['elapsed_s'] for r in rows) / len(rows):.1f} 秒", "记录"),
        "平均单次成本": (f"${sum(r['cost_usd'] for r in rows) / len(rows):.4f}", "记录"),
        "平均搭配次数": (f"{sum(r['attempts'] for r in rows) / len(rows):.2f}", "记录"),
    }


def write_md(rows: list[dict], summary: dict, wall: float) -> None:
    lines = ["# 评测结果", "",
             f"- 运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 模型：`{model_name()}`（OpenRouter）",
             f"- 用例：`data/test_cases.json` 共 {len(rows)} 条；总费用 ${sum(r['cost_usd'] for r in rows):.4f}；总耗时 {wall:.0f} 秒（并行运行）",
             "", "## 汇总", "", "| 指标 | 结果 | 目标 |", "| --- | --- | --- |"]
    lines += [f"| {k} | {v[0]} | {v[1]} |" for k, v in summary.items()]
    lines += ["", "指标说明：报价计算准确率、库存可行率按「方案档位」统计（每条可行需求 3 档）；预算符合率的分母是期望可行的需求，"
              "被系统判为不可行的也算不符合；临期占比中按个/盒卖的商品按估算重量折算。", "",
              "## 每条用例明细", "",
              "| 用例 | 需求 | 期望 | 结果 | 解析（份数/预算/场合） | 标准款单价 | 预算符合 | 三档单价（经济/标准/高档） | 最低毛利 | 忌讳违规 | 临期占比 | 搭配次数 | 耗时 | 成本 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        p = r["parsed"]
        exp = "可行" if r["expected_feasible"] else "不可行"
        got = {"ok": "可行", "infeasible": "不可行"}.get(r["status"], r["status"] or "错误")
        mark = "✅" if (r["status"] == "ok") == r["expected_feasible"] else "❌"
        if r["status"] == "ok":
            pr = r["prices"]
            cells = [f"{r['std_price']:.2f}", "✅" if r["budget_ok"] else "❌",
                     f"{pr['经济']:.2f} / {pr['标准']:.2f} / {pr['高档']:.2f}",
                     f"{min(r['margins'].values()) * 100:.0f}%", str(r["taboo_violations"]),
                     f"{r['near_expiry_ratio'] * 100:.0f}%"]
        else:
            cells = ["—"] * 6
        lines.append(f"| {r['id']} | {r['request']} | {exp} | {mark} {got} | {p['quantity']} / {p['budget']} / {p['occasion']} | "
                     + " | ".join(cells) + f" | {r['attempts']} | {r['elapsed_s']:.1f}s | ${r['cost_usd']:.4f} |")
    lines += ["", "## 方案与不可行原因", ""]
    for r in rows:
        lines.append(f"**{r['id']}** {r['request']}")
        lines.append("")
        if r["status"] == "ok":
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
        write_md(rows, summary, wall)
    for k, v in summary.items():
        print(f"{k}: {v[0]}（目标 {v[1]}）")
    print(f"本次进程累计费用 ${process_cost():.4f}")


if __name__ == "__main__":
    main()
