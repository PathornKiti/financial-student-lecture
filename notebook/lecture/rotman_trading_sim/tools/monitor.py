#!/usr/bin/env python3
"""
Read-only live dashboard: theoretical value vs the market, refreshed in place.

Use this when you are trading by hand - either because the competition bans
API order entry, or as the backup if a bot misbehaves and you kill it. It sends
no orders, so it is always safe to leave running.

    python tools/monitor.py --case fi2
    python tools/monitor.py --case ev1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib import config
from ritlib.client import RITClient, RITError
from ritlib.microstructure import OrderBook, Tape
from ritlib.news import apply_news, load_overrides
from ritlib.pricing import (
    BOND, BOND_COMMISSION, EPSBook, EV1_FEE, EV1_TICKER, TB6M, TB12M,
    TICKS_PER_PERIOD, fi2_values, replication_fair_bond,
)

CLEAR = "\033[2J\033[H"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def colour(edge: float, threshold: float) -> str:
    if edge > threshold:
        return GREEN
    if edge < -threshold:
        return RED
    return DIM


def fi2_screen(c: RITClient, args) -> None:
    case = c.case()
    tick, period = int(case["tick"]), int(case["period"])
    v = fi2_values(min(tick, TICKS_PER_PERIOD), period)
    secs = {s["ticker"]: s for s in c.securities()}

    print(CLEAR, end="")
    print(f"{BOLD}FI2{RESET}  period {period}/2   tick {tick}/{TICKS_PER_PERIOD}   "
          f"{case['status']}   accrued ${v.accrued:.3f}")
    print(f"{DIM}commission ${BOND_COMMISSION}/bond - an edge under that is not a trade{RESET}\n")
    print(f"{'SEC':<7}{'THEO':>9}{'BID':>8}{'ASK':>8}{'MICRO':>9}"
          f"{'BUY ED':>8}{'SELL ED':>9}{'QTY':>7}{'DEPTH':>12}{'POS':>7}  ACTION")

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

        buy_edge = theo - ask - BOND_COMMISSION          # profit if we lift the ask
        sell_edge = bid - theo - BOND_COMMISSION         # profit if we hit the bid

        # How much is actually THERE at a price that still makes money?
        qty, depth_txt, micro = 0, "-", (bid + ask) / 2
        if ob is not None and ob.mid is not None:
            micro = ob.microprice
            depth_txt = f"{ob.depth('BID')}/{ob.depth('ASK')}"
            if buy_edge > args.min_edge:
                qty = ob.max_qty_for_avg_price("BUY", theo - BOND_COMMISSION - args.min_edge)
            elif sell_edge > args.min_edge:
                qty = ob.max_qty_for_avg_price("SELL", theo + BOND_COMMISSION + args.min_edge)

        action = ""
        if buy_edge > args.min_edge:
            action = f"{GREEN}BUY {qty} -> ${buy_edge * qty:,.0f}{RESET}"
        elif sell_edge > args.min_edge:
            action = f"{RED}SELL {qty} -> ${sell_edge * qty:,.0f}{RESET}"

        print(f"{ticker:<7}{theo:>9.3f}{bid:>8.2f}{ask:>8.2f}{micro:>9.3f}"
              f"{colour(buy_edge, args.min_edge)}{buy_edge:>+8.3f}{RESET}"
              f"{colour(sell_edge, args.min_edge)}{sell_edge:>+9.3f}{RESET}"
              f"{qty:>7}{depth_txt:>12}{int(s.get('position', 0)):>7}  {action}")

    # Model-free cross-check
    b, b12 = secs.get(BOND), secs.get(TB12M)
    if b and b12 and b.get("bid") and b12.get("bid"):
        try:
            b6 = secs.get(TB6M)
            bid6 = b6.get("bid") if period == 1 else None
            ask6 = b6.get("ask") if period == 1 else None
            basket_bid = replication_fair_bond(bid6, b12["bid"], tick, period)
            basket_ask = replication_fair_bond(ask6, b12["ask"], tick, period)
            legs = "0.05xTB6M + 1.05xTB12M" if period == 1 else "1.05xTB12M"
            print(f"\n{BOLD}Replication{RESET} (bond = {legs}) "
                  f"basket {basket_bid:.3f}/{basket_ask:.3f}  bond {b['bid']:.2f}/{b['ask']:.2f}")
            long_bond = basket_bid - b["ask"] - BOND_COMMISSION * 2.1
            short_bond = b["bid"] - basket_ask - BOND_COMMISSION * 2.1
            if long_bond > args.min_edge:
                print(f"  {GREEN}BUY bond / SELL basket  -> +${long_bond:.3f}/bond{RESET}")
            elif short_bond > args.min_edge:
                print(f"  {RED}SELL bond / BUY basket  -> +${short_bond:.3f}/bond{RESET}")
        except (ValueError, TypeError, KeyError):
            pass

    print(f"\nNLV  ${c.trader().get('nlv', 0):,.2f}")


def ev1_screen(c: RITClient, args, book: EPSBook, seen: set[int], override: Path) -> None:
    case = c.case()
    for line in apply_news(book, c.news(limit=50), seen):
        pass                                    # state is what we render below
    load_overrides(override, book)

    s = c.sec(EV1_TICKER)
    bid, ask = s.get("bid"), s.get("ask")
    fv = book.fair_value
    pos = int(s.get("position", 0))

    print(CLEAR, end="")
    print(f"{BOLD}EV1{RESET}  tick {case['tick']}/{case['ticks_per_period']}   {case['status']}")
    print(f"{DIM}fee ${EV1_FEE}/share | override file: {override.name}{RESET}\n")

    for q in (1, 2, 3, 4):
        tag = f"{GREEN}ACTUAL{RESET}" if book.actual[q] else f"{DIM}est{RESET}"
        print(f"  Q{q}  {book.eps[q]:>5.2f}  {tag}")
    print(f"\n  sum EPS {book.total:.2f}  x 12.5  =  {BOLD}FV ${fv:.2f}{RESET}"
          f"   ({book.n_actual}/4 quarters reported)")

    if bid and ask:
        try:
            ob = OrderBook.from_api(c.book(EV1_TICKER, limit=20), EV1_TICKER)
        except RITError:
            ob = None
        micro = ob.microprice if (ob and ob.microprice) else (bid + ask) / 2
        buy_edge = fv - ask - EV1_FEE
        sell_edge = bid - fv - EV1_FEE

        print(f"\n  market {bid:.2f} / {ask:.2f}   micro {micro:.3f}   "
              f"edge {colour(fv - micro, args.min_edge)}{fv - micro:+.3f}{RESET}")
        if ob is not None and ob.mid is not None:
            imb = ob.imbalance()
            print(f"  {DIM}depth {ob.depth('BID'):,} bid / {ob.depth('ASK'):,} ask"
                  + (f"   imbalance {imb:+.2f}" if imb is not None else "")
                  + f"   spread {ob.spread:.2f}{RESET}")

        if buy_edge > args.min_edge:
            qty = ob.max_qty_for_avg_price("BUY", fv - EV1_FEE - args.min_edge) if ob else 0
            qty = min(qty, args.max_pos - pos)
            est = ob.walk("BUY", qty) if (ob and qty) else None
            print(f"  {GREEN}BUY {qty:,} shares  (${buy_edge:.3f}/share at the touch"
                  + (f", vwap {est.vwap:.3f}" if est else "")
                  + f")  -> ${buy_edge * max(qty,0):,.0f}{RESET}")
        elif sell_edge > args.min_edge:
            qty = ob.max_qty_for_avg_price("SELL", fv + EV1_FEE + args.min_edge) if ob else 0
            qty = min(qty, args.max_pos + pos)
            est = ob.walk("SELL", qty) if (ob and qty) else None
            print(f"  {RED}SELL {qty:,} shares  (${sell_edge:.3f}/share at the touch"
                  + (f", vwap {est.vwap:.3f}" if est else "")
                  + f")  -> ${sell_edge * max(qty,0):,.0f}{RESET}")
        else:
            print(f"  {DIM}fairly priced - hold your position, do not round-trip it{RESET}")

    print(f"\n  position {pos:+,} / {args.max_pos:,}      NLV ${c.trader().get('nlv', 0):,.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only RIT dashboard (sends no orders)")
    p.add_argument("--case", choices=["fi2", "ev1"], required=True)
    p.add_argument("--min-edge", type=float, default=0.02)
    p.add_argument("--max-pos", type=int, default=100_000)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--once", action="store_true", help="render one frame and exit")
    args = p.parse_args()

    c = RITClient()
    book, seen = EPSBook(), set()
    override = Path(__file__).resolve().parent.parent / "eps_override.json"

    while True:
        try:
            if args.case == "fi2":
                fi2_screen(c, args)
            else:
                ev1_screen(c, args, book, seen, override)
        except RITError as exc:
            print(f"\n{RED}API error: {exc}{RESET}")
        except KeyboardInterrupt:
            print("\nbye")
            return
        sys.stdout.flush()          # frames are lost otherwise when piped to a file
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
