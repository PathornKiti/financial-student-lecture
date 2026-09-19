#!/usr/bin/env python3
"""
Run a bot over complete simulated cases, many seeds, and report statistics.

IMPORTANT - what this can and cannot tell you:

  CAN: does the bot crash, halt spuriously, breach a position limit, or leave
       orders resting? Is it profitable against random order flow oscillating
       around a known fair value - which IS the mechanism of both real cases?
       How sensitive is it to its parameters?

  CANNOT: predict your competition score. The simulator's counterparties are
       noise, not people; its fills are more generous than a real book; and it
       cannot model competitors racing you to the same mispricing.

Timing note: the bot's edge depends on how many decision loops it gets per tick
of case time. Real case = 1 tick/second with a 0.25s loop = 4 loops/tick. This
harness preserves that ratio by scaling the bot's interval with the sim speed,
so a 20x-speed run is time-compressed but not advantaged or handicapped.

    python tools/backtest.py --case fi2 --runs 8
    python tools/backtest.py --case ev1 --runs 8 --extra "--confidence-floor 1.0"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOTS = {"fi2": "bots/fi2_bond_arb.py", "ev1": "bots/ev1_equity_valuation.py"}
TICKS = {"fi2": 624, "ev1": 480}           # 2x312, and 1x480
START_CASH = 1_000_000.0
REAL_LOOPS_PER_TICK = 4.0                  # 1 tick/s with a 0.25s bot loop


def api(port: int, path: str, timeout: float = 3.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1{path}", timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            api(port, "/case", timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.2)
    return False


def one_run(case: str, seed: int, port: int, speed: float, extra: list[str],
            verbose: bool, mock_args: dict | None = None) -> dict:
    mock_args = mock_args or {}
    """Start mock + bot, let the case run to completion, collect the result."""
    interval = 1.0 / (speed * REAL_LOOPS_PER_TICK)

    mock = subprocess.Popen(
        [sys.executable, "mock/mock_rit_server.py", "--case", case,
         "--seed", str(seed), "--speed", str(speed), "--port", str(port)]
        + (["--news-gap"] if mock_args.get("news_gap") else [])
        + ["--noise", str(mock_args.get("noise", 1.0))]
        + ["--spread", str(mock_args.get("spread", 1.0))],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not wait_ready(port):
        mock.kill()
        return {"seed": seed, "error": "mock never came up"}

    env = dict(os.environ,
               RIT_URL=f"http://127.0.0.1:{port}/v1",
               RIT_API_KEY="backtest", RIT_DRY_RUN="false")
    log = ROOT / "logs" / f"bt_{case}_{seed}.log"
    with log.open("w") as fh:
        bot = subprocess.Popen(
            [sys.executable, BOTS[case], "--live", "--interval", f"{interval:.4f}", *extra],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT,
        )

        peak_pos, nlv = 0, START_CASH
        deadline = time.time() + TICKS[case] / speed + 30
        status = "?"
        while time.time() < deadline:
            try:
                status = api(port, "/case")["status"]
                nlv = float(api(port, "/trader")["nlv"])
                peak_pos = max(peak_pos,
                               max(abs(int(s["position"])) for s in api(port, "/securities")))
            except Exception:
                pass
            if status == "STOPPED":
                break
            time.sleep(0.3)

        time.sleep(1.0)                                  # let the bot settle/cancel
        try:
            nlv = float(api(port, "/trader")["nlv"])
            resting = len(api(port, "/orders?status=OPEN"))
        except Exception:
            resting = -1
        bot.terminate()
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.kill()
    mock.terminate()
    try:
        mock.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mock.kill()

    text = log.read_text()
    return {
        "seed": seed,
        "nlv": nlv,
        "pnl": nlv - START_CASH,
        "orders": len(re.findall(r"order\(s\)|^\d\d:\d\d:\d\d (?:BUY|SELL)", text, re.M)),
        "peak_pos": peak_pos,
        "halts": len(re.findall(r"HALT", text)),
        "crashes": len(re.findall(r"UNEXPECTED", text)),
        "rejects": len(re.findall(r"rejected", text)),
        "resting": resting,
        "status": status,
        "log": str(log),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest a RIT bot over many seeds")
    p.add_argument("--case", choices=["fi2", "ev1"], required=True)
    p.add_argument("--runs", type=int, default=8)
    p.add_argument("--speed", type=float, default=20.0)
    p.add_argument("--port", type=int, default=9990)
    p.add_argument("--first-seed", type=int, default=1)
    p.add_argument("--extra", default="", help="extra args passed through to the bot")
    p.add_argument("--label", default="")
    p.add_argument("--news-gap", action="store_true",
                   help="EV1: price gaps on news instead of drifting (realistic)")
    p.add_argument("--noise", type=float, default=1.0,
                   help="scale the mispricing ANON creates (<1 = tighter market)")
    p.add_argument("--spread", type=float, default=1.0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    extra = args.extra.split() if args.extra else []
    (ROOT / "logs").mkdir(exist_ok=True)
    label = args.label or args.case.upper()
    print(f"\n{label}: {args.runs} full cases at {args.speed}x "
          f"(bot interval {1.0/(args.speed*REAL_LOOPS_PER_TICK):.4f}s "
          f"= {REAL_LOOPS_PER_TICK:.0f} loops/tick, as in the real case)")
    if extra:
        print(f"extra args: {' '.join(extra)}")
    print("-" * 78)
    print(f"{'seed':>5}{'P&L':>12}{'orders':>8}{'peak pos':>10}"
          f"{'halt':>6}{'crash':>7}{'rej':>5}{'rest':>6}{'status':>9}")

    results = []
    for i in range(args.runs):
        seed = args.first_seed + i
        r = one_run(args.case, seed, args.port + i, args.speed, extra, args.verbose,
                    {"news_gap": args.news_gap, "noise": args.noise,
                     "spread": args.spread})
        if "error" in r:
            print(f"{seed:>5}  {r['error']}")
            continue
        results.append(r)
        print(f"{r['seed']:>5}{r['pnl']:>+12,.0f}{r['orders']:>8}{r['peak_pos']:>10,}"
              f"{r['halts']:>6}{r['crashes']:>7}{r['rejects']:>5}"
              f"{r['resting']:>6}{r['status']:>9}")

    if not results:
        print("\nno completed runs")
        return

    pnl = [r["pnl"] for r in results]
    print("-" * 78)
    print(f"  runs            {len(pnl)}")
    print(f"  mean P&L        {statistics.mean(pnl):+,.0f}")
    print(f"  median P&L      {statistics.median(pnl):+,.0f}")
    if len(pnl) > 1:
        print(f"  stdev           {statistics.stdev(pnl):,.0f}")
    print(f"  best / worst    {max(pnl):+,.0f} / {min(pnl):+,.0f}")
    print(f"  profitable      {sum(1 for x in pnl if x > 0)}/{len(pnl)}")
    print(f"  peak position   {max(r['peak_pos'] for r in results):,}")
    print(f"  crashes         {sum(r['crashes'] for r in results)}")
    print(f"  halts           {sum(r['halts'] for r in results)}")
    print(f"  orders rejected {sum(r['rejects'] for r in results)}")
    print(f"  orders left in book at shutdown "
          f"{sum(r['resting'] for r in results if r['resting'] > 0)}")
    print()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# READ THIS BEFORE QUOTING ANY NUMBER THIS TOOL PRINTS
#
# The simulator flatters both bots in three specific ways:
#
# 1. The book is REGENERATED on every request, with fresh size at every level
#    one cent apart around the mid. Liquidity is therefore effectively
#    unlimited and always near the touch. A real book runs out.
#
# 2. Without --news-gap, the EV1 price DRIFTS toward a new fair value over
#    ~25 ticks. That hands the bot a head start no competitive room would ever
#    give it. Always pass --news-gap for a realistic EV1 read; it cut measured
#    P&L by roughly 4x and turned the worst case negative.
#
# 3. There are no competitors. Nobody else is racing you to the same
#    mispricing, so every edge the bot sees, it gets.
#
# Use this tool to answer "does it behave correctly and is the mechanism
# sound", never "how much will I make".
# ---------------------------------------------------------------------------
