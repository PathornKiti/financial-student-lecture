#!/usr/bin/env python3
"""
Pre-compute the entire FI2 fair-value path and write it to CSV + Markdown.

Print this, or paste the CSV into the spreadsheet beside your trading screen.
Because FI2 has no uncertainty, this table IS the answer key for the whole case:
every value for every tick of both periods is knowable before the case starts.

    python tools/fi2_price_table.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib.pricing import (
    ANNUAL_RATE, COMPOUND_TICKS, TICKS_PER_PERIOD, fi2_values, weekly_rate,
)

OUT = Path(__file__).resolve().parent.parent / "reference"


def rows(step: int):
    for period in (1, 2):
        for tick in range(0, TICKS_PER_PERIOD + 1, step):
            v = fi2_values(min(tick, TICKS_PER_PERIOD), period)
            yield {
                "period": period,
                "tick": tick,
                "week": tick // COMPOUND_TICKS,
                "TB6M": round(v.tb6m, 4) if v.tb6m is not None else "",
                "TB12M": round(v.tb12m, 4),
                "bond_clean": round(v.bond_clean, 4),
                "bond_dirty": round(v.bond_dirty, 4),
                "accrued": round(v.accrued, 4),
            }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    step = COMPOUND_TICKS                       # values only change on compounding ticks
    data = list(rows(step))

    csv_path = OUT / "fi2_fair_values.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)

    md_path = OUT / "fi2_fair_values.md"
    with md_path.open("w") as fh:
        fh.write("# FI2 fair values (answer key)\n\n")
        fh.write(f"Rates: {ANNUAL_RATE[1]:.0%} p.a. period 1, {ANNUAL_RATE[2]:.0%} p.a. period 2, "
                 f"compounded every {COMPOUND_TICKS} ticks.\n")
        fh.write(f"Weekly rate: {weekly_rate(1)*100:.4f}% (P1), {weekly_rate(2)*100:.4f}% (P2).\n\n")
        fh.write("Values change only on compounding ticks, so this table is complete.\n")
        fh.write("`bond_clean` is what RIT quotes and what you compare to the order book.\n\n")
        for period in (1, 2):
            fh.write(f"\n## Period {period}\n\n")
            fh.write("| tick | week | TB6M | TB12M | bond clean | bond dirty | accrued |\n")
            fh.write("|---:|---:|---:|---:|---:|---:|---:|\n")
            for r in data:
                if r["period"] != period:
                    continue
                fh.write(f"| {r['tick']} | {r['week']} | {r['TB6M'] or '-'} | {r['TB12M']} | "
                         f"{r['bond_clean']} | {r['bond_dirty']} | {r['accrued']} |\n")

    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")
    print(f"\n{len(data)} rows. Spot checks:")
    for period, tick in ((1, 0), (1, 156), (2, 0), (2, 300)):
        v = fi2_values(tick, period)
        tb6 = f"{v.tb6m:.4f}" if v.tb6m is not None else "n/a"
        print(f"  P{period} t={tick:>3}  TB6M {tb6:>8}  TB12M {v.tb12m:8.4f}  "
              f"bond clean {v.bond_clean:8.4f}")


if __name__ == "__main__":
    main()
