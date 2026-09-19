"""Regenerate the synthetic account CSVs. The committed files came from this script.

Each account gets 30 days of usage for three products. The seed is fixed, and the
script redraws any account whose top product or peak day would be a tie, so the
completion check always has one right answer.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

ACCOUNTS_DIR = Path(__file__).resolve().parents[1] / "demos/01_agent_worker/data/accounts"
PRODUCTS = ["search", "storage", "compute"]
START = date(2026, 8, 1)


def draw(rng):
    base = {product: rng.randint(40, 400) for product in PRODUCTS}
    return [
        (START + timedelta(days=day), product, max(0, int(rng.gauss(base[product], base[product] / 4))))
        for day in range(30) for product in PRODUCTS
    ]


def has_tie(rows):
    by_product, by_day = {}, {}
    for day, product, units in rows:
        by_product[product] = by_product.get(product, 0) + units
        by_day[day] = by_day.get(day, 0) + units
    return any(sorted(t.values())[-1] == sorted(t.values())[-2] for t in (by_product, by_day))


if __name__ == "__main__":
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20260918)
    for number in range(1, 41):
        rows = draw(rng)
        while has_tie(rows):
            rows = draw(rng)
        with open(ACCOUNTS_DIR / f"acct-{number:03d}.csv", "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "product", "units"])
            writer.writerows((day.isoformat(), product, units) for day, product, units in rows)
    print(f"wrote 40 accounts to {ACCOUNTS_DIR}")
