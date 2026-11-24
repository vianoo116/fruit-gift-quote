"""库存表读取。所有价格、库存、保鲜天数只从 data/inventory.csv 读取。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVENTORY_CSV = DATA_DIR / "inventory.csv"


@dataclass(frozen=True)
class Item:
    sku_id: str
    name: str
    origin: str
    unit: str
    cost_price: float
    wholesale_price: float
    retail_price: float
    stock: float
    shelf_days_left: int
    gift_suitable: str  # 是 / 否 / 包装
    meaning_note: str

    @property
    def is_packaging(self) -> bool:
        return self.gift_suitable == "包装"

    @property
    def is_gift_fruit(self) -> bool:
        return self.gift_suitable == "是"

    @property
    def step(self) -> float:
        """最小加减单位：按斤卖的可以半斤，按个/盒卖的只能整数。"""
        return 0.5 if self.unit == "斤" else 1.0

    @property
    def weight_jin(self) -> float:
        """每单位约合多少斤（用于统计临期水果的重量占比，个/盒为估算值）。"""
        if self.unit == "斤":
            return 1.0
        if self.unit.startswith("盒"):
            return 0.25  # 125g
        if self.sku_id == "F27":  # 柚子约 2.5 斤/个
            return 2.5
        if self.sku_id == "F25":  # 椰青约 3 斤/个
            return 3.0
        return 0.3  # 金果、牛油果等小个水果


@lru_cache(maxsize=1)
def load_inventory() -> dict[str, Item]:
    items: dict[str, Item] = {}
    with open(INVENTORY_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            items[row["sku_id"]] = Item(
                sku_id=row["sku_id"],
                name=row["name"],
                origin=row["origin"],
                unit=row["unit"],
                cost_price=float(row["cost_price"]),
                wholesale_price=float(row["wholesale_price"]),
                retail_price=float(row["retail_price"]),
                stock=float(row["stock"]),
                shelf_days_left=int(row["shelf_days_left"]),
                gift_suitable=row["gift_suitable"],
                meaning_note=row["meaning_note"],
            )
    return items


def fruits() -> list[Item]:
    return [i for i in load_inventory().values() if not i.is_packaging]


def packaging() -> list[Item]:
    return [i for i in load_inventory().values() if i.is_packaging]
