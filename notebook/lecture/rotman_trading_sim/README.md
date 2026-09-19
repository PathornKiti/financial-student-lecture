# RIT Competition Kit — FI2 (Coupon Govt Bonds) & EV1 (Equity Valuation)

Everything needed to trade both cases: exact pricing models, two bots, a read-only
dashboard for manual trading, a printable answer key, and an offline simulator so you
can rehearse tonight without the RIT client.

**New to RIT? Read [GETTING_STARTED.md](GETTING_STARTED.md) first** — what the
software is, the screens, the click shortcuts, and setup in ten minutes.

```
.env.example     copy to .env, fill in ONE line (your API key)
ritlib/          config.py (.env loader)      · client.py (REST wrapper)
                 pricing.py (fair values)     · news.py (EPS parser)
                 microstructure.py (book, liquidity, tape, execution sizing)
bots/            fi2_bond_arb.py · ev1_equity_valuation.py
tools/           doctor.py (run this first)   · monitor.py (read-only dashboard)
                 fi2_price_table.py (answer key)
mock/            mock_rit_server.py (offline RIT simulator)
notebooks/       rit_competition_prep.ipynb
reference/       fi2_fair_values.csv / .md — print this
```

## Three commands

```bash
cp .env.example .env     # then put your API key in it
python tools/doctor.py   # checks everything, tells you what to fix
python bots/fi2_bond_arb.py --verbose    # dry-run by default; --live to trade
```

---

## Read this first

**1. Check whether API trading is allowed.** Rotman competitions differ: some events
encourage algorithmic entry, others require manual clicking and treat a bot as a
disqualification. Confirm with the organiser before you run anything that sends orders.
If bots are banned, `tools/monitor.py` is still fully legal — it only reads.

**2. The RIT client is Windows-only.** You are on macOS. You cannot run the real client
here, which is exactly why `mock/mock_rit_server.py` exists: it serves the same REST
endpoints with a simulated market so you can prove your setup works before you sit down
at the competition machine.

**3. Verify the API before you trust it.** Field names below match the published RIT REST
API v1, but builds vary. First thing on the competition machine:

```bash
python ritlib/client.py        # dumps raw /case /trader /limits /securities /news
```

If a field name differs, you will see it immediately instead of during the case.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

In the RIT client, the API controls are on the **bottom bar**, not in a menu. Click the
**API** icon (bottom-right, next to *RTD* and *API Orders*) to open the settings and the
key. Put that key in `.env`:

```ini
RIT_API_KEY=your-key-here
```

That is the only line you must fill in. `RIT_HOST`/`RIT_PORT` default to
`localhost:9999`, which is correct for a normal setup — **the REST API is served by
the RIT client on your own machine**, not by a Rotman server. Everything else in
`.env` is an optional tuning knob, and `.env` is gitignored.

Then verify:

```bash
python tools/doctor.py
```

## Rehearse tonight (no RIT client needed)

```bash
# terminal 1 — simulated market, 6x speed
python mock/mock_rit_server.py --case fi2 --speed 6

# terminal 2 — watch the bot think without sending orders
python bots/fi2_bond_arb.py --dry-run --verbose

# then let it trade the simulator
python bots/fi2_bond_arb.py --verbose
```

Same for EV1 (`--case ev1`). The simulator scripts analyst revisions and earnings
releases so you can watch the news parser fire.

> The mock is for debugging **logic**, not for predicting your score. Its fills are
> generous and its "other traders" are noise, not people.

## On the day

```bash
python ritlib/client.py                          # verify connection + field names
python tools/fi2_price_table.py                  # regenerate the answer key, print it
python tools/monitor.py --case fi2               # read-only dashboard (always safe)
python bots/fi2_bond_arb.py --dry-run --verbose  # watch for 30s before going live
python bots/fi2_bond_arb.py                      # live
```

Always start `--dry-run`. Ctrl-C stops any bot; the FI2 bot cancels its resting orders on
the way out, and the EV1 bot offers to flatten.

