#!/usr/bin/env python3
"""
Real-time pricing desk: every security, its fair value, the DERIVATION behind
that value, and exactly what to do at the current bid/ask - ranked by dollars.

Read-only. It sends no orders, so it works even when the case has API order
submission disabled, and it is safe to leave running beside a bot.

    python tools/desk.py                 # auto-detects the case
    python tools/desk.py --min-edge 0.04 --bell

Why the derivation is on screen: when the bot says SELL a T-bill you should be
able to check in one glance WHY. T-bills pay no coupon, so they must trade below
par - if the market anchors near 100 they are badly overpriced and selling is
correct. Seeing "pays $100 in 24wk @7% -> 96.67" next to "market 99.80" makes
that obvious instead of surprising.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib.client import RITClient, RITError
from ritlib.microstructure import OrderBook
from ritlib.news import apply_news, load_overrides
from ritlib.pricing import (
    BOND, BOND_COMMISSION, COMP_PE, COMPOUND_TICKS, EPSBook, EV1_FEE, EV1_TICKER,
    TB6M, TB12M, TICKS_PER_PERIOD, ANNUAL_RATE, fi2_values, weeks_remaining,
)

CLR = "\033[2J\033[H"
G, R, Y, D, B, X = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def money(x: float) -> str:
    return f"${x:,.0f}"


def fi2_rows(c: RITClient, case: dict, secs: dict, args) -> list[dict]:
    tick, period = int(case["tick"]), int(case["period"])
    v = fi2_values(min(tick, TICKS_PER_PERIOD), period)
    wks = weeks_remaining(min(tick, TICKS_PER_PERIOD))
    rate = ANNUAL_RATE.get(period, 0.07)

    why = {
        TB6M: f"$100 at end P1 · {wks}wk @ {rate:.0%}",
        TB12M: (f"$100 at end P2 · {wks}wk @ {rate:.0%} then 26wk @ 9%"
                if period == 1 else f"$100 at end P2 · {wks}wk @ 9%"),
        BOND: (f"$5 at end P1 + $105 at end P2 · accrued {v.accrued:.3f}"
               if period == 1 else f"$105 at end P2 · accrued {v.accrued:.3f}"),
    }

    rows = []
    for ticker in (TB6M, TB12M, BOND):
        theo = v.theo(ticker)
        if theo is None or ticker not in secs:
            continue
        s = secs[ticker]
        bid, ask = s.get("bid"), s.get("ask")
        if bid is None or ask is None:
            continue
        try:
            ob = OrderBook.from_api(c.book(ticker, limit=20), ticker)
        except RITError:
            ob = None

        buy_edge = theo - ask - BOND_COMMISSION
        sell_edge = bid - theo - BOND_COMMISSION
        side = qty = limit = None
        if buy_edge > args.min_edge:
            side = "BUY"
            limit = math.floor((theo - BOND_COMMISSION - args.min_edge) * 100) / 100
            qty = ob.max_qty_for_avg_price("BUY", theo - BOND_COMMISSION - args.min_edge) if ob else 0
        elif sell_edge > args.min_edge:
            side = "SELL"
            limit = math.ceil((theo + BOND_COMMISSION + args.min_edge) * 100) / 100
            qty = ob.max_qty_for_avg_price("SELL", theo + BOND_COMMISSION + args.min_edge) if ob else 0

        qty = min(qty or 0, args.max_order * 20)
        rows.append(dict(ticker=ticker, theo=theo, bid=bid, ask=ask, why=why[ticker],
                         buy=buy_edge, sell=sell_edge, side=side, qty=qty, limit=limit,
                         pos=int(s.get("position", 0)), unit="bond",
                         value=(max(buy_edge, sell_edge) * qty) if side else 0.0))
    return rows


def ev1_rows(c: RITClient, case: dict, secs: dict, args, book: EPSBook,
             seen: set, override: Path) -> list[dict]:
    apply_news(book, c.news(limit=50), seen)
    load_overrides(override, book)
    if EV1_TICKER not in secs:
        return []
    s = secs[EV1_TICKER]
    bid, ask = s.get("bid"), s.get("ask")
    if bid is None or ask is None:
        return []
    fv = book.fair_value
    parts = " + ".join(f"{book.eps[q]:.2f}{'A' if book.actual[q] else 'E'}" for q in (1, 2, 3, 4))

    try:
        ob = OrderBook.from_api(c.book(EV1_TICKER, limit=20), EV1_TICKER)
    except RITError:
        ob = None
    buy_edge, sell_edge = fv - ask - EV1_FEE, bid - fv - EV1_FEE
    side = qty = limit = None
    if buy_edge > args.min_edge:
        side, limit = "BUY", math.floor((fv - EV1_FEE - args.min_edge) * 100) / 100
        qty = ob.max_qty_for_avg_price("BUY", fv - EV1_FEE - args.min_edge) if ob else 0
    elif sell_edge > args.min_edge:
        side, limit = "SELL", math.ceil((fv + EV1_FEE + args.min_edge) * 100) / 100
        qty = ob.max_qty_for_avg_price("SELL", fv + EV1_FEE + args.min_edge) if ob else 0

    return [dict(ticker=EV1_TICKER, theo=fv, bid=bid, ask=ask,
                 why=f"({parts}) x {COMP_PE} · {book.n_actual}/4 reported",
                 buy=buy_edge, sell=sell_edge, side=side, qty=min(qty or 0, 100_000),
                 limit=limit, pos=int(s.get("position", 0)), unit="share",
                 value=(max(buy_edge, sell_edge) * (qty or 0)) if side else 0.0)]


def render(case: dict, rows: list[dict], nlv: float, args) -> None:
    print(CLR, end="")
    print(f"{B}{case.get('name','?')}{X}   period {case.get('period')}"
          f"/{case.get('total_periods','?')}   tick {case.get('tick')}"
          f"/{case.get('ticks_per_period')}   {case.get('status')}"
          f"   NLV {money(nlv)}")
    print(f"{D}fair value vs market · edge is net of fees · read-only, sends nothing{X}\n")

    for r in rows:
        mark = G if r["side"] == "BUY" else (R if r["side"] == "SELL" else D)
        print(f"{B}{r['ticker']:<7}{X} theo {B}{r['theo']:>9.4f}{X}   "
              f"market {r['bid']:>8.2f} / {r['ask']:<8.2f}  pos {r['pos']:+,}")
        print(f"{D}        {r['why']}{X}")
        print(f"        buy edge {r['buy']:+.3f}   sell edge {r['sell']:+.3f}")
        if r["side"]:
            print(f"        {mark}{B}{r['side']} {r['qty']:,} @ {r['limit']:.2f}{X}"
                  f"{mark}  ->  {money(r['value'])}{X}")
        print()

    live = [r for r in rows if r["side"]]
    live.sort(key=lambda r: -r["value"])
    print("=" * 74)
    if live:
        print(f"{B}>>> DO THIS NOW{X}")
        for r in live:
            clips = -(-r["qty"] // args.max_order) if r["qty"] else 0
            mark = G if r["side"] == "BUY" else R
            print(f"  {mark}{B}{r['side']:<4} {r['ticker']:<7}{r['qty']:>7,} @ "
                  f"{r['limit']:>9.2f}{X}   {money(r['value']):>10}"
                  f"   {D}{clips} clip{'s' if clips != 1 else ''} of <={args.max_order:,}{X}")
        print(f"\n  {D}Book Trader: lightning icon ON, qty {args.max_order:,}, "
              f"shift+right-click to swipe a stack{X}")
        if args.bell:
            sys.stdout.write("\a")
    else:
        print(f"  {D}nothing above {args.min_edge:.3f} edge — wait{X}")
    sys.stdout.flush()


def main() -> None:
    p = argparse.ArgumentParser(description="Real-time RIT pricing desk (read-only)")
    p.add_argument("--case", choices=["fi2", "ev1", "auto"], default="auto")
    p.add_argument("--min-edge", type=float, default=0.02)
    p.add_argument("--max-order", type=int, default=0, help="0 = pick per case")
    p.add_argument("--interval", type=float, default=0.4)
    p.add_argument("--bell", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    c = RITClient()
    book, seen = EPSBook(), set()
    override = Path(__file__).resolve().parent.parent / "eps_override.json"

    while True:
        try:
            case = c.case()
            secs = {s["ticker"]: s for s in c.securities()}
            kind = args.case
            if kind == "auto":
                kind = "fi2" if (TB12M in secs or BOND in secs) else "ev1"
            if not args.max_order:
                args.max_order = 1_000 if kind == "fi2" else 10_000

            rows = (fi2_rows(c, case, secs, args) if kind == "fi2"
                    else ev1_rows(c, case, secs, args, book, seen, override))
            render(case, rows, float(c.trader().get("nlv", 0)), args)
        except RITError as exc:
            print(f"\n{R}API error:{X} {exc}")
        except KeyboardInterrupt:
            print("\nbye")
            return
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
