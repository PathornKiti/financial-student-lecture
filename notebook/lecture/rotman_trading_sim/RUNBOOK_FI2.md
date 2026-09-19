# FI2 Runbook — Coupon Govt Bonds

Keep this open at the desk. Commands are in order; don't skip steps 0–3.

---

## Step 0 — Before the case (do tonight)

```bash
cd notebook/lecture/rotman_trading_sim
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python tools/fi2_price_table.py       # writes reference/fi2_fair_values.{csv,md} — PRINT IT
```

That table is the whole case solved in advance: every security's fair value at every
tick of both periods. Even if every bit of software fails, you can trade off the paper.

**Ask your instructor one question tonight:** *is FI2 loaded as an ALGO case?*
If not, `API Orders` will be greyed out and no bot can send orders — you'd run
`tools/monitor.py` and click manually. Both paths are ready; knowing beforehand is better.

---

## Step 1 — Log the RIT Client in

The RIT Client is a Windows app. Log it into the competition server:

```
host  161.200.68.42      port  10000
```

That address goes in the **client's login screen**, never in `.env`.

---

## Step 2 — Read the API panel

Look at the **bottom bar**, bottom-right. Click the **API** icon.

Check the three icons — **green = on, grey = off, red = error**:

| Icon | Means |
|---|---|
| **API** | you can read market data |
| **RTD** | Excel data pulls |
| **API Orders** | **you can send orders** — grey = read-only, go manual |

Copy the **API key** from that panel.

---

## Step 3 — Point `.env` at the right endpoint

Two possibilities. The probe tells you which:

```bash
python tools/doctor.py --probe
```

**Client API (preferred)** — the client is running on this machine:

```ini
RIT_API_KEY=<key from the API panel>
# leave RIT_URL commented out
```

**DMA API** — only if you cannot run the Windows client (it needs no client):

```ini
RIT_URL=http://161.200.68.42:10002/v1
RIT_TRADER_ID=<your RIT login id>
RIT_PASSWORD=<your RIT login password>
RIT_MIN_INTERVAL=0.1
```

DMA uses your **login credentials**, not an API key, and rate-limits harder because the
smoothing lives inside the client. Prefer the Client API when you have the choice.

---

## Step 4 — Full setup check

```bash
python tools/doctor.py
```

Every line must be `OK`. It verifies the key/login, the connection, the case clock, your
**real position limits**, the securities, the order book and the tape.

**Write down the position limits it prints.** The default `FI2_MAX_POS=5000` is
deliberately conservative, and testing showed P&L scales almost linearly with it — it is
the single biggest dial you have. Raise it to ~80% of the reported limit, but watch
buying power: 5,000 of each security is already ~$1.45M of notional against $1M.

---

## Step 5 — Watch it think (do NOT skip)

```bash
python bots/fi2_bond_arb.py --verbose
```

Dry-run by default — it sends nothing. Watch for 30 seconds. You want to see:

```
FI2 bot | DRY RUN | url=...  api_key=tes…ey
max_pos=5000 max_order=1000 min_edge=$0.02 commission=$0.02
case ACTIVE
P1 t= 12 | TB6M: theo= 96.800 mkt= 96.650 edge=+0.150 pos=   +0 | ...
[DRY] BUY  4497 TB6M  vwap  96.7595 theo  96.7995 edge +0.0400 ...
```

Sanity-check two things against your printed table:
- `theo` matches the table for that tick (opening: TB6M **96.6736**, TB12M **92.5966**, 1YCP clean **102.0601**)
- it buys when `mkt < theo` and sells when `mkt > theo` — never the reverse

If `theo` disagrees with the paper table, **stop** and trade manually. Something is wrong
with the clock convention.

---

## Step 6 — Go live

```bash
python bots/fi2_bond_arb.py --live --verbose
```

That's it. `--live` is the only thing that sends real orders.

Once comfortable, add passive quoting for extra P&L:

```bash
python bots/fi2_bond_arb.py --live --verbose --mm
```

Only after the base strategy looks right. `--mm` rests orders, which can be picked off.

Recommended once you know the real limits:

```bash
python bots/fi2_bond_arb.py --live --verbose --max-pos 20000
```

---

## Step 7 — Second terminal: read-only dashboard

```bash
python tools/monitor.py --case fi2
```

Always safe, sends nothing. Shows theo vs market, the edge per security, **how much size
the edge actually supports**, book depth, and the model-free bond-vs-bills arbitrage.
This is also your fallback if you kill the bot.

---

## Stopping

`Ctrl-C`. The bot always cancels its resting orders on the way out. Your **position is
left alone** — in FI2 that's correct, since bills settle at $100 and the bond pays out.

---

## If something goes wrong

| What you see | Do this |
|---|---|
| `HALT: unexpected clock` | The case isn't 312 ticks × 2 periods, so the pricing model doesn't apply. Trade manually off the table. |
| Orders rejected, every one | `API Orders` is grey — read-only case. Switch to `monitor.py`. |
| `authentication rejected (401)` | Client API: key doesn't match the API panel. DMA: use your login ID/password, not a key. |
| HTTP 429 | Set `RIT_MIN_INTERVAL=0.1` in `.env`, or raise `--interval`. |
| `theo` disagrees with the printed table | Stop. Trade manually. Don't debug mid-case. |
| Bot acting strangely | `Ctrl-C`, switch to `monitor.py`, click manually. |
| Position stuck after a crash | `python -c "from ritlib.client import RITClient; RITClient().cancel_all()"` |

Every run writes a timestamped log to `logs/`.

---

## The three things that make or break FI2

1. **Print the fair-value table.** It's the answer key and it never fails.
2. **`FI2_MAX_POS` is the biggest dial.** Read the real limits in step 4 and raise it.
3. **Commission is $0.02/bond and your edge is a few cents.** A 1-cent edge is a loss —
   which is why `--min-edge` defaults to $0.02 *on top of* commission. Don't lower it.
