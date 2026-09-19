#!/usr/bin/env python3
"""
FI2 - Fixed Income 2 (TB6M, TB12M, 1YCP coupon bond)

Edge
----
The brief hands you the answer: "traders can solve the exact value of the bonds
at all times". Rates are known (7% then 9%, compounded weekly), so every
security has a closed-form price each tick. ANON liquidity traders push prices
around it at random. Profit = systematically trading against them, net of the
$0.02/bond commission.

Two independent money-makers, both implemented here:

  1. ABSOLUTE  - lift any ask below theoretical value, hit any bid above it.
                 This is the bread and butter and it is close to riskless.
  2. MAKER     - rest bids/asks a few cents either side of theoretical value and
                 let the liquidity traders' market orders come to you. Better
                 economics (no crossing the spread) but you only get filled when
                 someone wants the other side.

Plus a third, model-free check that runs as a monitor:

  3. REPLICATION - the bond's cash flows are exactly 0.05 x TB6M + 1.05 x TB12M,
                 so bond_dirty must equal that basket whatever the true rates
                 are. A gap here is arbitrage that does not depend on my rate
                 assumptions being right. Reported so you can trade the basket
                 by hand (it needs 3 legs and 2 order-size caps to line up).

Run:
    python bots/fi2_bond_arb.py --dry-run
    python bots/fi2_bond_arb.py --mm
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib import config
from ritlib.client import RITClient, RITError
from ritlib.microstructure import OrderBook, plan_execution
from ritlib.pricing import (
    BOND, BOND_COMMISSION, TB6M, TB12M, TICKS_PER_PERIOD,
    fi2_values, replication_fair_bond,
)

TICKERS = [TB6M, TB12M, BOND]


def plan_execution_chunks(quantity: int, max_order_size: int) -> list[int]:
    """Split a decided quantity into legal order sizes (FI2 caps at 1,000)."""
    out, qty = [], int(abs(quantity))
    while qty > 0:
        out.append(min(qty, max_order_size))
        qty -= out[-1]
    return out


class FI2Bot:
    def __init__(self, client: RITClient, args: argparse.Namespace):
        self.c = client
        self.a = args
        self.pending: list[int] = []          # marketable-limit ids to clean up
        self.quote_ids: list[int] = []        # resting maker quotes
        self.tick, self.period = 0, 1
        self.fwd_buy = self.fwd_sell = 0.0
        try:
            self.trader_id = str(client.trader().get("trader_id", "")) or None
        except RITError:
            self.trader_id = None
        self.last_quote_theo: dict[str, float] = {}
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        self.log_path = log_dir / (
            f"fi2_{datetime.now():%Y%m%d_%H%M%S}.log"
        )

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        with self.log_path.open("a") as fh:
            fh.write(line + "\n")

    # --------------------------------------------------------------- helpers
    def tradable(self, ticker: str, period: int) -> bool:
        return not (ticker == TB6M and period >= 2)

    def forward_theo(self, ticker: str, tick: int, period: int, side: str) -> float:
        """
        Theoretical value over the life of the order, not at the instant we send.

        Theo is not static between polls: bill values step UP by 12-17 cents at
        every compounding tick, and the bond's clean price drifts DOWN 1.6 cents
        every tick as interest accrues. Both moves are known in advance. Pricing
        a resting order at spot theo therefore hands a competitor a free,
        predictable pick-off on the same side every single time.

        So quote against the worst value theo can take while the order is live:
        the maximum for anything we are selling, the minimum for anything we are
        buying. Costs a fraction of a cent; removes the whole deterministic leak.
        """
        horizon = max(1, int(self.a.forward_ticks))
        values = [
            fi2_values(min(tick + k, TICKS_PER_PERIOD), period,
                       discrete=not self.a.continuous).theo(ticker)
            for k in range(horizon + 1)
        ]
        values = [v for v in values if v is not None]
        if not values:
            return 0.0
        return max(values) if side.upper() == "SELL" else min(values)

    def cancel_pending(self) -> None:
        """Only forget an order id once the cancel actually succeeded."""
        survivors = []
        for oid in self.pending:
            try:
                self.c.cancel(oid)
            except RITError as exc:
                # "already filled/cancelled" is fine; a network failure is not -
                # keep the id so we retry rather than orphaning a live order.
                if "not found" not in str(exc).lower() and "404" not in str(exc):
                    survivors.append(oid)
        self.pending = survivors

    def room(self, ticker: str, position: int, side: str) -> int:
        """
        Bonds we may still add on this side before hitting our own cap.
        Clamped at zero: once we are at or over the cap there is no room, and a
        negative number here used to be read as a quantity to trade.
        """
        cap = self.a.max_pos
        raw = cap - position if side == "BUY" else cap + position
        return max(0, raw)

    @staticmethod
    def available(level: dict) -> int:
        return int(level.get("quantity", 0)) - int(level.get("quantity_filled", 0))

    # ------------------------------------------------------- 1. absolute take
    def take(self, ticker: str, theo: float, position: int,
             tick: int = 0, period: int = 1) -> None:
        """
        Buy every unit the book will sell us at an average price that still
        clears theo + commission, and vice versa.

        The naive version of this sums only the levels priced better than the
        threshold. That leaves money behind: RIT fills a market order as a VWAP
        across levels, so paying through one level is fine as long as the
        BLENDED price stays profitable. max_qty_for_avg_price finds that
        maximum exactly.
        """
        self.fwd_buy = self.forward_theo(ticker, tick, period, "BUY")
        self.fwd_sell = self.forward_theo(ticker, tick, period, "SELL")
        try:
            book = OrderBook.from_api(self.c.book(ticker, limit=self.a.book_depth),
                                      ticker, exclude_trader=self.trader_id)
        except RITError as exc:
            self.log(f"book error {ticker}: {exc}")
            return

        buy_thresh = self.fwd_buy - BOND_COMMISSION - self.a.min_edge
        sell_thresh = self.fwd_sell + BOND_COMMISSION + self.a.min_edge

        for side, thresh, room in (
            ("BUY", buy_thresh, self.room(ticker, position, "BUY")),
            ("SELL", sell_thresh, self.room(ticker, position, "SELL")),
        ):
            chunks = plan_execution(book, side, room, thresh,
                                    self.a.max_order, self.a.min_clip)
            if not chunks:
                continue
            total = sum(chunks)
            est = book.walk(side, total)
            self.send(ticker, side, total, est.vwap, theo, est, len(chunks))
            return

    def send(self, ticker: str, action: str, qty: int, vwap: float, theo: float,
             est=None, n_orders: int = 1) -> None:
        edge = (theo - vwap) if action == "BUY" else (vwap - theo)
        gross = (edge - BOND_COMMISSION) * qty
        slip = f" slip {est.slippage:.4f}" if est else ""
        prefix = "[DRY] " if self.a.dry_run else ""
        self.log(f"{prefix}{action} {qty:>5} {ticker:<6} vwap {vwap:8.4f} "
                 f"theo {theo:8.4f} edge {edge:+.4f}{slip} "
                 f"in {n_orders} order(s) est ${gross:,.0f}")
        if self.a.dry_run:
            return
        # Marketable limit at the same threshold we sized against, priced off the
        # forward envelope. Keeping min_edge in the limit (it used to be dropped)
        # means any unfilled remainder rests at a price that is still profitable
        # a few ticks later, instead of at break-even that decays into a loss.
        limit = (self.fwd_buy - BOND_COMMISSION - self.a.min_edge) if action == "BUY" \
            else (self.fwd_sell + BOND_COMMISSION + self.a.min_edge)
        for chunk in plan_execution_chunks(qty, self.a.max_order):
            try:
                order = self.c.limit_order(ticker, action, chunk, limit)
                self.pending.append(order["order_id"])
            except RITError as exc:
                self.log(f"order rejected {ticker} {action} {chunk}: {exc}")
                return

    # ----------------------------------------------------------- 2. mm quotes
    def requote(self, values, positions: dict[str, int], period: int) -> None:
        if self.a.dry_run:
            return
        theos = {t: values.theo(t) for t in TICKERS if self.tradable(t, period)}
        if all(abs(theos[t] - self.last_quote_theo.get(t, -99)) < 0.005 for t in theos):
            return

        try:
            self.c.cancel_all()          # one call instead of six
            self.quote_ids.clear()
            self.pending.clear()
        except RITError as exc:
            self.log(f"bulk cancel failed: {exc}")

        size = min(self.a.mm_size, self.a.max_order)
        for ticker, theo in theos.items():
            edge = BOND_COMMISSION + self.a.mm_spread
            fwd_lo = self.forward_theo(ticker, self.tick, self.period, "BUY")
            fwd_hi = self.forward_theo(ticker, self.tick, self.period, "SELL")
            try:
                if self.room(ticker, positions.get(ticker, 0), "BUY") >= size:
                    o = self.c.limit_order(ticker, "BUY", size, fwd_lo - edge)
                    self.quote_ids.append(o["order_id"])
                if self.room(ticker, positions.get(ticker, 0), "SELL") >= size:
                    o = self.c.limit_order(ticker, "SELL", size, fwd_hi + edge)
                    self.quote_ids.append(o["order_id"])
            except RITError as exc:
                self.log(f"quote rejected {ticker}: {exc}")
        self.last_quote_theo = theos

    # ------------------------------------------------------ 3. replication chk
    def replication_check(self, secs: dict[str, dict], values, period: int) -> None:
        bond = secs.get(BOND, {})
        bill12 = secs.get(TB12M, {})
        bid6 = ask6 = None
        if period == 1:
            bill6 = secs.get(TB6M, {})
            bid6, ask6 = bill6.get("bid"), bill6.get("ask")
            if bid6 is None or ask6 is None:
                return
        if None in (bond.get("bid"), bond.get("ask"), bill12.get("bid"), bill12.get("ask")):
            return

        # Buy the bond, sell the basket: pay bond ask, receive basket bids.
        basket_bid = replication_fair_bond(bid6, bill12["bid"], values.tick, period)
        basket_ask = replication_fair_bond(ask6, bill12["ask"], values.tick, period)
        legs = 3 if period == 1 else 2
        cost = BOND_COMMISSION * (1 + 0.05 + 1.05 if period == 1 else 1 + 1.05)

        long_bond = basket_bid - bond["ask"] - cost
        short_bond = bond["bid"] - basket_ask - cost
        if long_bond > self.a.min_edge:
            self.log(f"REPLICATION: BUY bond @{bond['ask']:.2f} / SELL basket "
                     f"({legs} legs) -> +${long_bond:.3f}/bond")
        elif short_bond > self.a.min_edge:
            self.log(f"REPLICATION: SELL bond @{bond['bid']:.2f} / BUY basket "
                     f"({legs} legs) -> +${short_bond:.3f}/bond")

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        self.log(f"FI2 bot | {'DRY RUN' if self.a.dry_run else 'LIVE'} | {config.describe()}")
        self.log(f"max_pos={self.a.max_pos} max_order={self.a.max_order} "
                 f"min_edge=${self.a.min_edge} commission=${BOND_COMMISSION}")
        self.c.wait_for_start()
        self.log("case ACTIVE")

        while True:
            try:
                case = self.c.case()
            except RITError as exc:
                self.log(f"case error: {exc}")
                time.sleep(0.5)
                continue
            status = case.get("status")
            if status == "STOPPED":
                self.log("case STOPPED - shutting down")
                break
            if status != "ACTIVE":
                # PAUSED happens routinely mid-competition. Sit out, do not exit.
                self.cancel_pending()
                time.sleep(0.5)
                continue

            tick, period = int(case["tick"]), int(case["period"])

            # Never clamp an unexpected clock. min(tick, 312) would silently pin
            # weeks_remaining to zero, making every security look worth par -
            # and the bot would buy the entire book at 99.98. Halt instead.
            tpp = int(case.get("ticks_per_period", TICKS_PER_PERIOD))
            if tpp != TICKS_PER_PERIOD or not (0 <= tick <= TICKS_PER_PERIOD) or period not in (1, 2):
                self.log(f"HALT: unexpected clock ticks_per_period={tpp} tick={tick} "
                         f"period={period} - pricing model assumes {TICKS_PER_PERIOD} "
                         f"ticks/period over 2 periods. Cancelling and stopping.")
                self.cancel_pending()
                try:
                    self.c.cancel_all()
                except RITError:
                    pass
                break

            self.tick, self.period = tick, period
            values = fi2_values(tick, period, discrete=not self.a.continuous)

            try:
                secs = {s["ticker"]: s for s in self.c.securities()}
            except RITError as exc:
                self.log(f"securities error: {exc}")
                time.sleep(0.25)
                continue
            except Exception as exc:                       # noqa: BLE001
                # Never die with live orders resting in the book.
                self.log(f"UNEXPECTED {exc!r} - cancelling and continuing")
                self.cancel_pending()
                time.sleep(0.3)
                continue
            positions = {t: int(secs.get(t, {}).get("position", 0)) for t in TICKERS}

            for ticker in TICKERS:
                if not self.tradable(ticker, period):
                    continue
                theo = values.theo(ticker)
                if theo is None:
                    continue

                # Only pull the full book when the TOUCH already shows an edge.
                # This is exact, not an approximation: levels behind the touch
                # are strictly worse, so if the best ask is not cheap enough,
                # nothing deeper can be either. Cuts the common loop from 5 API
                # calls to 2, which buys back far more opportunities than
                # throttling the request rate costs us.
                s_row = secs.get(ticker, {})
                bid, ask = s_row.get("bid"), s_row.get("ask")
                gate = BOND_COMMISSION + self.a.min_edge
                cheap = ask is not None and ask < self.forward_theo(ticker, tick, period, "BUY") - gate
                rich = bid is not None and bid > self.forward_theo(ticker, tick, period, "SELL") + gate
                if not (cheap or rich):
                    continue

                self.take(ticker, theo, positions[ticker], tick, period)

            if self.a.mm:
                self.requote(values, positions, period)
            if self.a.replication:
                self.replication_check(secs, values, period)

            if self.a.verbose:
                row = []
                for t in TICKERS:
                    if not self.tradable(t, period):
                        continue
                    s, theo = secs.get(t, {}), values.theo(t)
                    bid, ask = s.get("bid"), s.get("ask")
                    mid = (bid + ask) / 2 if bid and ask else float("nan")
                    row.append(f"{t}: theo={theo:7.3f} mkt={mid:7.3f} "
                               f"edge={theo - mid:+.3f} pos={positions[t]:+5d}")
                self.log(f"P{period} t={tick:>3} | " + " | ".join(row))

            # Cancel before sleeping, not after waking: a residual limit that
            # survives the sleep is exposed across a compounding step.
            self.cancel_pending()
            time.sleep(self.a.interval)

        self.cancel_pending()
        try:
            self.log(f"NLV: {self.c.trader().get('nlv')}")
        except RITError:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="FI2 coupon bond arbitrage bot")
    p.add_argument("--live", action="store_true", help="send real orders (overrides RIT_DRY_RUN)")
    p.add_argument("--dry-run", action="store_true", help="force dry run")
    p.add_argument("--max-pos", type=int, default=config.setting("FI2_MAX_POS", 5_000, int),
                   help="per-security cap; raise once tools/doctor.py shows the real limits")
    p.add_argument("--max-order", type=int, default=config.setting("FI2_MAX_ORDER", 1_000, int),
                   help="case maximum is 1,000 bonds per order")
    p.add_argument("--min-clip", type=int, default=50)
    p.add_argument("--min-edge", type=float, default=config.setting("FI2_MIN_EDGE", 0.02, float),
                   help="$ edge required on top of the 2c commission")
    p.add_argument("--book-depth", type=int, default=20)
    p.add_argument("--mm", action="store_true", default=config.setting("FI2_MM", False, bool))
    p.add_argument("--mm-size", type=int, default=config.setting("FI2_MM_SIZE", 500, int))
    p.add_argument("--mm-spread", type=float, default=config.setting("FI2_MM_SPREAD", 0.05, float))
    p.add_argument("--no-replication", dest="replication", action="store_false",
                   help="suppress the model-free bond-vs-bills arbitrage report")
    p.add_argument("--forward-ticks", type=int, default=3,
                   help="ticks of theo drift to price resting orders against")
    p.add_argument("--continuous", action="store_true",
                   help="smooth discounting instead of the true weekly step")
    p.add_argument("--interval", type=float, default=config.setting("RIT_INTERVAL", 0.25, float))
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    args.dry_run = True if args.dry_run else (
        False if args.live else config.setting("RIT_DRY_RUN", True, bool))

    client = RITClient()
    bot = FI2Bot(client, args)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.log("interrupted")
    finally:
        # Whatever happened, do not leave orders resting in the book.
        try:
            client.cancel_all()
            bot.log("all orders cancelled")
        except RITError as exc:
            bot.log(f"FINAL CANCEL FAILED - check the blotter by hand: {exc}")


if __name__ == "__main__":
    main()
