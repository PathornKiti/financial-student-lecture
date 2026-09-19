# Getting started — you have never used RIT before

Read this once tonight. It takes ten minutes and covers the software, the setup,
and what to click when.

---

## 1. What RIT actually is

RIT is a **desktop application you install on Windows**. It is not a website.

```
 competition server  ←── you log in through the client's own login screen
        ↑
   RIT Client (Windows app, on your desk)
        ↓  exposes a local REST API on http://localhost:9999
   your Python code
```

Two consequences people get wrong:

- **The API is served by the client on your own machine**, not by a Rotman
  server. There is no Rotman URL to point at. You log the *client* into the
  competition server, and the client then hands you `localhost:9999`. That is
  why `.env` defaults to `localhost` and you almost never change it.
- **The client is Windows-only.** You are on a Mac, so you cannot run it here.
  That is what `mock/mock_rit_server.py` is for — it serves the identical
  endpoints so you can test tonight.

A case runs on a clock measured in **ticks** (one tick ≈ one second) grouped into
**periods**. FI2 is 2 periods × 312 ticks; EV1 is one 8-minute period.

---

## 2. The screens you will see

You get an empty, modular workspace and open the modules you want from the top
menu. The ones that matter:

| Module | What it shows | Why you care |
|---|---|---|
| **Portfolio** | Every security: position, bid, ask, last, VWAP, volume, realized/unrealized P&L | Your home screen |
| **Book Trader** | The central limit order book — every bid and ask with size and the trader who posted it | Where you read liquidity |
| **Ladder Trader** | Same book, aggregated by price level | Faster to read at a glance |
| **Order Entry** | Ticker, BUY/SELL toggle, quantity, LMT/MKT toggle, price | Manual order entry |
| **Trade Blotter** | Your orders: Live / Partial / Filled / Cancelled | Cancel resting orders here |
| **News** | Headlines — **this is the EV1 case** | Watch it obsessively in EV1 |
| **Time & Sales** | Every trade that printed | Shows what actually traded |
| **User Info** | Cash, buying power, net liquid value (NLV) | NLV is your score |

**Colour code in the book:** new order flashes green, a filled order flashes red
for ¼ second, **your own orders are highlighted blue**.

---

## 3. Two things about orders that cost people money

**A market order walks the book.** It does not fill at the price you see. From
Rotman's own tutorial: a 5,000-share market buy filled 700 @ 25.54, 1,500 @
25.55, 2,100 @ 25.63, 700 @ 25.74 — an average of **25.6088** when the screen
said 25.54. The deeper you go, the worse your average. This is exactly why the
bots here size with `max_qty_for_avg_price` instead of just firing market orders.

**A crossing limit order executes immediately.** If your limit buy is at or above
the best ask, it does not rest in the book — it trades. Use that deliberately: a
limit priced at your break-even is a market order that cannot fill you at a loss.

You **cannot modify** an existing order. Cancel and resubmit.

---

## 4. Manual trading shortcuts

Worth knowing even if a bot is doing the work — you will need these if you kill it.

In **Order Entry**: the quantity and price arrows step by 100 shares / 1 cent.
Hold **Ctrl** while clicking to step by 1,000 shares / 10 cents.

In the **Book Trader**, once you enable the shortcut ("lightning" icon — it
**resets after every case**, so re-enable it each time) and set a default
quantity and offset:

| Action | Result |
|---|---|
| **Left-click a bid** | Place a limit bid one offset better than that level |
| **Alt + left-click your order** | Cancel that order |
| **Right-click** either side | Market order for your default quantity |
| **Shift + right-click** a level | "Swipe" — take everything up to and including that level |

Swipe orders are how you take a whole mispriced stack at once in FI2 by hand.

---

## 5. Setup (do this tonight)

```bash
cd notebook/lecture/rotman_trading_sim
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in **one line**: `RIT_API_KEY=`.

To get that key, in the RIT client: **File → Preferences → API**
1. tick **Enable REST API**
2. tick **Enable API Orders** (needed to *send* orders; leave off if you only read)
3. type any string into the API key box
4. paste the same string into `.env`

Then:

```bash
python tools/doctor.py
```

It checks the file, the key, the connection, the case, your position limits, the
securities, the order book, the tape, and whether dry-run is on — and tells you
exactly what to fix if something is wrong. **Run it first, every time.**

### Testing tonight without the RIT client

```bash
# terminal 1
python mock/mock_rit_server.py --case fi2 --speed 6
# terminal 2
python tools/doctor.py
python bots/fi2_bond_arb.py --verbose         # dry-run by default
```

---

## 6. Competition-day runbook

```bash
python tools/doctor.py                  # 1. everything green?
python tools/fi2_price_table.py         # 2. print the FI2 answer key
python tools/monitor.py --case fi2      # 3. read-only dashboard, leave it running
python bots/fi2_bond_arb.py --verbose   # 4. DRY RUN — watch it for 30 seconds
python bots/fi2_bond_arb.py --live      # 5. go live only when step 4 looked right
```

`RIT_DRY_RUN=true` in `.env` means every bot is safe by default. `--live` is the
only thing that sends real orders. Ctrl-C always stops cleanly.

**Before you go live, confirm with the organiser that API trading is permitted.**
Some Rotman events require manual entry. `tools/monitor.py` only reads, so it is
safe either way.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| **Tick** | One second of case time |
| **Period** | A block of ticks; FI2 has 2, each 312 ticks = 6 calendar months |
| **ANON** | A computer-run participant. You cannot tell informed from uninformed ones |
| **NLV** | Net liquid value — your portfolio closed out at current bid/ask. Your score |
| **VWAP** | Volume-weighted average price — what you actually paid across levels |
| **Clean / dirty price** | Bond quoted without / with accrued interest. RIT quotes **clean** |
| **Tender** | A block offer from an institution, usually off-market, take-it-or-leave-it |
| **Spread** | Best ask − best bid |
| **Book imbalance** | Resting bid size vs ask size; short-term pressure gauge |
| **Microprice** | Size-weighted mid — a better read of value than the plain mid |
