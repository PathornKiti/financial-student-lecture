#!/usr/bin/env python3
"""
EV1 - Equity Valuation 1 (Prandium Industries, ticker PI)

Edge
----
Terminal value is a formula: FV = (Q1+Q2+Q3+Q4 realised EPS) x 12.5. Everyone in
the room has it. The edge is latency and execution discipline:

  1. VALUATION  - re-price the instant a news item lands (~4 polls/second).
  2. EXECUTION  - size to what the book can actually supply at a profitable
                  price, instead of firing a market order that walks four
                  levels deep and gives the edge back as slippage.
  3. DISCIPLINE - PI settles at fair value, so a position bought below FV is
                  already a winner. Hold it. Only flip when price overshoots to
                  the other side. Churning at 1c/share is how people lose this.

Why microprice, not mid
-----------------------
RIT market orders walk the book (Rotman's own example: a 5,000-share buy fills
across four price levels). The arithmetic mid ignores that a 10,000-share bid
against a 500-share ask means the next print is almost certainly at the ask.
`OrderBook.microprice` weights by size and is a better read of where the market
really is. We measure edge against it, and we size against the book itself.

Run:
    python tools/doctor.py                      # check setup first
    python bots/ev1_equity_valuation.py         # honours RIT_DRY_RUN in .env
    python bots/ev1_equity_valuation.py --live --verbose
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib import config
from ritlib.client import RITClient, RITError
from ritlib.microstructure import Level, OrderBook, Tape, plan_execution
from ritlib.news import apply_news, load_overrides
from ritlib.pricing import COMP_PE, EPSBook, EV1_FEE, EV1_TICKER


class EV1Bot:
    def __init__(self, client: RITClient, args: argparse.Namespace):
        self.c = client
        self.a = args
        self.book = EPSBook()
        self.seen_news: set[int] = set()
        self.last_fv = self.book.fair_value
        self.last_quote_fv: float | None = None
        self.halted = False               # set when an earnings item fails to parse
        self.last_news_id = 0             # so we only pull NEW news
        self.sent_delta = 0               # fills not yet visible in the API position
        self.has_resting = False          # did we leave orders in the book?
        try:
            self.trader_id = str(client.trader().get("trader_id", "")) or None
        except RITError:
            self.trader_id = None
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        self.log_path = log_dir / f"ev1_{datetime.now():%Y%m%d_%H%M%S}.log"
        self.override_path = Path(__file__).resolve().parent.parent / "eps_override.json"

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        with self.log_path.open("a") as fh:
            fh.write(line + "\n")

    # ------------------------------------------------------------- valuation
    def refresh_valuation(self) -> None:
        try:
            # Only fetch what we have not seen. Re-downloading 50 items four
            # times a second is what pushes this bot into rate limiting, and a
            # throttled bot has no latency edge left to trade on.
            items = self.c.news(since=self.last_news_id or None, limit=20)
        except RITError as exc:
            self.log(f"news error: {exc}")
            items = []

        for item in items:
            nid = item.get("news_id")
            if isinstance(nid, int):
                self.last_news_id = max(self.last_news_id, nid)

        logs, unparsed = apply_news(self.book, items, self.seen_news)
        for line in logs:
            self.log(line)
        for line in load_overrides(self.override_path, self.book):
            self.log(line)
            self.halted = False           # a manual override clears the halt

        if unparsed and not self.halted:
            # An earnings item we could not read means our fair value is stale
            # while the rest of the room has already repriced. Trading a stale
            # FV at full size is the most expensive thing this bot can do, so
            # stop taking risk until a human resolves it.
            self.halted = True
            self.log("!" * 70)
            for h in unparsed:
                self.log(f"HALTED - could not parse earnings item: {h}")
            self.log("HALTED - fix with eps_override.json, e.g. {\"2\": [0.28, true]}")
            self.log("!" * 70)

        fv = self.book.fair_value
        if abs(fv - self.last_fv) > 1e-9:
            self.log(f"*** FAIR VALUE {self.last_fv:.2f} -> {fv:.2f} | {self.book.summary()}")
            self.last_fv = fv

    # ---------------------------------------------------------------- signal
    def direction(self, fv: float, book: OrderBook, position: int) -> str | None:
        """
        'BUY', 'SELL' or None (hold). Measured against the microprice so a
        lopsided book does not fool us into thinking there is an edge.

        The hold case is deliberate: "fairly priced" must never mean "go flat".
        """
        # No microprice guard here on purpose. A taker pays the ask and receives
        # the bid, so those are the only prices that matter - and requiring a
        # two-sided book would make us sit out the instant after a big headline,
        # when one side momentarily empties and the opportunity is largest.
        #
        # Reversing the book costs twice the fee plus the spread, so demand a
        # much larger edge to flip sign than to add to an existing position.
        flip = self.a.min_edge * self.a.flip_multiple

        if book.best_ask is not None:
            need = flip if position < 0 else self.a.min_edge
            if book.best_ask < fv - EV1_FEE - need:
                return "BUY"
        if book.best_bid is not None:
            need = flip if position > 0 else self.a.min_edge
            if book.best_bid > fv + EV1_FEE + need:
                return "SELL"
        return None

    def size_cap(self) -> int:
        """
        Position cap scaled by how much of the terminal value is actually known.

        Fair value is 12.5x the sum of four quarterly EPS figures. While a
        quarter is still an estimate it carries roughly +/-0.04 of surprise, so
        with k quarters unreported the standard deviation of fair value is
        about 12.5 * 0.04 * sqrt(k):

            0 actual -> ~$1.00     3 actual -> ~$0.50

        Taking 100,000 shares for a $0.25 edge against a $1.00 sigma is a 0.25:1
        bet on the next earnings surprise, not an arbitrage. So we start at 35%
        of the limit and scale to full size as quarters settle.
        """
        floor = self.a.confidence_floor
        return int(self.a.max_pos * (floor + (1.0 - floor) * self.book.confidence()))

    def fv_sigma(self) -> float:
        """1 sigma of fair value, in dollars, given how many quarters are actual."""
        return COMP_PE * self.a.eps_sigma * math.sqrt(max(0, 4 - self.book.n_actual))

    def desired_position(self, fv: float, book: OrderBook, side: str, position: int) -> int:
        """Scale with the mispricing, then ratchet (never trim a good side)."""
        ref = book.best_ask if side == "BUY" else book.best_bid
        # The break-even is fv - fee when buying but fv + fee when selling. Using
        # abs() here overstated every short's edge by 2 cents.
        edge = (fv - EV1_FEE - ref) if side == "BUY" else (ref - fv - EV1_FEE)
        if edge <= 0:
            return position

        # Require more edge per share when fair value itself is uncertain.
        full = max(self.a.full_edge, 0.25 * self.fv_sigma())
        scaled = int(min(1.0, edge / full) * self.size_cap())
        if side == "BUY":
            return max(scaled, position)
        return min(-scaled, position)

    # ------------------------------------------------------------- execution
    def execute(self, side: str, fv: float, book: OrderBook, position: int, target: int) -> None:
        """
        Size to the book, then send PROTECTED orders.

        Market orders here were unsafe: the quantity is computed from one book
        snapshot and then sent as up to a dozen sequential orders, so slices
        four onward fill wherever the book has moved - and nothing at the
        exchange enforces the break-even we calculated. A marketable limit at
        that break-even executes immediately against everything better and
        simply does not fill the rest. It cannot fill us at a loss.
        """
        delta = target - position
        if (side == "BUY" and delta <= 0) or (side == "SELL" and delta >= 0):
            return

        # Hard clamp against the case position limit, counting fills the API has
        # not shown us yet, so a lagging position read cannot breach the limit.
        effective = position + self.sent_delta
        headroom = (self.a.max_pos - effective) if side == "BUY" else (self.a.max_pos + effective)
        if headroom <= 0:
            return
        delta = min(abs(delta), headroom)

        # Do not turn the book over in one go on a marginal edge; a fair-value
        # revision is exempt because that is exactly when speed matters.
        if not self.fv_changed_this_loop:
            delta = min(delta, int(self.a.max_turnover * self.a.max_pos))

        worst_avg = (fv - EV1_FEE - self.a.min_edge) if side == "BUY" \
            else (fv + EV1_FEE + self.a.min_edge)

        chunks = plan_execution(book, side, delta, worst_avg,
                                self.a.max_order, self.a.min_clip)
        if not chunks:
            return

        total = sum(chunks)
        est = book.walk(side, total)
        gross = (abs(fv - est.vwap) - EV1_FEE) * total
        self.log(f"{'[DRY] ' if self.a.dry_run else ''}{side} {total:,} {EV1_TICKER} "
                 f"in {len(chunks)} order(s) @ lim {worst_avg:.2f} | vwap~{est.vwap:.3f} "
                 f"slip {est.slippage:.3f} | FV {fv:.2f} sigma {self.fv_sigma():.2f} "
                 f"cap {self.size_cap():,} | pos {position:,} -> "
                 f"{position + (total if side == 'BUY' else -total):,} | est ${gross:,.0f}")
        if self.a.dry_run:
            return
        for chunk in chunks:
            try:
                self.c.limit_order(EV1_TICKER, side, chunk, worst_avg)
                self.sent_delta += chunk if side == "BUY" else -chunk
                self.has_resting = True
            except RITError as exc:
                self.log(f"order rejected: {exc}")
                return

    def requote(self, fv: float, book: OrderBook, position: int) -> None:
        """
        Passive quotes around FV, skewed by book imbalance: when resting size is
        stacked on the bid the next print is likely at the ask, so we widen the
        side we are about to get run over on.
        """
        if self.a.dry_run:
            return
        if self.last_quote_fv is not None and abs(fv - self.last_quote_fv) < 0.01:
            return
        try:
            self.c.cancel_all(EV1_TICKER)
        except RITError:
            pass

        imb = book.imbalance() or 0.0
        skew = imb * self.a.mm_spread * 0.5
        size = min(self.a.mm_size, self.a.max_order)
        try:
            if self.a.max_pos - position >= size:
                self.c.limit_order(EV1_TICKER, "BUY", size, fv - self.a.mm_spread + skew)
            if self.a.max_pos + position >= size:
                self.c.limit_order(EV1_TICKER, "SELL", size, fv + self.a.mm_spread + skew)
            self.last_quote_fv = fv
        except RITError as exc:
            self.log(f"quote rejected: {exc}")

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        self.log(f"EV1 bot | {'DRY RUN' if self.a.dry_run else 'LIVE'} | {config.describe()}")
        self.log(f"max_pos={self.a.max_pos:,} full_edge=${self.a.full_edge} "
                 f"min_edge=${self.a.min_edge} | {self.book.summary()}")
        self.c.wait_for_start()
        self.log("case ACTIVE")
        self.fv_changed_this_loop = False

        while True:
            try:
                case = self.c.case()
                status = case.get("status")
                if status == "STOPPED":
                    self.log("case STOPPED - shutting down")
                    break
                if status != "ACTIVE":
                    # PAUSED is routine mid-competition. Sit out, do not exit.
                    time.sleep(0.5)
                    continue

                prev_fv = self.book.fair_value
                self.refresh_valuation()
                fv = self.book.fair_value
                self.fv_changed_this_loop = abs(fv - prev_fv) > 1e-9

                if self.halted:
                    try:
                        self.c.cancel_all(EV1_TICKER)
                    except RITError:
                        pass
                    time.sleep(self.a.interval)
                    continue

                # Unfilled remainders rest at a break-even computed from an FV
                # that may be about to move, so clear them - but only when we
                # actually sent something last loop.
                if self.has_resting and not self.a.dry_run:
                    try:
                        self.c.cancel_all(EV1_TICKER)
                    except RITError:
                        pass
                    self.has_resting = False

                raw_book = self.c.book(EV1_TICKER, limit=self.a.book_depth)
                book = OrderBook.from_api(raw_book, EV1_TICKER, exclude_trader=self.trader_id)
                position = self.c.position(EV1_TICKER)
                self.sent_delta = 0          # API has caught up; reset the tracker

                side = self.direction(fv, book, position)
                if side:
                    target = self.desired_position(fv, book, side, position)
                    self.execute(side, fv, book, position, target)

                if self.a.mm:
                    self.requote(fv, book, position)

                if self.a.verbose and book.mid is not None:
                    imb = book.imbalance()
                    imb_txt = f"imb={imb:+.2f}" if imb is not None else "imb=n/a"
                    self.log(
                        f"t={case['tick']:>3} FV={fv:6.2f} "
                        f"{book.best_bid:.2f}x{book.best_ask:.2f} "
                        f"micro={book.microprice:6.3f} "
                        f"edge={fv - book.microprice:+.3f} {imb_txt} "
                        f"depth={book.depth('BID')}/{book.depth('ASK')} "
                        f"pos={position:>8,} {side or 'hold'}"
                    )
            except RITError as exc:
                self.log(f"api error: {exc}")
                time.sleep(0.5)
                continue
            except Exception as exc:                       # noqa: BLE001
                # Dying here would leave 100,000 shares on with no bot running.
                self.log(f"UNEXPECTED {exc!r} - continuing")
                time.sleep(0.3)
                continue
            time.sleep(self.a.interval)

        self.log(f"final: {self.book.summary()}")
        try:
            self.log(f"NLV: ${float(self.c.trader().get('nlv', 0)):,.2f}")
        except RITError:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="EV1 equity valuation bot")
    p.add_argument("--live", action="store_true", help="send real orders (overrides RIT_DRY_RUN)")
    p.add_argument("--dry-run", action="store_true", help="force dry run")
    p.add_argument("--max-pos", type=int, default=config.setting("EV1_MAX_POS", 100_000, int))
    p.add_argument("--max-order", type=int, default=config.setting("EV1_MAX_ORDER", 10_000, int))
    p.add_argument("--min-clip", type=int, default=1_000)
    p.add_argument("--min-edge", type=float, default=config.setting("EV1_MIN_EDGE", 0.03, float))
    p.add_argument("--full-edge", type=float, default=config.setting("EV1_FULL_EDGE", 0.25, float))
    p.add_argument("--book-depth", type=int, default=20)
    p.add_argument("--flip-multiple", type=float, default=3.0,
                   help="how many times min_edge is needed to reverse position sign")
    p.add_argument("--max-turnover", type=float, default=0.35,
                   help="fraction of max_pos tradable per loop absent an FV change")
    p.add_argument("--confidence-floor", type=float,
                   default=config.setting("EV1_CONFIDENCE_FLOOR", 0.35, float),
                   help="fraction of max_pos usable when NO quarter has reported "
                        "(1.0 = full size on pure estimates, the old behaviour)")
    p.add_argument("--eps-sigma", type=float, default=0.04,
                   help="assumed 1-sigma surprise per unreported quarter")
    p.add_argument("--mm", action="store_true", default=config.setting("EV1_MM", False, bool))
    p.add_argument("--mm-size", type=int, default=config.setting("EV1_MM_SIZE", 2_500, int))
    p.add_argument("--mm-spread", type=float, default=config.setting("EV1_MM_SPREAD", 0.10, float))
    p.add_argument("--interval", type=float, default=config.setting("RIT_INTERVAL", 0.25, float))
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    args.dry_run = True if args.dry_run else (
        False if args.live else config.setting("RIT_DRY_RUN", True, bool))

    client = RITClient()
    bot = EV1Bot(client, args)
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.log("interrupted")
    finally:
        if not args.dry_run:
            try:
                client.cancel_all(EV1_TICKER)
                bot.log("resting orders cancelled (position left as-is - PI "
                        "settles at fair value, so flat is usually wrong)")
            except RITError as exc:
                bot.log(f"FINAL CANCEL FAILED - check the blotter by hand: {exc}")


if __name__ == "__main__":
    main()
