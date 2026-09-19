"""
Parse RIT news items into EPS updates for EV1.

The whole edge in EV1 is reacting to an earnings headline before the humans in
the room finish reading it. RIT news text is not a fixed contract, so this
parser is deliberately loose: find quarter references, find dollar amounts,
pair them up, and decide actual-vs-estimate from the verbs used.

If a headline shape shows up that this misses, do NOT fight the regex during the
case - drop a line into eps_override.json and the bot picks it up next loop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# "Q1", "Q 1", "quarter 1", "first quarter", "1Q"
# Each entry yields (quarter, span_of_whole_match) so the digit that IS the
# quarter number can never be mistaken for an EPS figure.
_QUARTER_PATTERNS = [
    (re.compile(r"\bQ\s?([1-4])\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b([1-4])\s?Q\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\bquarter\s+([1-4])\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.I),
     lambda m: {"first": 1, "second": 2, "third": 3, "fourth": 4}[m.group(1).lower()]),
]

# EPS in this case is always quoted to two decimals ("$0.42"). Match that shape
# first; the looser form is only a fallback so a one-decimal quote still works.
_EPS_RE = re.compile(r"-?\$?\s*(-?\d\.\d{1,2})\b")

# Spans that must never be read as an EPS figure.
_PERCENT_RE = re.compile(r"\s*(%|percent)", re.I)
_SCALE_RE = re.compile(r"\s*(billion|million|thousand|bn|mm|m\b|k\b)", re.I)
_LOSS_RE = re.compile(r"\b(loss|lost|negative|deficit)\b", re.I)
_EPS_CONTEXT_RE = re.compile(r"(per share|earnings per share|\bEPS\b)", re.I)

# Words that mean "this number is now a fact", not a forecast.
_ACTUAL_WORDS = re.compile(
    r"\b(announc\w*|report\w*|releas\w*|actual\w*|post\w*|realiz\w*|realis\w*"
    r"|came in|results|earnings|beat\w*|miss\w*|declar\w*)\b", re.I
)
# "expect\w*" was here and matched "expectations", which appears in almost every
# earnings headline ("Q2 results beat expectations") and flipped reported actuals
# to estimates. Only forward-looking "expects to" counts now.
_ESTIMATE_WORDS = re.compile(
    r"\b(estimat\w*|analyst\w*|forecast\w*|expects? to|revis\w*"
    r"|guidance|project\w*|outlook|consensus)\b", re.I
)
# An earnings release says so in the headline. That is decisive - the body of a
# release almost always also mentions the consensus it beat or missed.
_ACTUAL_HEADLINE_RE = re.compile(
    r"\b(announc\w*|report\w*|releas\w*|results|earnings|posts?|declar\w*)\b", re.I
)


def _classify_actual(headline: str, body: str) -> bool:
    """
    Actual or estimate? The headline is the reliable signal - an earnings release
    says "announces/reports Q3 earnings" while an analyst item says
    "estimates revised". Bodies often mention both ("came in at $0.19, missing
    the consensus of $0.27"), so the headline gets 3x the weight.
    """
    # A headline that announces earnings is an actual, whatever the body says.
    if _ACTUAL_HEADLINE_RE.search(headline) and not re.search(
            r"\b(estimat\w*|analyst\w*|forecast\w*|revis\w*|outlook|guidance)\b",
            headline, re.I):
        return True

    score = 0
    for text, weight in ((headline, 3), (body, 1)):
        score += weight * len(_ACTUAL_WORDS.findall(text))
        score -= weight * len(_ESTIMATE_WORDS.findall(text))
    return score > 0


def _find_quarters(text: str) -> list[tuple[int, int]]:
    """[(char_position, quarter)] sorted by position."""
    hits: list[tuple[int, int]] = []
    for pattern, extract in _QUARTER_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((m.start(), extract(m)))
    hits.sort()
    # de-duplicate overlapping matches of the same quarter
    out: list[tuple[int, int]] = []
    for pos, q in hits:
        if out and pos - out[-1][0] < 3 and out[-1][1] == q:
            continue
        out.append((pos, q))
    return out


# Quarterly EPS for this company runs ~0.18-0.44. Anything outside this band is
# a percentage, a revenue figure, a P/E or a quarter number - never an EPS.
EPS_MIN, EPS_MAX = -1.0, 1.5


def _plausible_eps(value: float) -> bool:
    """Reject years, percentages, revenue figures, P/E ratios and share counts."""
    return EPS_MIN < value < EPS_MAX


def _quarter_spans(text: str) -> list[tuple[int, int, int]]:
    """[(start, end, quarter)] of every quarter mention, sorted, de-duplicated."""
    hits: list[tuple[int, int, int]] = []
    for pattern, extract in _QUARTER_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((m.start(), m.end(), extract(m)))
    hits.sort()
    out: list[tuple[int, int, int]] = []
    for start, end, q in hits:
        if out and start < out[-1][1]:        # overlapping match of the same thing
            continue
        out.append((start, end, q))
    return out


def _eps_candidates(text: str, lo: int, hi: int) -> list[tuple[int, float]]:
    """
    EPS-shaped numbers inside text[lo:hi], with the traps removed:
      - a figure followed by % or "percent"      ("rose 3% to $0.44")
      - a figure followed by billion/million/... ("revenue was $1.20 billion")
      - anything outside the plausible EPS band
    A nearby "loss"/"negative" flips the sign.
    """
    out: list[tuple[int, float]] = []
    for m in _EPS_RE.finditer(text, lo, hi):
        tail = text[m.end():m.end() + 12]
        if _PERCENT_RE.match(tail) or _SCALE_RE.match(tail):
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if not _plausible_eps(value):
            continue
        if value > 0 and _LOSS_RE.search(text[max(0, m.start() - 40):m.start()]):
            value = -value
        out.append((m.start(), value))
    return out


def _pick(candidates: list[tuple[int, float]], text: str) -> float | None:
    """
    One EPS per quarter. When a segment holds several figures - "EPS came in at
    $0.19, missing the consensus of $0.27" - prefer the one sitting closest to an
    EPS phrase, which is the reported number rather than the comparison.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    contexts = [m.start() for m in _EPS_CONTEXT_RE.finditer(text)]
    if not contexts:
        return candidates[0][1]
    return min(candidates, key=lambda c: min(abs(c[0] - ctx) for ctx in contexts))[1]


def parse_eps_news(headline: str, body: str = "") -> list[tuple[int, float, bool]]:
    """
    Return [(quarter, eps, is_actual)] found in a news item.

    Each quarter mention owns the text from the end of its own match up to the
    next quarter mention, so "Q2 $0.22, Q3 $0.29 and Q4 $0.31" resolves cleanly
    and the digit inside "quarter 4" can never be read as an EPS figure.
    """
    text = f"{headline}. {body}".strip()
    quarters = _quarter_spans(text)
    if not quarters:
        return []

    is_actual = _classify_actual(headline, body)
    results: dict[int, float] = {}

    for i, (start, end, q) in enumerate(quarters):
        stop = quarters[i + 1][0] if i + 1 < len(quarters) else len(text)
        value = _pick(_eps_candidates(text, end, stop), text)
        # "EPS of $0.42 for the first quarter" - the figure precedes its quarter.
        if value is None and i == 0 and start > 0:
            value = _pick(_eps_candidates(text, 0, start), text)
        if value is not None:
            results.setdefault(q, value)

    return [(q, v, is_actual) for q, v in sorted(results.items())]


_EARNINGS_HINT_RE = re.compile(r"\b(earnings|EPS|per share|quarter|Q[1-4])\b", re.I)


def apply_news(book, items: list[dict], seen: set[int]) -> tuple[list[str], list[str]]:
    """
    Feed a batch of /v1/news rows into an EPSBook.

    `seen` is mutated so each news_id is applied once. Returns
    (log_lines, unparsed_earnings_headlines).

    The second element is the important one: an item that mentions earnings but
    produced no EPS update means our fair value is now STALE while the rest of
    the room has repriced. Trading a stale fair value at full size is the single
    most expensive thing this bot can do, so the caller halts on it.
    """
    logs: list[str] = []
    unparsed: list[str] = []
    for item in sorted(items, key=lambda n: n.get("news_id", 0)):
        nid = item.get("news_id")
        if nid in seen:
            continue
        seen.add(nid)
        headline, body = item.get("headline", ""), item.get("body", "")
        updates = parse_eps_news(headline, body)
        if not updates:
            if _EARNINGS_HINT_RE.search(f"{headline} {body}"):
                logs.append(f"[news {nid}] !! UNPARSED EARNINGS ITEM: {headline[:90]}")
                unparsed.append(headline[:90])
            continue
        for quarter, value, is_actual in updates:
            if book.update(quarter, value, is_actual):
                logs.append(
                    f"[news {nid}] Q{quarter} -> {value:.2f}"
                    f"{'A' if is_actual else 'E'}  ({headline[:60]})"
                )
    return logs, unparsed


def load_overrides(path: str | Path, book) -> list[str]:
    """
    Manual escape hatch. Create eps_override.json next to the bot:
        {"1": [0.42, true], "2": [0.28, false]}
    i.e. quarter -> [eps, is_actual]. Edited values are applied every loop, so
    you can correct the bot mid-case without restarting it.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return ["[override] file is not valid JSON, ignoring"]

    applied = getattr(book, "_applied_overrides", None)
    if applied is None:
        applied = book._applied_overrides = {}

    logs = []
    for key, val in data.items():
        try:
            quarter = int(key)
            if isinstance(val, (int, float)):
                value, is_actual = float(val), False
            else:
                value, is_actual = float(val[0]), bool(val[1])
        except (ValueError, TypeError, IndexError):
            continue
        # A manual override always wins, including over a recorded actual, but
        # it is re-read every loop so only announce it when the file changes.
        book.actual[quarter] = False
        changed = book.update(quarter, value, is_actual)
        if changed and applied.get(quarter) != (value, is_actual):
            logs.append(f"[override] Q{quarter} -> {value:.2f}{'A' if is_actual else 'E'}")
        applied[quarter] = (value, is_actual)
    return logs


if __name__ == "__main__":
    samples = [
        ("Prandium Industries announces Q1 earnings", "Prandium reported earnings per share of $0.42 for the first quarter."),
        ("Analysts revise Q2 estimates", "Analysts now estimate Q2 EPS of 0.28, down from 0.24."),
        ("PI Q3 earnings release", "Q3 EPS came in at $0.19, missing the consensus of 0.27."),
        ("Analyst update", "Estimates for the third quarter are raised to $0.31 and the fourth quarter to $0.35."),
        ("Prandium Industries wins new contract", "No financial details were disclosed."),
        ("Q4 2024 outlook", "Management guidance for Q4 is $0.30 per share, with the comp P/E at 12.5."),
    ]
    for h, b in samples:
        print(f"{h!r:55} -> {parse_eps_news(h, b)}")
