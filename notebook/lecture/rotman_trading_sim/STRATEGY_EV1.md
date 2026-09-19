# EV1 — Equity Valuation 1 (Prandium Industries, ticker PI)

## The case in numbers

| | |
|---|---|
| Security | PI, one stock |
| Length | 1 trading year over **8 minutes** |
| Valuation | `P = (Q1 + Q2 + Q3 + Q4 realised EPS) × 12.5` |
| Last year actuals | 0.32, 0.18, 0.20, 0.25 → sum 0.95 → $11.875 |
| Opening estimates | 0.40, 0.24, 0.27, 0.33 → sum 1.24 → **$15.50** |
| Position limit | **±100,000 shares** net |
| Fee | **$0.01 per share** |

Earnings are released at the end of each quarter; analyst estimate revisions arrive
intermittently throughout.

## The edge

Everyone in the room has the same formula. Nobody has an information advantage. The edge
is entirely **reaction time and discipline**:

- A headline prints. Fair value moves by `surprise × 12.5`.
- A $0.05 EPS surprise = **$0.625** of fair value = **$62,500** at full size.
- The traders who are still reading the headline are your counterparty for the next few
  seconds.

`bots/ev1_equity_valuation.py` polls `/v1/news` about four times a second, re-prices, and
takes size before the mid moves.

## The three ways people lose this case

**1. Churn.** This is the big one. At $0.01/share, a full-size round trip costs **$2,000**.
A naive bot that targets "flat when price ≈ fair" will buy 40,000 and sell it back
minutes later on noise, over and over, and hand its entire edge to the exchange.

PI **settles at fair value**. A position bought below FV is already profitable — you do not
need to trade out of it. So the rule this bot uses:

> Buy when the ask is below FV. Hold. Only sell when the **bid goes above FV** — i.e. the
> price overshot to the other side. "Fairly priced" means *hold*, never *flatten*.

It also ratchets: while the buy signal is live it only adds to a long, never trims it, so
noise in the mid cannot shake it out of a good position.

**2. The anchor trap.** Last year's EPS summed to 0.95 → $11.875. Some traders anchor
there and sell a stock that is genuinely worth $15.50. That anchor is not information; it
is the counterparty you want. Trade against it.

**3. Confusing an estimate with an actual.** An analyst revision is a *guess* about a
quarter that has not reported. An earnings release is a *fact* that can never change. Once
a quarter is actual, later estimates for it are noise — `EPSBook` ignores them, and
`confidence()` rises as quarters settle. As more quarters become actual, your fair value
is progressively more certain, so size up as the case progresses.

## Sizing

Target position scales with the edge: `--full-edge` dollars of mispricing (default $0.25)
justifies the full ±100,000. `--min-edge` (default $0.03, on top of the 1c fee) is the
minimum before the bot acts at all.

Going into the close, being **max long when the stock is below FV** is the right position —
it settles at FV. Do not flatten "to be safe"; flat is how you score zero.

## Optional: market making (`--mm`)

Quote two-sided around FV (default ±$0.10) and collect the spread from impatient traders.
Adds P&L on top of the directional edge, but it means resting orders — if a headline drops
while you are quoting the wrong side, you get run over. The bot cancels and requotes
whenever FV moves by more than a cent. Only enable this after you are comfortable with the
base strategy.

## The bot halts rather than guess

If a news item mentions earnings but the parser extracts no EPS, the bot **stops taking
risk** and logs `HALTED` every loop until you resolve it. This is deliberate: the
alternative is trading a stale fair value at full size while everyone else has repriced,
which is the single most expensive thing it can do. A review found that a missed Q2
actual could put 100,000 shares on the wrong side — a six-figure loss. Halting turns that
into a few idle seconds.

Write `eps_override.json` to clear it (below).

## If the news parser misses a headline

Do not debug a regex with four minutes left. Write `eps_override.json` next to the bot:

```json
{"2": [0.28, false], "3": [0.31, true]}
```

`quarter: [eps, is_actual]`. It is re-read every loop and overrides everything, so the
correction takes effect in under a second. The bot logs `[override] Q2 -> 0.28E` to confirm.

Any news item the parser cannot read is logged as `NO EPS PARSED` with the headline — watch
for those lines.

## Running it

```bash
python tools/monitor.py --case ev1                     # read-only, always legal
python bots/ev1_equity_valuation.py --dry-run --verbose
python bots/ev1_equity_valuation.py --verbose
python bots/ev1_equity_valuation.py --mm --mm-size 2500
```

Ctrl-C offers to flatten your position.
