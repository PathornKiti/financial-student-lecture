"""
Exact fair values for the two cases.

FI2 (Fixed Income 2 - Coupon Govt Bonds)
----------------------------------------
There is NO uncertainty in FI2: the risk-free rate is known in advance, so every
security has a closed-form value at every tick. Profit = buying below that value
or selling above it, net of the $0.02/bond commission.

Securities
    TB6M   $100 at the end of period 1
    TB12M  $100 at the end of period 2
    1YCP   $5 at the end of period 1, $105 at the end of period 2   (quoted CLEAN)

Rates: 7% p.a. in period 1, 9% p.a. in period 2, compounded WEEKLY - i.e. cash
interest is credited every 12 ticks, 26 times per 312-tick period. Because the
credit is discrete, the theoretical value is a step function: it is flat between
compounding ticks. `discrete=True` (the default) reproduces that exactly.

EV1 (Equity Valuation 1)
------------------------
Terminal value of PI = (Q1 + Q2 + Q3 + Q4 realised EPS) * 12.5
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ----------------------------------------------------------------- FI2 config
TICKS_PER_PERIOD = 312
COMPOUND_TICKS = 12                      # interest paid every 12 seconds
WEEKS_PER_PERIOD = TICKS_PER_PERIOD // COMPOUND_TICKS      # 26
ANNUAL_RATE = {1: 0.07, 2: 0.09}         # per case brief
COUPON = 5.0                             # 10% semi-annual on $100 face
FACE = 100.0
BOND_COMMISSION = 0.02                   # $ per bond, per transaction

TB6M, TB12M, BOND = "TB6M", "TB12M", "1YCP"


def weekly_rate(period: int) -> float:
    """(1 + annual)^(1/52) - 1, matching the 0.1302% / 0.1659% in the brief."""
    return (1.0 + ANNUAL_RATE[period]) ** (1.0 / 52.0) - 1.0


def weeks_remaining(tick: int, discrete: bool = True) -> float:
    """Compounding intervals left in the current period."""
    remaining_ticks = max(TICKS_PER_PERIOD - tick, 0)
    w = remaining_ticks / COMPOUND_TICKS
    return math.ceil(w) if discrete else w


def discount(tick: int, period: int, discrete: bool = True) -> float:
    """Discount factor from the END of `period` back to `tick` inside it."""
    n = weeks_remaining(tick, discrete)
    return 1.0 / (1.0 + weekly_rate(period)) ** n


def period2_full_discount() -> float:
    """Discount factor across the whole of period 2 = 1 / 1.09^0.5."""
    return 1.0 / (1.0 + weekly_rate(2)) ** WEEKS_PER_PERIOD


def accrued_interest(tick: int) -> float:
    """(time since last coupon / time between coupons) * coupon, per the brief."""
    return (min(max(tick, 0), TICKS_PER_PERIOD) / TICKS_PER_PERIOD) * COUPON


@dataclass
class FI2Values:
    tick: int
    period: int
    tb6m: float | None       # None once it has matured
    tb12m: float
    bond_dirty: float
    bond_clean: float        # this is what RIT quotes and what you compare to the book
    accrued: float

    def theo(self, ticker: str) -> float | None:
        return {TB6M: self.tb6m, TB12M: self.tb12m, BOND: self.bond_clean}[ticker]

    def as_dict(self) -> dict:
        return {TB6M: self.tb6m, TB12M: self.tb12m, BOND: self.bond_clean}


def fi2_values(tick: int, period: int, discrete: bool = True) -> FI2Values:
    """Theoretical value of every FI2 security at (period, tick)."""
    acc = accrued_interest(tick)

    if period == 1:
        df1 = discount(tick, 1, discrete)          # to end of period 1
        df2 = df1 * period2_full_discount()        # to end of period 2
        tb6m = FACE * df1
        tb12m = FACE * df2
        dirty = COUPON * df1 + (FACE + COUPON) * df2
    else:
        df2 = discount(tick, 2, discrete)
        tb6m = None                                 # matured at the end of period 1
        tb12m = FACE * df2
        dirty = (FACE + COUPON) * df2

    return FI2Values(
        tick=tick, period=period,
        tb6m=tb6m, tb12m=tb12m,
        bond_dirty=dirty, bond_clean=dirty - acc, accrued=acc,
    )


def replication_fair_bond(tb6m_price: float | None, tb12m_price: float, tick: int,
                          period: int) -> float:
    """
    Model-free cross-check. The bond's cash flows are exactly replicated by
        0.05 x TB6M  +  1.05 x TB12M
    so the bond's DIRTY price must equal that basket's price whatever the true
    discount rates are. Returns the implied CLEAN price.

    Use this to trade the bond against the bills: if the bond is cheap relative
    to the basket you are arbitraging, not forecasting. In period 2 the TB6M leg
    is gone and the relationship collapses to bond_dirty = 1.05 x TB12M.
    """
    if period == 1:
        if tb6m_price is None:
            raise ValueError("TB6M price required in period 1")
        dirty = 0.05 * tb6m_price + 1.05 * tb12m_price
    else:
        dirty = 1.05 * tb12m_price
    return dirty - accrued_interest(tick)


# ----------------------------------------------------------------- EV1 config
COMP_PE = 12.5
EV1_TICKER = "PI"
EV1_FEE = 0.01                   # $ per share
EV1_POSITION_LIMIT = 100_000     # net long or short

LAST_YEAR_EPS = {1: 0.32, 2: 0.18, 3: 0.20, 4: 0.25}     # 0.95 -> $11.875
INITIAL_ESTIMATES = {1: 0.40, 2: 0.24, 3: 0.27, 4: 0.33}  # 1.24 -> $15.50


@dataclass
class EPSBook:
    """Running record of the four quarters: estimates until an actual lands."""
    eps: dict[int, float] = field(default_factory=lambda: dict(INITIAL_ESTIMATES))
    actual: dict[int, bool] = field(default_factory=lambda: {q: False for q in (1, 2, 3, 4)})

    # Last year ran 0.18-0.32 and estimates 0.24-0.40, so a real figure lives
    # well inside this band. Anything outside it is a parser accident, and acting
    # on it would mean a maximum-size position on a fictional fair value.
    EPS_FLOOR, EPS_CEILING = -0.50, 1.00

    def update(self, quarter: int, value: float, is_actual: bool) -> bool:
        """Returns True if this changed our view. Actuals are never overwritten."""
        if quarter not in (1, 2, 3, 4):
            return False
        if not (self.EPS_FLOOR <= value <= self.EPS_CEILING):
            return False
        if self.actual[quarter] and not is_actual:
            return False                      # ignore estimates for a settled quarter
        changed = (abs(self.eps[quarter] - value) > 1e-9) or (is_actual and not self.actual[quarter])
        self.eps[quarter] = value
        if is_actual:
            self.actual[quarter] = True
        return changed

    @property
    def total(self) -> float:
        return sum(self.eps.values())

    @property
    def fair_value(self) -> float:
        return self.total * COMP_PE

    @property
    def n_actual(self) -> int:
        return sum(self.actual.values())

    def confidence(self) -> float:
        """0 -> all estimates, 1 -> all four quarters reported. Scale size with this."""
        return self.n_actual / 4.0

    def summary(self) -> str:
        parts = [f"Q{q}={self.eps[q]:.2f}{'A' if self.actual[q] else 'E'}" for q in (1, 2, 3, 4)]
        return f"{' '.join(parts)} | sum={self.total:.2f} | FV=${self.fair_value:.2f}"
