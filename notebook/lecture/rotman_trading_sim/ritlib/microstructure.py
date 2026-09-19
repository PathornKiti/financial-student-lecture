"""
Order-book, tape and liquidity analytics for RIT.

Grounded in how the RIT matching engine actually behaves, per Rotman's own
tutorials:

  * One central limit order book per security. Bids sort descending, asks
    ascending, so the best prices sit at the top.
  * A market order "transacts at the current best bids or asks, taking into
    account the liquidity of the market" - it WALKS the book across price
    levels. Rotman's worked example: a 5,000-share market buy fills 700 @ 25.54,
    1500 @ 25.55, 2100 @ 25.63, 700 @ 25.74. Your fill is a VWAP, not the touch.
  * A limit order that crosses the spread executes immediately instead of
    resting.
  * Computer participants are labelled ANON in the book. You cannot tell an
    informed ANON from an uninformed one by its label.
  * Rotman defines liquidity as "how close the bids and asks are, and the volume
    available at each price level" - i.e. spread AND depth, which is what
    `liquidity_score` measures.

Everything here is pure computation on API payloads. Nothing sends orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# --------------------------------------------------------------------- levels
@dataclass
class Level:
    price: float
    quantity: int
    quantity_filled: int = 0
    trader_id: str = ""

    @property
    def available(self) -> int:
        """Unfilled size. RIT leaves partially-filled orders in the book."""
        return max(int(self.quantity) - int(self.quantity_filled), 0)


@dataclass
class FillEstimate:
    """What a market/marketable order of this size would actually cost."""
    requested: int
    filled: int
    vwap: float
    worst_price: float
    levels_consumed: int
    touch: float                  # best price before we started

    @property
    def complete(self) -> bool:
        return self.filled >= self.requested

    @property
    def slippage(self) -> float:
        """$/share paid worse than the touch. Always >= 0."""
        if not self.filled:
            return 0.0
        return abs(self.vwap - self.touch)

    @property
    def notional(self) -> float:
        return self.filled * self.vwap

    def __str__(self) -> str:
        return (f"{self.filled}/{self.requested} @ vwap {self.vwap:.4f} "
                f"(touch {self.touch:.4f}, slip {self.slippage:.4f}, "
                f"{self.levels_consumed} levels)")


# ----------------------------------------------------------------- order book
@dataclass
class OrderBook:
    ticker: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict, ticker: str = "",
                 exclude_trader: str | None = None) -> "OrderBook":
        """
        `exclude_trader` drops your own resting orders. Leaving them in makes the
        book look deeper than it is, and - worse - lets the bot detect its own
        stale quote as a trading opportunity and cross with itself, paying
        commission both ways for zero economics.
        """
        def build(rows, reverse):
            levels = [
                Level(
                    price=float(r["price"]),
                    quantity=int(r.get("quantity", 0)),
                    quantity_filled=int(r.get("quantity_filled", 0)),
                    trader_id=str(r.get("trader_id", "")),
                )
                for r in rows or [] if r.get("price") is not None
            ]
            levels = [l for l in levels if l.available > 0]
            if exclude_trader:
                levels = [l for l in levels if l.trader_id != exclude_trader]
            levels.sort(key=lambda l: l.price, reverse=reverse)
            return levels

        return cls(
            ticker=ticker or payload.get("ticker", ""),
            bids=build(payload.get("bids"), reverse=True),    # highest first
            asks=build(payload.get("asks"), reverse=False),   # lowest first
        )

    # ------------------------------------------------------------- top of book
    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> int:
        return sum(l.available for l in self.bids if l.price == self.best_bid) if self.bids else 0

    @property
    def best_ask_size(self) -> int:
        return sum(l.available for l in self.asks if l.price == self.best_ask) if self.asks else 0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def microprice(self) -> float | None:
        """
        Size-weighted mid. A bid of 10,000 against an ask of 500 means the next
        trade is far more likely to happen at the ask, so true value sits nearer
        the ask than the arithmetic mid says.

            micro = (bid*ask_size + ask*bid_size) / (bid_size + ask_size)

        Note the cross-multiplication: heavy size on the BID pulls the estimate
        UP toward the ask. Use this instead of mid when measuring an edge.
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        bs, as_ = self.best_bid_size, self.best_ask_size
        if bs + as_ == 0:
            return self.mid
        return (self.best_bid * as_ + self.best_ask * bs) / (bs + as_)

    # ------------------------------------------------------------------ depth
    def depth(self, side: str, levels: int | None = None) -> int:
        """Total unfilled size resting on one side, optionally top-N levels."""
        book = self.bids if side.upper() in ("BUY", "BID") else self.asks
        book = book[:levels] if levels else book
        return sum(l.available for l in book)

    def imbalance(self, levels: int = 5) -> float | None:
        """
        Order book imbalance in [-1, +1].

            +1  all resting size is on the bid  -> buying pressure
            -1  all resting size is on the ask  -> selling pressure

        In RIT this is a short-horizon pressure gauge, not a fair-value signal.
        Its real use is TIMING: when you already know you want to buy, waiting
        for imbalance to turn positive gets you a better fill; buying into a
        heavily negative book means walking into sellers.
        """
        b, a = self.depth("BID", levels), self.depth("ASK", levels)
        if b + a == 0:
            return None
        return (b - a) / (b + a)

    def anon_share(self, side: str, levels: int = 5) -> float | None:
        """
        Fraction of resting size posted by ANON (the computer participants).
        A book that is nearly all ANON is a book of noise - safe to trade
        against. A book thick with named traders means humans are competing for
        the same edge you are.
        """
        book = (self.bids if side.upper() in ("BUY", "BID") else self.asks)[:levels]
        total = sum(l.available for l in book)
        if not total:
            return None
        return sum(l.available for l in book if l.trader_id.upper() == "ANON") / total

    # --------------------------------------------------------------- walking
    def walk(self, side: str, quantity: int) -> FillEstimate:
        """
        Simulate a market order of `quantity`, exactly as RIT would fill it.

        side="BUY" consumes asks, side="SELL" consumes bids. This is THE
        function for sizing: it tells you the VWAP you would really get rather
        than the touch price you see.
        """
        buying = side.upper() == "BUY"
        book = self.asks if buying else self.bids
        touch = (self.best_ask if buying else self.best_bid) or 0.0

        filled, notional, levels_used, worst = 0, 0.0, 0, touch
        for level in book:
            if filled >= quantity:
                break
            take = min(quantity - filled, level.available)
            if take <= 0:
                continue
            filled += take
            notional += take * level.price
            worst = level.price
            levels_used += 1

        vwap = notional / filled if filled else 0.0
        return FillEstimate(quantity, filled, vwap, worst, levels_used, touch)

    def qty_at_or_better(self, side: str, limit_price: float) -> int:
        """
        How much can I trade without a price worse than `limit_price`?

        side="BUY"  -> total ask size priced <= limit_price
        side="SELL" -> total bid size priced >= limit_price

        This is the correct way to size a value trade: it answers "how much of
        this mispricing actually exists" instead of guessing.
        """
        if side.upper() == "BUY":
            return sum(l.available for l in self.asks if l.price <= limit_price + 1e-9)
        return sum(l.available for l in self.bids if l.price >= limit_price - 1e-9)

    def max_qty_for_avg_price(self, side: str, target_avg: float) -> int:
        """
        Largest order whose *average* fill price still beats `target_avg`.

        Less conservative than qty_at_or_better: it lets you pay through a level
        or two as long as the blended price keeps the trade profitable. Walking
        two levels deep at a worse price is fine if the average still clears
        your fair value - this finds that maximum.
        """
        buying = side.upper() == "BUY"
        book = self.asks if buying else self.bids
        best_qty, filled, notional = 0, 0, 0.0
        for level in book:
            take = level.available
            # Binary-search inside the level for the exact break-even quantity.
            lo, hi = 0, take
            while lo < hi:
                trial = (lo + hi + 1) // 2
                avg = (notional + trial * level.price) / (filled + trial)
                ok = avg <= target_avg + 1e-12 if buying else avg >= target_avg - 1e-12
                if ok:
                    lo = trial
                else:
                    hi = trial - 1
            if lo:
                best_qty = filled + lo
            if lo < take:            # this level broke the average - stop
                break
            filled += take
            notional += take * level.price
            best_qty = filled
        return best_qty

    # ------------------------------------------------------------- liquidity
    def liquidity_score(self, reference_size: int, tick_size: float = 0.01) -> float | None:
        """
        0 (illiquid) to 1 (deep and tight), using Rotman's own definition of
        liquidity: how tight the spread is AND how much size rests at each level.

        `reference_size` is the order size you actually care about trading.
        """
        if self.spread is None or self.mid is None or self.mid <= 0:
            return None
        tightness = 1.0 / (1.0 + max(self.spread / tick_size - 1.0, 0.0))
        est = self.walk("BUY", reference_size)
        if not est.filled:
            return 0.0
        fill_ratio = est.filled / reference_size
        impact = 1.0 / (1.0 + est.slippage / tick_size)
        return round(tightness * 0.3 + fill_ratio * 0.4 + impact * 0.3, 4)

    def summary(self, reference_size: int = 1000) -> str:
        if self.mid is None:
            return f"{self.ticker}: no two-sided market"
        imb = self.imbalance()
        parts = [
            f"{self.ticker}: {self.best_bid:.2f}x{self.best_ask:.2f}",
            f"spread {self.spread:.2f}",
            f"mid {self.mid:.3f}",
            f"micro {self.microprice:.3f}",
            f"depth {self.depth('BID')}/{self.depth('ASK')}",
        ]
        if imb is not None:
            parts.append(f"imb {imb:+.2f}")
        parts.append(f"liq {self.liquidity_score(reference_size)}")
        return "  ".join(parts)


