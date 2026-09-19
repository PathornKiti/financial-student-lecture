#!/usr/bin/env python3
"""
Run this FIRST, before anything else. It checks your setup end to end and tells
you exactly what to fix, in order.

    python tools/doctor.py

It is completely read-only: it never sends an order. Safe to run during a live
case if something stops working.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ritlib import config
from ritlib.client import RITClient, RITError
from ritlib.microstructure import OrderBook, Tape

OK, WARN, BAD = "\033[32m  OK  \033[0m", "\033[33m WARN \033[0m", "\033[31m FAIL \033[0m"
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

problems: list[str] = []


def check(label: str, status: str, detail: str = "", fix: str = "") -> None:
    print(f"[{status}] {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if fix:
        print(f"         {DIM}-> {fix}{RESET}")
        problems.append(f"{label}: {fix}")


def probe() -> int:
    """
    Which RIT API can this machine actually reach?

    RIT exposes two, and in a lab the firewall usually opens only some ports:
      Client API  localhost:9999   - needs the Windows RIT Client running here
      DMA API     <server>:<port>  - needs no client, but is often restricted
                                     to the lab network
    """
    import socket
    from urllib.parse import urlparse

    print(f"\n{BOLD}Endpoint probe{RESET}\n" + "=" * 62)
    targets = [("Client API", config.client_url(), "X-API-Key")]
    if config.dma_url():
        targets.append(("DMA API", config.dma_url(), "Basic (login ID + password)"))

    reachable = []
    for name, url, auth in targets:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port or 80
        sock = socket.socket()
        sock.settimeout(5)
        try:
            sock.connect((host, port))
            tcp = True
        except Exception as exc:
            tcp, err = False, f"{type(exc).__name__}"
        finally:
            sock.close()

        if not tcp:
            check(f"{name}  {url}", BAD, f"TCP unreachable ({err})")
            continue
        try:
            c = RITClient(base_url=url)
            case = c.case()
            check(f"{name}  {url}", OK,
                  f"LIVE - {case.get('name')} {case.get('status')} | auth: {auth}")
            reachable.append((name, url))
        except RITError as exc:
            msg = str(exc)
            if "401" in msg or "403" in msg:
                check(f"{name}  {url}", WARN, f"reachable but AUTH REJECTED - needs {auth}")
                reachable.append((name, url))
            else:
                check(f"{name}  {url}", BAD, f"TCP open but not speaking the API: {msg[:70]}")

    print("=" * 62)
    if not reachable:
        print(f"\n{BOLD}Neither endpoint is reachable.{RESET}")
        print("  - Client API: is the Windows RIT Client running on THIS machine?")
        print("  - DMA API: are you on the lab network? Check the port under the")
        print("    client's 'API' icon.\n")
        return 1
    print(f"\n{BOLD}Use this one:{RESET}")
    name, url = reachable[0]
    print(f"  {name} -> {url}")
    if name == "DMA API":
        print(f"  Put in .env:  RIT_URL={url}")
        print( "                RIT_TRADER_ID / RIT_PASSWORD = your RIT login")
        print( "                RIT_MIN_INTERVAL=0.1   (DMA rate-limits harder)")
    else:
        print( "  Put in .env:  RIT_API_KEY = the key under the client's 'API' icon")
        print( "                (leave RIT_URL commented out)")
    print()
    return 0


def main() -> int:
    if "--probe" in sys.argv:
        return probe()
    print(f"\n{BOLD}RIT setup check{RESET}\n" + "=" * 62)

    # 1 ---------------------------------------------------------------- .env
    env = config.ROOT / ".env"
    if env.exists():
        check(".env file", OK, str(env.name))
    else:
        check(".env file", BAD, "not found",
              "cp .env.example .env   then put your API key in it")
        print("\nCannot continue without .env.\n")
        return 1

    # 2 ------------------------------------------------------------ API key
    key = config.api_key()
    tid = config.trader_id()
    if tid:
        check("DMA Basic auth", OK, f"trader_id={tid} "
              f"password={'set' if config.password() else 'EMPTY'}")
        print(f"         {DIM}-> using the DMA REST API (server-hosted). Make sure "
              f"RIT_URL points at the DMA port, not the client's 9999.{RESET}")
    if not key and not tid:
        check("API key", BAD, "RIT_API_KEY is empty",
              "in the RIT client click the 'API' icon on the BOTTOM BAR, read the "
              "key there, paste it into .env")
    elif not key:
        check("API key", OK, "not set - not needed when using DMA Basic auth")
    elif len(key) < 4:
        check("API key", WARN, f"only {len(key)} characters",
              "make sure this matches the key in the RIT client exactly")
    else:
        check("API key", OK, f"{key[:3]}…{key[-2:]} ({len(key)} chars)")

    # 3 ------------------------------------------------------------- reachable
    url = config.base_url()
    client = RITClient()
    try:
        case = client.case()
        check("RIT API reachable", OK, url)
    except RITError as exc:
        msg = str(exc)
        if "Connection refused" in msg or "Max retries" in msg:
            check("RIT API reachable", BAD, url,
                  "the RIT client is not running, or the REST API is not enabled, "
                  "or the port is wrong. On macOS the real client cannot run at all - "
                  "use: python mock/mock_rit_server.py --case fi2")
        elif "401" in msg or "403" in msg:
            if config.trader_id():
                fix = ("DMA API: RIT_TRADER_ID / RIT_PASSWORD must be your RIT "
                       "LOGIN credentials, not an API key")
            else:
                fix = ("Client API: RIT_API_KEY must match the key shown under the "
                       "client's 'API' icon. If you are pointing at the SERVER's DMA "
                       "port instead, set RIT_TRADER_ID / RIT_PASSWORD - DMA uses "
                       "HTTP Basic with your login, not an API key")
            check("RIT API reachable", BAD, "authentication rejected (401/403)", fix)
        else:
            check("RIT API reachable", BAD, msg[:120], "see the error above")
        print(f"\n{BOLD}Stopped: cannot reach the API.{RESET}\n")
        return 1

    # 4 ---------------------------------------------------------------- case
    status = case.get("status", "?")
    detail = (f"{case.get('name','?')} | period {case.get('period')} | "
              f"tick {case.get('tick')}/{case.get('ticks_per_period')} | {status}")
    if status == "ACTIVE":
        check("Case running", OK, detail)
    elif status in ("PAUSED", "STOPPED"):
        check("Case running", WARN, detail,
              "bots wait for ACTIVE automatically - this is fine before the buzzer")
    else:
        check("Case running", WARN, detail)

    # 5 -------------------------------------------------------------- trader
    try:
        t = client.trader()
        check("Trader account", OK,
              f"{t.get('first_name','')} {t.get('last_name','')} "
              f"({t.get('trader_id','?')}) NLV ${float(t.get('nlv', 0)):,.2f}")
    except RITError as exc:
        check("Trader account", WARN, str(exc)[:90])

    # 6 -------------------------------------------------------------- limits
    try:
        limits = client.limits()
        if limits:
            for row in limits:
                check(f"Position limit '{row.get('name','default')}'", OK,
                      f"net {row.get('net')}/{row.get('net_limit')}  "
                      f"gross {row.get('gross')}/{row.get('gross_limit')}")
            print(f"         {DIM}-> set FI2_MAX_POS / EV1_MAX_POS in .env "
                  f"below these numbers{RESET}")
        else:
            check("Position limits", WARN, "none reported",
                  "this case may not enforce limits; keep *_MAX_POS conservative")
    except RITError as exc:
        check("Position limits", WARN, str(exc)[:90])

    # 7 ---------------------------------------------------------- securities
    try:
        secs = client.securities()
        if not secs:
            check("Securities", BAD, "none returned", "is a case loaded in the client?")
        else:
            names = ", ".join(s["ticker"] for s in secs)
            check("Securities", OK, f"{len(secs)}: {names}")
            missing = [k for k in ("bid", "ask", "position") if k not in secs[0]]
            if missing:
                check("Security fields", WARN, f"missing {missing}",
                      "this RIT build names fields differently - check the dump below")
            else:
                check("Security fields", OK, "bid/ask/position present")
    except RITError as exc:
        check("Securities", BAD, str(exc)[:90])
        secs = []

    # 8 ------------------------------------------------------- book + liquidity
    if secs:
        ticker = secs[0]["ticker"]
        try:
            book = OrderBook.from_api(client.book(ticker, limit=10), ticker)
            if book.mid is None:
                check(f"Order book ({ticker})", WARN, "no two-sided market yet",
                      "normal before the case starts")
            else:
                check(f"Order book ({ticker})", OK, book.summary(reference_size=1000))
                ref = 1000
                est = book.walk("BUY", ref)
                print(f"         {DIM}-> a {ref}-unit market BUY would fill "
                      f"{est.filled} at vwap {est.vwap:.4f} "
                      f"(slippage {est.slippage:.4f} vs touch){RESET}")
                anon = book.anon_share("ASK")
                if anon is not None:
                    print(f"         {DIM}-> {anon:.0%} of the ask side is ANON "
                          f"(computer liquidity){RESET}")
        except RITError as exc:
            check(f"Order book ({ticker})", WARN, str(exc)[:90])

        try:
            tape = Tape.from_api(client.tas(ticker, limit=50))
            if tape.trades:
                check(f"Time & sales ({ticker})", OK,
                      f"{len(tape.trades)} trades, vol {tape.volume}, "
                      f"vwap {tape.vwap:.4f}, flow {tape.flow_ratio():+.2f}")
            else:
                check(f"Time & sales ({ticker})", WARN, "no trades yet")
        except RITError as exc:
            check(f"Time & sales ({ticker})", WARN, str(exc)[:90])

    # 9 ---------------------------------------------------------- order entry
    try:
        client.orders("OPEN")
        check("Order endpoint readable", OK, "GET /orders works")
    except RITError as exc:
        check("Order endpoint readable", WARN, str(exc)[:90],
              "if this fails, 'Enable API Orders' may be off in the RIT client")

    # 10 -------------------------------------------------------------- safety
    dry = config.setting("RIT_DRY_RUN", True, bool)
    check("Dry-run default", OK if dry else WARN,
          "RIT_DRY_RUN=true (bots send nothing)" if dry
          else "RIT_DRY_RUN=false - bots WILL send real orders",
          "" if dry else "set RIT_DRY_RUN=true in .env until you have watched a dry run")

    # ------------------------------------------------------------------ wrap
    print("=" * 62)
    if problems:
        print(f"\n{BOLD}Fix these, in order:{RESET}")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print()
        return 1

    print(f"\n{BOLD}All checks passed.{RESET} Next:")
    print("  python tools/monitor.py --case fi2        # read-only edge dashboard")
    print("  python bots/fi2_bond_arb.py --dry-run --verbose")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
