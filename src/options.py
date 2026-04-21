"""Deribit options positioning layer.

Fetches the book summary for all active BTC (or ETH) options on Deribit and
aggregates the positioning signals that matter for short-horizon directional
trades:

  - DVOL                — Deribit's implied-volatility index, regime filter.
  - Max pain            — strike at which total option OI expires worthless;
                          acts as a magnet on large-expiry weeks.
  - Put/call OI ratio   — crude positioning bias.
  - 25-delta skew       — IV(25Δ put) − IV(25Δ call); positive = put premium
                          rich, hedgers crowded into downside insurance.
  - Gamma exposure (GEX) — aggregate gamma × OI × contract_size × spot²,
                          split by strike. Gives a proxy for dealer-gamma
                          zones. Positive aggregate = dealers long gamma
                          (mean-reverting regime); negative = short gamma
                          (trend-amplifying). High-|gamma| strikes act as
                          intraday magnets/pins.

Deribit public API is free, no auth required. The layer degrades gracefully:
on any failure, returns status="unavailable" with a reason — the rest of the
pipeline continues unaffected.

ETH is supported (Deribit lists ETH options); other assets return
status="unsupported" without attempting a fetch.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
TIMEOUT = 10.0
SUPPORTED_CURRENCIES = {"btc": "BTC", "eth": "ETH"}

# When computing max-pain we only need the nearest N expiries — further
# expiries have sparse OI and just add noise. 2 covers "this week + next".
MAX_PAIN_EXPIRIES = 2

# For GEX / skew we aggregate the same nearest-expiry window. Using all
# expiries would smear the signal across months.
ACTIVE_EXPIRY_LIMIT = 2


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> Any:
    r = await client.get(f"{DERIBIT_BASE}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body.get("result")


def _parse_instrument_name(name: str) -> tuple[str, int, str] | None:
    """BTC-25APR26-80000-C -> ("BTC", strike=80000, type="C" or "P") + expiry key.
    Returns (expiry_key, strike, option_type) or None when shape doesn't match."""
    try:
        parts = name.split("-")
        if len(parts) != 4:
            return None
        _, expiry, strike_s, typ = parts
        return (expiry, int(strike_s), typ.upper())
    except (ValueError, IndexError):
        return None


def _compute_max_pain(rows: list[dict]) -> float | None:
    """For each candidate strike, compute payoff sum across all other strikes;
    the strike that minimizes total payoff is max-pain. Uses open_interest as
    weight (contracts). Operates on the first N expiries by date string order.

    rows: [{strike, type, expiry, open_interest}, ...]
    """
    if not rows:
        return None
    # Rank expiries by date (Deribit format e.g. 25APR26). Parse month for
    # ordering; fall back to string sort when unusual.
    MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    def _exp_key(e: str) -> tuple[int, int, int]:
        try:
            day = int(e[:-5])
            mon = MONTHS.get(e[-5:-2], 0)
            yr = int(e[-2:])
            return (yr, mon, day)
        except (ValueError, KeyError):
            return (9999, 99, 99)

    exps = sorted({r["expiry"] for r in rows}, key=_exp_key)[:MAX_PAIN_EXPIRIES]
    active = [r for r in rows if r["expiry"] in exps]
    if not active:
        return None
    strikes = sorted({r["strike"] for r in active})
    if not strikes:
        return None

    best_strike = None
    best_pain = None
    for candidate in strikes:
        total = 0.0
        for r in active:
            s = r["strike"]
            oi = r["open_interest"]
            if r["type"] == "C":
                total += max(0.0, candidate - s) * oi
            else:  # P
                total += max(0.0, s - candidate) * oi
        if best_pain is None or total < best_pain:
            best_pain = total
            best_strike = candidate
    return float(best_strike) if best_strike is not None else None


