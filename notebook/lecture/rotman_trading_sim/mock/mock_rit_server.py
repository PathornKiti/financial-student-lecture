#!/usr/bin/env python3
"""
Offline stand-in for the RIT client's REST API.

Why this exists: the real RIT client only runs on Windows, and on competition
day you will not get a practice run. This serves the same endpoints on
localhost:9999 with a simulated market, so you can prove the bots work,
tune thresholds, and see the P&L before you ever touch the real thing.

It is a simulator, not an emulator: fills are approximate and the other traders
are noise, not people. Use it to debug logic, not to predict your score.

    python mock/mock_rit_server.py --case fi2
    python mock/mock_rit_server.py --case ev1 --speed 2

Then, in another terminal:
    python bots/fi2_bond_arb.py --verbose
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib.pricing import (
    BOND, COMP_PE, EV1_TICKER, TB6M, TB12M, TICKS_PER_PERIOD, fi2_values,
)

TICK_SIZE = 0.01


class Market:
    """One simulated security: a fair value, a laggy noisy mid, and a book."""

    def __init__(self, ticker: str, spread: float, lot: int, max_order: int, fee: float):
        self.ticker = ticker
        self.spread = spread
        self.lot = lot
        self.max_order = max_order
        self.fee = fee
        self.mid = 0.0
        self.last = 0.0
        self.position = 0
        self.realized = 0.0
        self.tape: list[dict] = []
        self.volume = 0

    def step(self, fair: float, pull: float, sigma: float) -> None:
        if self.mid == 0.0:
            self.mid = fair
        # Mean-revert toward fair value (slow = the humans in the room) + noise.
        self.mid += pull * (fair - self.mid) + random.gauss(0, sigma)
        self.mid = max(self.mid, 0.01)
        self.last = self.mid

    def book(self, depth: int = 10) -> dict:
        bids, asks = [], []
        half = self.spread / 2
        for i in range(depth):
            bid = round(self.mid - half - i * TICK_SIZE, 2)
            ask = round(self.mid + half + i * TICK_SIZE, 2)
            qty = self.lot * random.randint(1, 5)
            bids.append({"order_id": 90000 + i, "trader_id": "ANON", "ticker": self.ticker,
                         "price": bid, "quantity": qty, "quantity_filled": 0,
                         "action": "BUY", "status": "OPEN"})
            asks.append({"order_id": 95000 + i, "trader_id": "ANON", "ticker": self.ticker,
                         "price": ask, "quantity": qty, "quantity_filled": 0,
                         "action": "SELL", "status": "OPEN"})
        # Match the live API exactly: singular keys.
        return {"bid": bids, "ask": asks}

    def best(self) -> tuple[float, float]:
        half = self.spread / 2
        return round(self.mid - half, 2), round(self.mid + half, 2)

    def fill(self, action: str, qty: int, limit: float | None) -> tuple[int, float]:
        """Walk the simulated book. Returns (filled_qty, average_price)."""
        levels = self.book(20)["ask" if action == "BUY" else "bid"]
        filled, notional = 0, 0.0
        for lvl in levels:
            if filled >= qty:
                break
            price = lvl["price"]
            if limit is not None:
                if action == "BUY" and price > limit + 1e-9:
                    break
                if action == "SELL" and price < limit - 1e-9:
                    break
            take = min(qty - filled, lvl["quantity"])
            filled += take
            notional += take * price
        if filled == 0:
            return 0, 0.0
        avg = notional / filled
        self.volume += filled
        self.tape.append({"id": len(self.tape) + 1, "price": round(avg, 2),
                          "quantity": filled, "tick": 0})
        signed = filled if action == "BUY" else -filled
        self.position += signed
        self.realized -= signed * avg + filled * self.fee       # cash effect
        # Trades push the mid a little, like they do in the real case.
        self.mid += (0.0002 * signed) / max(self.lot, 1)
        return filled, avg


class Simulation:
    def __init__(self, case: str, speed: float, seed: int | None,
                 news_gap: bool = False, noise: float = 1.0, spread: float = 1.0,
                 no_api_orders: bool = False):
        random.seed(seed)
        self.no_api_orders = no_api_orders
        self.news_gap = news_gap
        self.noise = noise           # scales how far ANON pushes price off value
        self.spread = spread         # scales the quoted bid-ask
        self.case_name = case
        self.speed = speed
        self.start = time.time()
        self.orders: list[dict] = []
        self.next_order_id = 1000
        self.news: list[dict] = []
        self.lock = threading.Lock()
        self.cash = 1_000_000.0

        if case == "fi2":
            self.periods, self.ticks_per_period = 2, TICKS_PER_PERIOD
            self.markets = {
                TB6M: Market(TB6M, 0.06 * spread, 100, 1000, 0.02),
                TB12M: Market(TB12M, 0.08 * spread, 100, 1000, 0.02),
                BOND: Market(BOND, 0.10 * spread, 100, 1000, 0.02),
            }
            self.pull, self.sigma = 0.15, 0.025 * noise
        else:
            self.periods, self.ticks_per_period = 1, 480
            self.markets = {EV1_TICKER: Market(EV1_TICKER, 0.06, 1000, 10_000, 0.01)}
            self.pull, self.sigma = 0.04, 0.02 * noise
            self.eps = {1: 0.40, 2: 0.24, 3: 0.27, 4: 0.33}
            self._build_ev1_news()

        threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------- EV1 script
    def _build_ev1_news(self) -> None:
        """Estimate revisions during each quarter, an actual at each quarter end."""
        tpq = self.ticks_per_period // 4
        actuals = {1: 0.44, 2: 0.21, 3: 0.30, 4: 0.29}      # what really happens
        self.scheduled: list[tuple[int, str, str, int, float, bool]] = []
        for q in (1, 2, 3, 4):
            for offset in (int(tpq * 0.35), int(tpq * 0.70)):
                drift = round(self.eps[q] + random.gauss(0, 0.03), 2)
                self.scheduled.append((
                    (q - 1) * tpq + offset, "Analyst estimates revised",
                    f"Analysts now estimate Q{q} earnings per share of ${drift:.2f}.",
                    q, drift, False,
                ))
            self.scheduled.append((
                q * tpq - 1, f"Prandium Industries announces Q{q} earnings",
                f"Prandium Industries reported earnings per share of "
                f"${actuals[q]:.2f} for Q{q}.",
                q, actuals[q], True,
            ))
        self.scheduled.sort()
        self._news_idx = 0

    def _release_news(self, tick: int) -> None:
        while self._news_idx < len(self.scheduled) and self.scheduled[self._news_idx][0] <= tick:
            at, headline, body, q, value, actual = self.scheduled[self._news_idx]
            self._news_idx += 1
            self.eps[q] = value
            if self.news_gap:
                # Everyone reprices at once: the mid jumps to the new fair value
                # (plus a little overreaction) instead of drifting there slowly.
                fv = sum(self.eps.values()) * COMP_PE
                m = self.markets[EV1_TICKER]
                m.mid = fv * (1 + random.gauss(0, 0.004))
            self.news.insert(0, {
                "news_id": self._news_idx, "period": 1, "tick": at,
                "ticker": EV1_TICKER, "headline": headline, "body": body,
            })

    # ----------------------------------------------------------------- engine
    def clock(self) -> tuple[int, int, str]:
        elapsed = (time.time() - self.start) * self.speed
        total = int(elapsed)
        period = total // self.ticks_per_period + 1
        tick = total % self.ticks_per_period
        if period > self.periods:
            return self.periods, self.ticks_per_period, "STOPPED"
        return period, tick, "ACTIVE"

    def fair_values(self, period: int, tick: int) -> dict[str, float]:
        if self.case_name == "fi2":
            v = fi2_values(min(tick, TICKS_PER_PERIOD), min(period, 2))
            out = {TB12M: v.tb12m, BOND: v.bond_clean}
            if v.tb6m is not None:
                out[TB6M] = v.tb6m
            return out
        self._release_news(tick)
        return {EV1_TICKER: sum(self.eps.values()) * COMP_PE}

    def _loop(self) -> None:
        while True:
            period, tick, status = self.clock()
            if status != "ACTIVE":
                time.sleep(0.2)
                continue
            with self.lock:
                fvs = self.fair_values(period, tick)
                for ticker, market in self.markets.items():
                    if ticker not in fvs:
                        continue
                    market.step(fvs[ticker], self.pull, self.sigma)
                    # ANON liquidity traders print trades around the mid.
                    if random.random() < 0.5:
                        bid, ask = market.best()
                        market.volume += market.lot
                        market.tape.append({
                            "id": len(market.tape) + 1,
                            "price": round(random.choice([bid, ask]), 2),
                            "quantity": market.lot * random.randint(1, 3),
                            "tick": tick,
                        })
                        del market.tape[:-400]
                self._match_resting()
            time.sleep(0.2 / self.speed)

    def _match_resting(self) -> None:
        """Resting orders fill when the simulated mid trades through them."""
        for o in self.orders:
            if o["status"] != "OPEN":
                continue
            m = self.markets.get(o["ticker"])
            if m is None:
                continue
            bid, ask = m.best()
            crossed = (o["action"] == "BUY" and o["price"] >= ask) or \
                      (o["action"] == "SELL" and o["price"] <= bid)
            # ANON market orders occasionally sweep passive quotes at the touch.
            touched = (o["action"] == "BUY" and o["price"] >= bid) or \
                      (o["action"] == "SELL" and o["price"] <= ask)
            if crossed or (touched and random.random() < 0.08):
                remaining = o["quantity"] - o["quantity_filled"]
                filled, avg = m.fill(o["action"], remaining, o["price"])
                if filled:
                    o["quantity_filled"] += filled
                    o["vwap"] = avg
                    if o["quantity_filled"] >= o["quantity"]:
                        o["status"] = "TRANSACTED"

    # ------------------------------------------------------------------- API
    def nlv(self) -> float:
        total = self.cash + sum(m.realized for m in self.markets.values())
        period, tick, _ = self.clock()
        fvs = self.fair_values(period, tick)
        for ticker, m in self.markets.items():
            total += m.position * fvs.get(ticker, m.mid)
        return round(total, 2)

    def securities(self, period: int) -> list[dict]:
        rows = []
        for ticker, m in self.markets.items():
            if ticker == TB6M and self.case_name == "fi2" and period >= 2:
                continue
            bid, ask = m.best()
            rows.append({
                "ticker": ticker, "type": "STOCK" if self.case_name == "ev1" else "BOND",
                "bid": bid, "bid_size": m.lot * 3, "ask": ask, "ask_size": m.lot * 3,
                "last": round(m.last, 2), "position": m.position,
                "vwap": round(m.mid, 2), "nlv": round(m.position * m.mid, 2),
                "volume": m.volume,
                "max_trade_size": m.max_order, "trading_fee": m.fee,
            })
        return rows

    def place(self, params: dict) -> dict:
        ticker = params.get("ticker", [""])[0]
        m = self.markets.get(ticker)
        if m is None:
            raise KeyError(f"unknown ticker {ticker}")
        qty = int(float(params.get("quantity", ["0"])[0]))
        if qty > m.max_order:
            raise ValueError(f"order size {qty} exceeds max {m.max_order}")
        action = params.get("action", ["BUY"])[0].upper()
        otype = params.get("type", ["MARKET"])[0].upper()
        price = float(params["price"][0]) if "price" in params else None

        with self.lock:
            self.next_order_id += 1
            order = {
                "order_id": self.next_order_id, "period": self.clock()[0],
                "tick": self.clock()[1], "trader_id": "MOCK", "ticker": ticker,
                "type": otype, "quantity": qty, "action": action,
                "price": price, "quantity_filled": 0, "vwap": 0.0, "status": "OPEN",
            }
            filled, avg = m.fill(action, qty, None if otype == "MARKET" else price)
            order["quantity_filled"] = filled
            order["vwap"] = round(avg, 4)
            if otype == "MARKET" or filled >= qty:
                order["status"] = "TRANSACTED" if filled else "CANCELLED"
            self.orders.append(order)
            return order


class Handler(BaseHTTPRequestHandler):
    sim: Simulation = None          # injected below

    def _send(self, payload, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass                        # keep the console readable

    def _route(self, method: str):
        url = urlparse(self.path)
        path = url.path.replace("/v1", "", 1)
        params = parse_qs(url.query)
        sim = self.sim
        period, tick, status = sim.clock()

        if path == "/case":
            return {"name": f"MOCK-{sim.case_name.upper()}", "period": period, "tick": tick,
                    "ticks_per_period": sim.ticks_per_period, "status": status}
        if path == "/trader":
            return {"trader_id": "MOCK", "first_name": "Mock", "last_name": "Trader",
                    "nlv": sim.nlv()}
        if path == "/limits":
            return [{"name": "default", "gross": 0, "net": 0,
                     "gross_limit": 250_000, "net_limit": 100_000,
                     "gross_fine": 0, "net_fine": 0}]
        if path == "/news":
            limit = int(params.get("limit", ["50"])[0])
            rows = sim.news
            since = params.get("since", [None])[0]
            if since is not None:
                rows = [n for n in rows if n["news_id"] > int(since)]
            return rows[:limit]
        if path == "/securities":
            rows = sim.securities(period)
            t = params.get("ticker", [None])[0]
            return [r for r in rows if t is None or r["ticker"] == t]
        if path == "/securities/book":
            t = params["ticker"][0]
            return sim.markets[t].book(int(params.get("limit", ["10"])[0]))
        if path == "/securities/tas":
            t = params["ticker"][0]
            rows = list(reversed(sim.markets[t].tape))
            after = params.get("after", [None])[0]
            if after is not None:
                rows = [r for r in rows if r["id"] > int(after)]
            limit = int(params.get("limit", ["50"])[0])
            return rows[:limit]
        if path == "/securities/history":
            return []
        if path == "/orders" and method == "GET":
            want = params.get("status", ["OPEN"])[0]
            return [o for o in sim.orders if o["status"] == want]
        if path == "/orders" and method == "POST":
            if sim.no_api_orders:
                raise PermissionError("API order submission has been disabled")
            return sim.place(params)
        if path.startswith("/orders/") and method == "DELETE":
            oid = int(path.rsplit("/", 1)[1])
            for o in sim.orders:
                if o["order_id"] == oid:
                    o["status"] = "CANCELLED"
                    return o
            raise KeyError(f"no order {oid}")
        if path == "/commands/cancel":
            t = params.get("ticker", [None])[0]
            cancelled = []
            for o in sim.orders:
                if o["status"] == "OPEN" and (t is None or o["ticker"] == t):
                    o["status"] = "CANCELLED"
                    cancelled.append(o["order_id"])
            return {"cancelled_order_ids": cancelled}
        if path in ("/tenders", "/leases", "/assets"):
            return []
        raise FileNotFoundError(path)

    def _handle(self, method: str) -> None:
        try:
            self._send(self._route(method))
        except FileNotFoundError as exc:
            self._send({"code": "Not Found", "message": str(exc)}, 404)
        except PermissionError as exc:
            self._send({"code": "Forbidden", "message": str(exc)}, 403)
        except (KeyError, ValueError) as exc:
            self._send({"code": "Bad Request", "message": str(exc)}, 400)
        except Exception as exc:                                  # noqa: BLE001
            self._send({"code": "Server Error", "message": repr(exc)}, 500)

    def do_GET(self):     self._handle("GET")       # noqa: E704
    def do_POST(self):    self._handle("POST")      # noqa: E704
    def do_DELETE(self):  self._handle("DELETE")    # noqa: E704


def main() -> None:
    p = argparse.ArgumentParser(description="Mock RIT REST API")
    p.add_argument("--case", choices=["fi2", "ev1"], default="fi2")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--speed", type=float, default=1.0, help="ticks per real second")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-api-orders", action="store_true",
                   help="refuse order submission with 403, as a non-ALGO case does")
    p.add_argument("--noise", type=float, default=1.0,
                   help="scale how far ANON pushes price from fair value "
                        "(<1 = a tighter, more competitive market)")
    p.add_argument("--spread", type=float, default=1.0, help="scale the bid-ask")
    p.add_argument("--news-gap", action="store_true",
                   help="price gaps to the new fair value on news (realistic for a "
                        "competitive room) instead of drifting there over ~25 ticks")
    args = p.parse_args()

    Handler.sim = Simulation(args.case, args.speed, args.seed, args.news_gap,
                             args.noise, args.spread, args.no_api_orders)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock RIT [{args.case}] on http://127.0.0.1:{args.port}/v1  "
          f"speed={args.speed}x  ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
