"""
Configuration from a .env file - zero dependencies, no pip install needed.

Copy .env.example to .env, fill in your API key, done. Everything (bots,
monitor, doctor, notebook) reads from here, so the key lives in exactly one
place and never gets pasted into a source file you might share or commit.

Precedence: real environment variables win over .env, so you can override a
single setting for one run without editing the file:

    RIT_PORT=9998 python tools/doctor.py
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
_loaded = False


def load_env(path: Path | str | None = None, override: bool = False) -> dict[str, str]:
    """
    Parse a .env file into os.environ. Handles `KEY=value`, `export KEY=value`,
    quoted values, inline `#` comments and blank lines. Missing file is fine.
    """
    global _loaded
    p = Path(path) if path else ENV_PATH
    found: dict[str, str] = {}
    if not p.exists():
        _loaded = True
        return found

    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]                       # quoted: keep as-is
        else:
            value = value.split(" #", 1)[0].strip()   # unquoted: strip comment
        if not key:
            continue
        found[key] = value
        if override or key not in os.environ:
            os.environ[key] = value

    _loaded = True
    return found


def _get(key: str, default: str = "") -> str:
    if not _loaded:
        load_env()
    return os.environ.get(key, default).strip()


def api_key() -> str:
    return _get("RIT_API_KEY")


def base_url() -> str:
    """
    Build the API base URL. Set RIT_URL directly for anything unusual;
    otherwise RIT_HOST + RIT_PORT are assembled for you.

    Local RIT client (the normal case):   http://localhost:9999/v1
    Competition server on the network:    RIT_HOST=10.0.0.5
    """
    explicit = _get("RIT_URL")
    if explicit:
        return explicit.rstrip("/")
    host = _get("RIT_HOST", "localhost")
    port = _get("RIT_PORT", "9999")
    scheme = "https" if _get("RIT_HTTPS", "false").lower() in ("1", "true", "yes") else "http"
    if "://" in host:                                  # user pasted a full URL into RIT_HOST
        scheme, _, host = host.partition("://")
    host = host.strip("/")
    return f"{scheme}://{host}:{port}/v1"


def setting(key: str, default, cast=None):
    """Read a tunable from .env with a fallback, e.g. setting('EV1_MAX_POS', 100000, int)."""
    raw = _get(key)
    if raw == "":
        return default
    if cast is None:
        cast = type(default) if default is not None else str
    try:
        if cast is bool:
            return raw.lower() in ("1", "true", "yes", "on")
        return cast(raw)
    except (TypeError, ValueError):
        return default


def describe() -> str:
    key = api_key()
    masked = f"{key[:4]}…{key[-2:]} ({len(key)} chars)" if len(key) > 6 else ("SET" if key else "MISSING")
    return f"url={base_url()}  api_key={masked}  env_file={'found' if ENV_PATH.exists() else 'MISSING'}"