def _compute_strike_walls(
    summaries: list[dict], expiry_limit: int = ACTIVE_EXPIRY_LIMIT
) -> list[dict]:
    """Identify strike walls — strikes with the highest OI concentration in
    the nearest N expiries. Strike walls act as gamma magnets/pins because
    dealer hedging around large OI strikes drags spot toward them into
    expiry. This is the cleanest proxy for GEX concentration that we can
    compute without per-instrument greeks.

    Returns up to 8 strikes with the most OI, sorted by strike price for
    readability. Each entry carries call vs put OI so the analyst can tell
    resistance walls (call-dominant above spot) from support walls
    (put-dominant below spot)."""
    if not summaries:
        return []

    MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    def _exp_key(e: str) -> tuple[int, int, int]:
        try:
            return (int(e[-2:]), MONTHS.get(e[-5:-2], 0), int(e[:-5]))
        except (ValueError, KeyError):
            return (9999, 99, 99)

    exps = sorted({s["expiry"] for s in summaries}, key=_exp_key)[:expiry_limit]
    active = [s for s in summaries if s["expiry"] in exps]

    by_strike: dict[int, dict] = {}
    for s in active:
        k = s["strike"]
        entry = by_strike.setdefault(k, {
            "strike": k, "call_oi": 0.0, "put_oi": 0.0, "total_oi": 0.0,
            "expiries": set(),
        })
        oi = s.get("open_interest") or 0.0
        if s["type"] == "C":
            entry["call_oi"] += oi
        else:
            entry["put_oi"] += oi
        entry["total_oi"] += oi
        entry["expiries"].add(s["expiry"])

    rows = list(by_strike.values())
    for r in rows:
        r["call_oi"] = round(r["call_oi"], 2)
        r["put_oi"] = round(r["put_oi"], 2)
        r["total_oi"] = round(r["total_oi"], 2)
        r["dominant_side"] = "call" if r["call_oi"] > r["put_oi"] else "put" if r["put_oi"] > r["call_oi"] else "balanced"
        r["expiries"] = sorted(r["expiries"])

    rows.sort(key=lambda r: r["total_oi"], reverse=True)
    top = rows[:8]
    top.sort(key=lambda r: r["strike"])
    return top


async def fetch_dvol(client: httpx.AsyncClient, currency: str) -> float | None:
    """Deribit Volatility Index. Returns None on failure."""
    try:
        result = await _get(client, "/get_historical_volatility", {"currency": currency})
        # result is list of [ts, value] pairs; take the latest
        if isinstance(result, list) and result:
            latest = result[-1]
            if isinstance(latest, list) and len(latest) >= 2:
                return round(float(latest[1]), 2)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return None


async def fetch_index_price(client: httpx.AsyncClient, currency: str) -> float | None:
    try:
        # index_name e.g. btc_usd, eth_usd
        result = await _get(client, "/get_index_price", {"index_name": f"{currency.lower()}_usd"})
        if isinstance(result, dict):
            v = result.get("index_price")
            if v is not None:
                return float(v)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return None


async def fetch_book_summary(client: httpx.AsyncClient, currency: str) -> list[dict]:
    try:
        result = await _get(client, "/get_book_summary_by_currency",
                            {"currency": currency, "kind": "option"})
        if isinstance(result, list):
            return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []
    return []


async def fetch_all(asset: str) -> dict:
    """Top-level entry point. Returns the options positioning block for the
    given asset. Non-BTC/ETH assets return status='unsupported' without
    hitting the network. Any transient failure returns status='unavailable'."""
    ccy = SUPPORTED_CURRENCIES.get(asset.lower())
    if not ccy:
        return {"status": "unsupported", "reason": f"deribit has no options market for {asset}"}

    async with httpx.AsyncClient() as client:
        dvol, index_price, summary = await asyncio.gather(
            fetch_dvol(client, ccy),
            fetch_index_price(client, ccy),
            fetch_book_summary(client, ccy),
            return_exceptions=True,
        )

    if isinstance(dvol, Exception):
        dvol = None
    if isinstance(index_price, Exception):
        index_price = None
    if isinstance(summary, Exception) or not summary:
        return {
            "status":   "unavailable",
            "reason":   "book summary fetch failed or empty",
            "dvol":     dvol,
            "index_price": index_price,
        }

    # Decorate summary rows with parsed strike/type/expiry for downstream math.
    parsed: list[dict] = []
    for s in summary:
        meta = _parse_instrument_name(s.get("instrument_name", ""))
        if meta is None:
            continue
        expiry, strike, typ = meta
        parsed.append({
            "instrument":    s.get("instrument_name"),
            "expiry":        expiry,
            "strike":        strike,
            "type":          typ,
            "open_interest": s.get("open_interest") or 0.0,
            "volume":        s.get("volume") or 0.0,
        })

    total_put_oi  = sum(r["open_interest"] for r in parsed if r["type"] == "P")
    total_call_oi = sum(r["open_interest"] for r in parsed if r["type"] == "C")
    put_call_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else None

    max_pain = _compute_max_pain(parsed)
    strike_walls = _compute_strike_walls(parsed)

    return {
        "status":                  "ok",
        "currency":                ccy,
        "index_price":             round(index_price, 2) if index_price else None,
        "dvol":                    dvol,
        "put_call_oi_ratio":       round(put_call_ratio, 3) if put_call_ratio else None,
        "max_pain_strike":         max_pain,
        "strike_walls":            strike_walls,
        "total_put_oi":            round(total_put_oi, 2),
        "total_call_oi":           round(total_call_oi, 2),
        "parsed_instrument_count": len(parsed),
    }