# ----------------------------------------------------------------------- tape
@dataclass
class Tape:
    """Time & sales analytics - what actually traded, versus what is resting."""
    trades: list[dict] = field(default_factory=list)

    @classmethod
    def from_api(cls, rows: list[dict]) -> "Tape":
        clean = [r for r in (rows or []) if r.get("price") is not None]
        clean.sort(key=lambda r: (r.get("tick", 0), r.get("id", 0)))
        return cls(clean)

    @property
    def volume(self) -> int:
        return sum(int(t.get("quantity", 0)) for t in self.trades)

    @property
    def vwap(self) -> float | None:
        vol = self.volume
        if not vol:
            return None
        return sum(float(t["price"]) * int(t.get("quantity", 0)) for t in self.trades) / vol

    @property
    def last_price(self) -> float | None:
        return float(self.trades[-1]["price"]) if self.trades else None

    def signed_volume(self) -> int:
        """
        Tick-rule order flow (Lee-Ready). A trade printing above the previous
        print is classified buyer-initiated, below it seller-initiated, equal
        prints inherit the last sign.

        Positive = net buying pressure hitting the book. Persistent one-sided
        flow is the footprint of someone working a large order - in RIT that is
        usually an institution unwinding a tender, and it is the thing most
        likely to run your resting quote over.
        """
        signed, last_sign, prev = 0, 1, None
        for t in self.trades:
            price, qty = float(t["price"]), int(t.get("quantity", 0))
            if prev is None or price == prev:
                sign = last_sign
            else:
                sign = 1 if price > prev else -1
            signed += sign * qty
            last_sign, prev = sign, price
        return signed

    def flow_ratio(self) -> float | None:
        """signed_volume / volume, in [-1, +1]. Direction of aggression."""
        vol = self.volume
        return None if not vol else self.signed_volume() / vol

    def realized_vol(self) -> float | None:
        """Stdev of tick-to-tick log returns - how jumpy this security is."""
        prices = [float(t["price"]) for t in self.trades if float(t["price"]) > 0]
        if len(prices) < 3:
            return None
        rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    def since_tick(self, tick: int) -> "Tape":
        return Tape([t for t in self.trades if int(t.get("tick", 0)) >= tick])


# ---------------------------------------------------------------- execution
def plan_execution(book: OrderBook, side: str, desired: int, worst_avg_price: float,
                   max_order_size: int, min_clip: int = 1) -> list[int]:
    """
    Turn "I want `desired` units, but not at a worse average than
    `worst_avg_price`" into a list of legal order sizes.

    Two caps are applied before slicing:
      1. What the book can actually supply at an acceptable average price.
      2. RIT's per-order maximum (1,000 bonds in FI2, typically 10,000 shares
         in equity cases) - oversized orders are rejected outright.

    Returns [] when the trade is not worth doing.
    """
    # A negative `desired` means the caller has no room left on this side - it is
    # already at or beyond its cap. abs() would turn that into a BUY, letting an
    # over-limit position grow without bound. Refuse instead.
    if desired <= 0:
        return []
    affordable = book.max_qty_for_avg_price(side, worst_avg_price)
    qty = min(desired, affordable)
    if qty < min_clip:
        return []
    chunks = []
    while qty > 0:
        chunk = min(qty, max_order_size)
        chunks.append(chunk)
        qty -= chunk
    return chunks
