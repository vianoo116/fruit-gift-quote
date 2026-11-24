"""库存表读取。所有价格、库存、保鲜天数只从 data/inventory.csv 读取。

v1.1：库存以整箱原包装礼盒（type=整箱礼盒）为主；散装水果（type=散装）只用于组装礼盒；包装只用于组装礼盒。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVENTORY_CSV = DATA_DIR / "inventory.csv"

SMALL_BOX_SKUS = {"G19", "G20"}  # 伴手礼小盒：可作为「主礼盒 + 小礼盒」里的小礼盒


@dataclass(frozen=True)
class Item:
    sku_id: str
    name: str
    type: str  # 整箱礼盒 / 散装 / 包装
    spec: str
    unit: str
    cost_price: float
    wholesale_price: float
    retail_price: float
    stock: float
    shelf_days_left: int
    grade: str  # 普通 / 精品 / 高档
    packaging_style: str  # 大气 / 实在 / 精美
    gift_suitable: str  # 是 / 组装可用 / 否 / 包装
    meaning_note: str

    @property
    def is_box(self) -> bool:
        return self.type == "整箱礼盒" and self.gift_suitable == "是"

    @property
    def is_small_box(self) -> bool:
        return self.sku_id in SMALL_BOX_SKUS

    @property
    def is_bulk(self) -> bool:
        """可用于组装礼盒的散装水果。"""
        return self.type == "散装" and self.gift_suitable == "组装可用"

    @property
    def is_packaging(self) -> bool:
        return self.type == "包装"

    @property
    def step(self) -> float:
        """组装时的最小加减单位：按斤卖的可以半斤，按个卖的只能整数。"""
        return 0.5 if self.unit == "斤" else 1.0

    @property
    def weight_jin(self) -> float:
        """散装水果每单位约合多少斤（个按估算）。"""
        if self.unit == "斤":
            return 1.0
        return 0.3  # 佳沛金果约 150g/个

    @property
    def species(self) -> str:
        """同一种水果的不同规格归为同一品种（如车厘子 JJ / JJJ）。"""
        return re.sub(r"(JJJ|JJ|原箱|精品|礼盒|伴手礼小盒)", "", self.name)


@lru_cache(maxsize=1)
def load_inventory() -> dict[str, Item]:
    items: dict[str, Item] = {}
    with open(INVENTORY_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            items[row["sku_id"]] = Item(
                sku_id=row["sku_id"], name=row["name"], type=row["type"], spec=row["spec"],
                unit=row["unit"], cost_price=float(row["cost_price"]),
                wholesale_price=float(row["wholesale_price"]), retail_price=float(row["retail_price"]),
                stock=float(row["stock"]), shelf_days_left=int(row["shelf_days_left"]),
                grade=row["grade"], packaging_style=row["packaging_style"],
                gift_suitable=row["gift_suitable"], meaning_note=row["meaning_note"],
            )
    return items


def boxes() -> list[Item]:
    return [i for i in load_inventory().values() if i.is_box]


def bulk_fruits() -> list[Item]:
    return [i for i in load_inventory().values() if i.is_bulk]
