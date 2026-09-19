# FI2 — Fixed Income 2 (Coupon Govt Bonds)

## The case in numbers

| | |
|---|---|
| Securities | TB6M, TB12M (bills, $100 face), 1YCP (10% semi-annual coupon bond, $100 face) |
| Periods | 2 × 312 ticks (~5 min each), each = 6 months calendar |
| Risk-free | 7% p.a. period 1 → 9% p.a. period 2, compounded **weekly** (every 12 ticks) |
| Weekly rate | 0.1302% (P1), 0.1659% (P2) |
| Endowment | $1,000,000 |
| Commission | **$0.02 per bond**, every transaction |
| Max order | **1,000 bonds** per order |
| Settlement | Bills close out at $100 at the end of their period; bond pays $5 then $105 |

## The edge

The brief says it in plain text: *"traders can solve the exact value of the bonds at all
times."* There is no interest rate risk, no default risk, no liquidity risk. This is not a
trading case in the usual sense — it is a **speed-of-arithmetic** case. ANON liquidity
traders submit orders normally distributed around mid, and you take the tails.

Opening values: TB6M **96.6736**, TB12M **92.5966**, bond clean **102.0601**.

## Three trades, in priority order

**1. Absolute value (the bread and butter).** Lift any ask below theo − $0.02, hit any bid
above theo + $0.02. Near-riskless: the security converges to a known payoff. This is what
`bots/fi2_bond_arb.py` does on every loop, and it is where most of the money is.

**2. Passive quoting (`--mm`).** Rest bids/asks a few cents either side of theo and let the
liquidity traders' market orders come to you. Better economics — you earn the spread
instead of paying it — but you only trade when someone wants the other side. Run it
alongside the taker, not instead of it.

**3. Replication (model-free).** The bond's cash flows are *exactly*:

```
1YCP  =  0.05 × TB6M  +  1.05 × TB12M
```

$5 = 0.05 × $100 at the end of P1; $105 = 1.05 × $100 at the end of P2. So the bond's
**dirty** price must equal that basket whatever the true rates are. If your rate
assumptions were somehow wrong, this trade still works. Cost: 3 legs × $0.02 and all three
must fill — the bot reports these rather than legging in automatically, because a
half-filled basket is a naked position. Take them by hand when the gap is wide.

## Things that will cost you money

**Clean vs dirty.** RIT quotes the bond **clean**. You pay clean + accrued. Accrued =
`(tick / 312) × $5`, so it runs from $0 to ~$4.98 across a period and resets. Compare book
prices to `bond_clean`, never to dirty. Getting this backwards means you think the bond is
$5 cheap at the end of a period and you buy garbage.

**The staircase.** Interest is credited every 12 ticks, so fair value is **flat then
jumps** — it does not drift smoothly. Price it continuously and you will think the bond is
going cheap right before each compounding tick. The bot uses the discrete step by default
(`--continuous` exists only for comparison).

**The commission is bigger than it looks.** $0.02/bond on a security worth ~$100 is 2bp.
Your typical mispricing is a few cents. A 1-cent edge is a **loss**. `--min-edge` defaults
to $0.02 on top of commission for that reason — do not lower it below $0.01.

**Shorting the bond is not free.** Short at settlement and you pay the $5 coupon and the
$105 principal. The theoretical value already accounts for this; just do not be surprised
by the cash flows in your blotter.

## The period transition

At the end of period 1, three things happen at once:

1. TB6M matures and closes out at $100 — any position settles, and it stops trading.
2. The bond pays its $5 coupon; accrued resets to zero and clean jumps back up.
3. The discount rate changes from 7% to 9%, so **every value re-bases**.

Opening period 2: TB12M **95.7826**, bond clean **100.5718**. Expect other traders to be
briefly confused here. The bot handles it automatically (it reads `period` from
`/v1/case`); if you are trading manually, have the period-2 half of
`reference/fi2_fair_values.md` in front of you before the bell.

## Position sizing

The brief does not state a position limit — check `/v1/limits` on the day and raise
`--max-pos` (default 5,000 per security, deliberately conservative) once you have seen the
real numbers. With max order size 1,000, building a large position takes many orders, so
start early rather than trying to size up in the last minute.

Because the payoff is certain, inventory is not really risk here — it is locked-in profit
waiting to settle. Do not panic out of a position just because you are "big".

## Running it

```bash
python tools/fi2_price_table.py                  # print the answer key
python tools/monitor.py --case fi2               # read-only, always legal
python bots/fi2_bond_arb.py --dry-run --verbose  # watch first
python bots/fi2_bond_arb.py --mm --verbose       # live, taker + maker
```

Useful flags: `--min-edge` (raise it if fills are going against you), `--max-pos`,
`--max-order` (case cap is 1,000), `--interval` (raise it if you see HTTP 429).
