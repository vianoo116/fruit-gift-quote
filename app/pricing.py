"""报价核算：纯 Python 计算，不让模型算数。"""
from __future__ import annotations

from .inventory import load_inventory
from .rules import MARGIN_FLOOR


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def price_plan(items: list[dict], packaging_sku: str, quantity: int) -> dict:
    """计算一档方案的单份价格（按批发价）、总价、成本、毛利率。

    items: [{"sku_id": "F02", "qty": 2}]，qty 为每份用量（单位见库存表）。
    """
    inv = load_inventory()
    lines = []
    unit_price = 0.0
    unit_cost = 0.0
    for it in items:
        item = inv[it["sku_id"]]
        qty = float(it["qty"])
        line_price = item.wholesale_price * qty
        line_cost = item.cost_price * qty
        unit_price += line_price
        unit_cost += line_cost
        lines.append({
            "sku_id": item.sku_id, "name": item.name, "qty": qty, "unit": item.unit,
            "unit_price": item.wholesale_price, "line_price": r2(line_price),
            "shelf_days_left": item.shelf_days_left,
        })
    pkg = inv[packaging_sku]
    unit_price += pkg.wholesale_price
    unit_cost += pkg.cost_price
    unit_price, unit_cost = r2(unit_price), r2(unit_cost)
    total_price = r2(unit_price * quantity)
    total_cost = r2(unit_cost * quantity)
    margin = r2((unit_price - unit_cost) / unit_price) if unit_price else 0.0
    return {
        "lines": lines,
        "packaging": {"sku_id": pkg.sku_id, "name": pkg.name, "price": pkg.wholesale_price},
        "unit_price": unit_price,
        "unit_cost": unit_cost,
        "total_price": total_price,
        "total_cost": total_cost,
        "gross_margin": margin,
        "margin_warning": margin < MARGIN_FLOOR,
    }


def quote_text(tier: str, plan: dict, quantity: int, intro: str = "", blessing: str = "") -> str:
    """给顾客的报价文本：不含成本和毛利。"""
    lines = [f"【{tier}款】水果礼盒"]
    for ln in plan["lines"]:
        q = ln["qty"]
        q_str = str(int(q)) if q == int(q) else str(q)
        lines.append(f"· {ln['name']} {q_str}{ln['unit']}")
    lines.append(f"· {plan['packaging']['name']}")
    if intro:
        lines.append(intro)
    lines.append(f"单份价格：{plan['unit_price']:.2f} 元")
    lines.append(f"数量：{quantity} 份，合计：{plan['total_price']:.2f} 元")
    if blessing:
        lines.append(f"祝福语：{blessing}")
    return "\n".join(lines)