---

## How to win, in one page

**FI2 is an arithmetic race, not a forecasting problem.** The brief tells you rates in
advance, so every price is knowable for every tick of both periods — that is what
`reference/fi2_fair_values.csv` is. You are not predicting anything. You are collecting
the difference between random ANON orders and a number you already have. The only way to
lose is to trade an edge smaller than the $0.02/bond commission, so the bot requires
`commission + min_edge` before it acts.

**EV1 is a latency race.** Everyone has the same formula (EPS × 12.5). The money is in the
seconds between a headline printing and the room finishing reading it. The bot re-prices
on every `/v1/news` poll and takes size immediately.

**Both bots size to the book, not to a target.** A RIT market order walks the book — a
5,000-share buy in Rotman's own worked example filled across four price levels at an
average 7 cents worse than the screen price. So before trading, the bots ask
`max_qty_for_avg_price`: *what is the largest order whose blended VWAP still clears fair
value?* That is the number they trade. It is the difference between capturing a
mispricing and paying it back as slippage.

**The mistake that costs most people the EV1 case is churn.** At $0.01/share a full-size
round trip costs $2,000. PI settles at fair value, so a position bought below FV is
already a winner — you do not need to sell it back when the price returns to fair. This
bot holds and only flips when the price overshoots to the other side.

Detailed playbooks: [STRATEGY_FI2.md](STRATEGY_FI2.md) · [STRATEGY_EV1.md](STRATEGY_EV1.md)

---

## Safety behaviour (added after a quant review of both bots)

| Behaviour | Why |
|---|---|
| **Dry-run by default** | `RIT_DRY_RUN=true`. Only `--live` sends orders |
| **EV1 halts on an unreadable earnings item** | A news item mentioning earnings that yields no EPS means our fair value is stale while the room has repriced. The bot cancels, stops taking risk, and screams in the log until you write `eps_override.json` |
| **Protected orders, never naked market orders** | Both bots send marketable limits at their break-even price. A market order sized off a stale book snapshot can fill several levels worse; a crossing limit fills everything better and simply does not fill the rest |
| **Limit prices round in the safe direction** | `round()` moved a protective limit adversely ~50% of the time. Now floor for buys, ceil for sells |
| **FI2 prices against a forward envelope** | Bill values step up 12-17c at every compounding tick and the bond's clean price drifts down 1.6c per tick — both known in advance. Quoting at spot theo hands competitors a free, predictable pick-off |
| **FI2 halts on an unexpected clock** | It used to clamp `min(tick, 312)`, which silently made every security look worth par — and would have bought the whole book at 99.98 |
| **EV1 sizes by confidence, not just edge** | With all four quarters still estimates, fair value carries ~$1.00 of standard deviation. Full size there is a bet on the next surprise, not an arbitrage. Tune with `EV1_CONFIDENCE_FLOOR` |
| **Position caps cannot be breached** | An over-cap position used to be read as a quantity to trade, so it grew without bound |
| **Neither bot dies with orders resting** | Any unexpected exception is caught and logged; shutdown always cancels |

## When something goes wrong mid-case

| Symptom | Fix |
|---|---|
| News headline the parser misses | Write `eps_override.json`: `{"2": [0.28, false]}` — picked up next loop, no restart |
| Orders rejected, "exceeds limit" | Lower `--max-pos`; check real caps with `python ritlib/client.py` |
| HTTP 429 | Pass `min_interval=0.2` to `RITClient`, or raise `--interval` |
| Bot behaving oddly | Ctrl-C, switch to `tools/monitor.py`, trade by hand off the edge table |
| `HALTED - could not parse earnings item` | Read the headline in the log, write `eps_override.json`, trading resumes in under a second |
| `HALT: unexpected clock` | The case clock is not 312 ticks x 2 periods. The pricing model does not apply — trade manually |
| Position stuck | `python -c "from ritlib.client import RITClient; RITClient().flatten('PI')"` |

Every run writes a timestamped log to `logs/`.
