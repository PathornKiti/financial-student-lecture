"""
Thin, defensive wrapper around the RIT (Rotman Interactive Trader) REST API v1.

The RIT client application exposes a local REST server, by default at
    http://localhost:9999/v1
RIT exposes TWO REST APIs, and they authenticate DIFFERENTLY:

  1. CLIENT REST API - served by the RIT Client on your own machine
     (http://localhost:9999/v1). Authenticate with the API key you read from the
     client's "API" icon on the bottom bar, sent in the `X-API-Key` header.

  2. DMA REST API - served by the RIT Instructor App (the SERVER), directly.
     Same hostname you log the client into, but a DIFFERENT port (the official
     docs use 10001 as their example). Authenticate with HTTP BASIC auth using
     your LOGIN TRADER ID AND PASSWORD - not an API key:
         Authorization: Basic base64("traderID:password")

This class sends whichever credentials you configure, and will send both at once
if you configure both - the two use different headers, so there is no conflict
and the server simply honours the one it checks. That means the same code works
against either API without a mode switch.

Note on permissions: the bottom bar's "API Orders" icon is the right to SUBMIT
orders, and per Rotman's feature guide it is OFF by default for every case except
ALGO cases. It is granted by the server, not by you - if it is grey, every order
will be rejected and you can only read.

Every method returns plain Python dicts/lists straight from the API so that you
can print them and sanity-check field names on the competition machine. Field
names below match the published v1 API, but ALWAYS run `python -m ritlib.client`
once on the real client before the competition to confirm.
"""

from __future__ import annotations

import math
import os
import time
import threading
from typing import Any, Iterable

import requests

from . import config


class RITError(RuntimeError):
    """Any non-2xx response from the RIT API."""


class RateLimited(RITError):
    """HTTP 429. The API tells us how long to wait in the body."""

    def __init__(self, wait: float):
        super().__init__(f"rate limited, wait {wait:.3f}s")
        self.wait = wait


class RITClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        trader_id: str | None = None,
        password: str | None = None,
        timeout: float = 5.0,
        max_retries: int = 3,
        min_interval: float | None = None,
    ):
        # Defaults come from .env via ritlib.config, so nothing here needs an
        # API key pasted into it. Explicit arguments still win.
        self.base_url = (base_url or config.base_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else config.api_key()
        self.timeout = timeout
        self.max_retries = max_retries
        # Client-side throttle. RIT's server-side limit is configurable; set
        # RIT_MIN_INTERVAL=0.2 in .env if you start seeing 429s.
        self.min_interval = (config.setting("RIT_MIN_INTERVAL", 0.0, float)
                             if min_interval is None else min_interval)
        self.trader_id = trader_id if trader_id is not None else config.trader_id()
        self.password = password if password is not None else config.password()
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        # Documented casing is X-API-Key. Header names are case-insensitive per
        # RFC 7230, but matching the spec exactly costs nothing.
        if self.api_key:
            self.session.headers.update({"X-API-Key": self.api_key})
        if self.trader_id:
            # HTTP Basic, for the DMA REST API. requests builds the header.
            self.session.auth = (self.trader_id, self.password or "")

    @property
    def auth_mode(self) -> str:
        modes = []
        if self.api_key:
            modes.append("X-API-Key")
        if self.trader_id:
            modes.append(f"Basic({self.trader_id})")
        return " + ".join(modes) or "NONE"

    # ---------------------------------------------------------------- plumbing
    def _request(self, method: str, path: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            with self._lock:
                if self.min_interval:
                    gap = time.time() - self._last_call
                    if gap < self.min_interval:
                        time.sleep(self.min_interval - gap)
                self._last_call = time.time()
            try:
                r = self.session.request(method, url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:      # client not running / net blip
                last_exc = exc
                time.sleep(0.25 * (attempt + 1))
                continue

            if r.status_code == 429:
                # Spec: a 429 carries BOTH a Retry-After header and a `wait` body
                # field. Per-security limits can be stricter than the global one,
                # so honour whichever value is larger.
                wait = 0.5
                try:
                    wait = float(r.json().get("wait", wait))
                except Exception:
                    pass
                try:
                    wait = max(wait, float(r.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(wait, 2.0))
                last_exc = RateLimited(wait)
                continue

            if r.status_code >= 400:
                raise RITError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")

            if not r.content:
                return None
            return r.json()

        raise RITError(f"{method} {path} failed after {self.max_retries} attempts: {last_exc}")

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, **params)

    def post(self, path: str, **params) -> Any:
        return self._request("POST", path, **params)

    def delete(self, path: str, **params) -> Any:
        return self._request("DELETE", path, **params)

    # ------------------------------------------------------------------ state
    def case(self) -> dict:
        """{'name','period','tick','ticks_per_period','status'} status: ACTIVE/PAUSED/STOPPED."""
        return self.get("/case")

    def trader(self) -> dict:
        """{'trader_id','first_name','last_name','nlv'}"""
        return self.get("/trader")

    def limits(self) -> list[dict]:
        """[{'name','gross','net','gross_limit','net_limit','gross_fine','net_fine'}]"""
        return self.get("/limits")

    def news(self, since: int | None = None, limit: int = 50) -> list[dict]:
        """[{'news_id','period','tick','ticker','headline','body'}] newest first."""
        return self.get("/news", since=since, limit=limit)

    def assets(self, ticker: str | None = None) -> list[dict]:
        return self.get("/assets", ticker=ticker)

    def securities(self, ticker: str | None = None) -> list[dict]:
        """Per-security snapshot: bid/ask/last/position/vwap/nlv_contribution/..."""
        return self.get("/securities", ticker=ticker)

    def book(self, ticker: str, limit: int = 20) -> dict:
        """{'bids': [...], 'asks': [...]} each order dict has price/quantity/quantity_filled."""
        return self.get("/securities/book", ticker=ticker, limit=limit)

    def history(self, ticker: str, period: int | None = None, limit: int | None = None) -> list[dict]:
        """OHLC per tick, newest first."""
        return self.get("/securities/history", ticker=ticker, period=period, limit=limit)

    def tas(self, ticker: str, after: int | None = None, limit: int | None = None) -> list[dict]:
        """Time & sales."""
        return self.get("/securities/tas", ticker=ticker, after=after, limit=limit)

    # ----------------------------------------------------------------- orders
    def orders(self, status: str = "OPEN") -> list[dict]:
        """status in {OPEN, TRANSACTED, CANCELLED}."""
        return self.get("/orders", status=status)

    def order(self, order_id: int) -> dict:
        return self.get(f"/orders/{order_id}")

    def market_order(self, ticker: str, action: str, quantity: int) -> dict:
        return self.post(
            "/orders", ticker=ticker, type="MARKET",
            quantity=quantity, action=action.upper(),
        )

    @staticmethod
    def round_limit(price: float, action: str) -> float:
        """
        Round a limit price to the cent in the SAFE direction.

        round() moves the price adversely about half the time - a protective BUY
        limit of 96.9055 becomes 96.91, i.e. half a cent ABOVE the break-even it
        was supposed to enforce. Floor for buys, ceiling for sells, so a limit
        meant to cap your cost can never end up above it.
        """
        cents = price * 100
        return (math.floor(cents) if action.upper() == "BUY" else math.ceil(cents)) / 100

    def limit_order(self, ticker: str, action: str, quantity: int, price: float) -> dict:
        action = action.upper()
        return self.post(
            "/orders", ticker=ticker, type="LIMIT",
            quantity=quantity, action=action,
            price=self.round_limit(price, action),
        )

    def cancel(self, order_id: int) -> dict:
        return self.delete(f"/orders/{order_id}")

    def cancel_all(self, ticker: str | None = None) -> dict:
        """Bulk cancel. Without a ticker this cancels every open order."""
        if ticker:
            return self.post("/commands/cancel", ticker=ticker)
        return self.post("/commands/cancel", all=1)

    def cancel_query(self, query: str) -> dict:
        """e.g. cancel_query("Price > 15.50 AND Volume > 0")"""
        return self.post("/commands/cancel", query=query)

    # ---------------------------------------------------------------- tenders
    def tenders(self) -> list[dict]:
        return self.get("/tenders")

    def accept_tender(self, tender_id: int, price: float | None = None) -> dict:
        return self.post(f"/tenders/{tender_id}", price=price)

    def decline_tender(self, tender_id: int) -> dict:
        return self.delete(f"/tenders/{tender_id}")

    # -------------------------------------------------------------- shortcuts
    def sec(self, ticker: str) -> dict:
        rows = self.securities(ticker=ticker)
        if not rows:
            raise RITError(f"unknown ticker {ticker!r}")
        return rows[0]

    def quote(self, ticker: str) -> tuple[float | None, float | None]:
        s = self.sec(ticker)
        return s.get("bid"), s.get("ask")

    def position(self, ticker: str) -> int:
        return int(self.sec(ticker).get("position", 0))

    def tick(self) -> int:
        return int(self.case()["tick"])

    def is_active(self) -> bool:
        return self.case().get("status") == "ACTIVE"

    def wait_for_start(self, poll: float = 0.5) -> dict:
        """Block until the case is ACTIVE. Safe to call before the buzzer."""
        while True:
            c = self.case()
            if c.get("status") == "ACTIVE":
                return c
            time.sleep(poll)

    def flatten(self, ticker: str, max_order_size: int = 10_000) -> None:
        """Cancel resting orders and market out of the position in this ticker."""
        self.cancel_all(ticker)
        pos = self.position(ticker)
        action = "SELL" if pos > 0 else "BUY"
        for chunk in slice_qty(abs(pos), max_order_size):
            self.market_order(ticker, action, chunk)


def slice_qty(quantity: int, max_order_size: int) -> Iterable[int]:
    """RIT rejects oversized orders outright; always slice."""
    quantity = int(abs(quantity))
    while quantity > 0:
        chunk = min(quantity, max_order_size)
        yield chunk
        quantity -= chunk


def _selftest() -> None:
    """Dump raw API responses so you can verify field names on the real client."""
    import json

    c = RITClient()
    print(config.describe())
    for name, fn in [
        ("case", c.case),
        ("trader", c.trader),
        ("limits", c.limits),
        ("securities", c.securities),
        ("news", lambda: c.news(limit=3)),
        ("open orders", lambda: c.orders("OPEN")),
    ]:
        try:
            print(f"\n--- {name} ---")
            print(json.dumps(fn(), indent=2)[:1200])
        except Exception as exc:
            print(f"  FAILED: {exc}")


if __name__ == "__main__":
    _selftest()
