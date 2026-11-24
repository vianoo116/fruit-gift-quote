"""报价核算：纯 Python 计算，不让模型算数。整箱礼盒和组装礼盒共用。"""
from __future__ import annotations

from .inventory import load_inventory
from .rules import MARGIN_FLOOR


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def fmt_qty(q: float) -> str:
    return str(int(q)) if q == int(q) else f"{q:g}"


def price_plan(items: list[dict], quantity: int, packaging_sku: str | None = None) -> dict:
    """计算一档方案的单份价格（按批发价）、总价、成本、毛利率。

    items: [{"sku_id": "G08", "qty": 1}]，qty 为每份用量（整箱礼盒按箱/盒，散装按斤/个）。
    packaging_sku: 组装礼盒的包装（整箱礼盒自带包装，传 None）。
    """
    inv = load_inventory()
    lines = []
    unit_price = unit_cost = 0.0
    for it in items:
        item = inv[it["sku_id"]]
        qty = float(it["qty"])
        unit_price += item.wholesale_price * qty
        unit_cost += item.cost_price * qty
        lines.append({
            "sku_id": item.sku_id, "name": item.name, "type": item.type, "spec": item.spec,
            "grade": item.grade, "packaging_style": item.packaging_style,
            "qty": qty, "unit": item.unit, "unit_price": item.wholesale_price,
            "line_price": r2(item.wholesale_price * qty), "shelf_days_left": item.shelf_days_left,
        })
    packaging = None
    if packaging_sku:
        pkg = inv[packaging_sku]
        unit_price += pkg.wholesale_price
        unit_cost += pkg.cost_price
        packaging = {"sku_id": pkg.sku_id, "name": pkg.name, "price": pkg.wholesale_price}
    unit_price, unit_cost = r2(unit_price), r2(unit_cost)
    margin = r2((unit_price - unit_cost) / unit_price) if unit_price else 0.0
    return {
        "lines": lines,
        "packaging": packaging,
        "unit_price": unit_price,
        "unit_cost": unit_cost,
        "total_price": r2(unit_price * quantity),
        "total_cost": r2(unit_cost * quantity),
        "gross_margin": margin,
        "margin_warning": margin < MARGIN_FLOOR,
    }


def quote_text(tier: str, plan: dict, quantity: int, intro: str = "", blessing: str = "") -> str:
    """给顾客的报价文本：不含成本和毛利。"""
    lines = [f"【{tier}款】"]
    for ln in plan["lines"]:
        spec = f"（{ln['spec']}）" if ln["type"] == "整箱礼盒" and ln["spec"] else ""
        lines.append(f"· {ln['name']}{spec} × {fmt_qty(ln['qty'])}{ln['unit']}")
    if plan.get("packaging"):
        lines.append(f"· {plan['packaging']['name']}")
    if intro:
        lines.append(intro)
    lines.append(f"单份价格：{plan['unit_price']:.2f} 元")
    lines.append(f"数量：{quantity} 份，合计：{plan['total_price']:.2f} 元")
    if blessing:
        lines.append(f"祝福语：{blessing}")
    return "\n".join(lines)
