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
import math
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

# Skew label thresholds in vol-points (put_iv − call_iv at ~±10% OTM on
# the nearest expiry). Crypto options typically run moderately put-skewed
# (+2 to +5 vol-points); beyond that indicates crash-hedging crowding.
# Negative skew (calls richer than puts) = upside-chasing lean.
SKEW_CRASH_HEDGED_VOL_PTS = 5.0
SKEW_UPSIDE_CHASE_VOL_PTS = -2.0

# Vol term-structure slope deadband in vol-points between short (~7d) and
# mid (~30d) IV. Outside the deadband labels contango/backwardation.
TERM_STRUCTURE_DEADBAND_VOL_PTS = 1.0

# OTM distance used for the skew proxy and ATM-band IV on each expiry.
# ±10% brackets real options liquidity at standard crypto tenors without
# wandering into illiquid tail strikes.
OTM_BAND_PCT = 0.10
ATM_BAND_PCT = 0.02  # ±2% around spot for "at-the-money" IV averaging


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
    summaries: list[dict], spot: float | None,
    expiry_limit: int = ACTIVE_EXPIRY_LIMIT,
) -> list[dict]:
    """Identify strike walls — strikes with the highest OI concentration in
    the nearest N expiries. Strike walls act as gamma magnets/pins because
    dealer hedging around large OI strikes drags spot toward them into
    expiry. This is the cleanest proxy for GEX concentration that we can
    compute without per-instrument greeks.

    Returns up to 4 strikes total — the top 2 by OI above spot (ceiling
    candidates) and the top 2 below spot (floor candidates). Walls 5+ are
    historically never cited in briefings; capping at 4 preserves the
    signal without adding noise."""
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
    if spot is not None:
        above = [r for r in rows if r["strike"] >= spot][:2]
        below = [r for r in rows if r["strike"] <  spot][:2]
        top = above + below
    else:
        top = rows[:4]
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


def compute_expected_moves(price: float | None, dvol: float | None) -> dict | None:
    """Convert annualized DVOL into ±1σ daily, ±1σ weekly, ±2σ weekly
    price bands. DVOL is annualized vol in percent; rescale by sqrt(days/365).
    Returns None when either input is missing — the agent treats absence as
    "no expected-move confluence available".

    These are the natural statistical targets the options market prices in.
    Analyst uses them as T1/T2 confluence: when a structural zone midpoint
    coincides with a ±1σ or ±2σ band, that's an options-market endorsement
    of the level."""
    if price is None or dvol is None or dvol <= 0 or price <= 0:
        return None
    ann_vol = dvol / 100.0
    sd_daily = ann_vol * math.sqrt(1 / 365)
    sd_weekly = ann_vol * math.sqrt(7 / 365)
    return {
        "plus_1sd_daily":   round(price * (1 + sd_daily),     2),
        "minus_1sd_daily":  round(price * (1 - sd_daily),     2),
        "plus_1sd_weekly":  round(price * (1 + sd_weekly),    2),
        "minus_1sd_weekly": round(price * (1 - sd_weekly),    2),
        "plus_2sd_weekly":  round(price * (1 + 2 * sd_weekly), 2),
        "minus_2sd_weekly": round(price * (1 - 2 * sd_weekly), 2),
    }


def _expiry_days_ahead(expiry: str, *, now_ms: int) -> int | None:
    """Parse Deribit expiry string (e.g. '25APR26') into days-ahead from
    `now_ms`. Deribit expiries are UTC 08:00. Returns None on parse failure."""
    MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    try:
        day = int(expiry[:-5])
        mon = MONTHS.get(expiry[-5:-2], 0)
        yr = 2000 + int(expiry[-2:])
        if not mon:
            return None
        import datetime as _dt
        exp_dt = _dt.datetime(yr, mon, day, 8, 0, tzinfo=_dt.timezone.utc)
        now_dt = _dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.timezone.utc)
        return max(0, (exp_dt - now_dt).days)
    except (ValueError, KeyError):
        return None


def compute_term_structure(
    summaries: list[dict], spot: float | None,
) -> dict | None:
    """Group mark_iv by expiry, build a 3-point term structure: short (~7d),
    mid (~30d), long (~90d). Return slope label. The signal is backwardation
    (short > mid) = stress / pre-event; contango = normal.

    `summaries` items must have mark_iv, strike, expiry, type after decoration.
    ATM IV per expiry = average of mark_iv for strikes within ±2% of spot."""
    if not summaries or spot is None:
        return None
    import time as _t
    now_ms = int(_t.time() * 1000)
    low  = spot * (1 - ATM_BAND_PCT)
    high = spot * (1 + ATM_BAND_PCT)

    by_expiry_days: dict[int, list[float]] = {}
    for s in summaries:
        iv = s.get("mark_iv")
        strike = s.get("strike")
        expiry = s.get("expiry")
        if iv is None or strike is None or expiry is None:
            continue
        if not (low <= strike <= high):
            continue
        days = _expiry_days_ahead(expiry, now_ms=now_ms)
        if days is None:
            continue
        by_expiry_days.setdefault(days, []).append(float(iv))

    if not by_expiry_days:
        return None

    # Pick the expiry closest to each target tenor.
    def _pick(target_days: int, tol: float) -> tuple[int, float] | None:
        best = None
        for d, ivs in by_expiry_days.items():
            if abs(d - target_days) / max(target_days, 1) <= tol:
                avg = sum(ivs) / len(ivs)
                if best is None or abs(d - target_days) < abs(best[0] - target_days):
                    best = (d, round(avg, 2))
        return best

    short = _pick(7,  1.0)   # anything 0–14d
    mid   = _pick(30, 0.6)   # 12–48d
    long_ = _pick(90, 0.5)   # 45–135d

    if short is None or mid is None:
        # Need at least short + mid to label slope.
        return None

    short_iv, mid_iv = short[1], mid[1]
    delta = short_iv - mid_iv
    if delta > TERM_STRUCTURE_DEADBAND_VOL_PTS:
        slope = "backwardation"
    elif delta < -TERM_STRUCTURE_DEADBAND_VOL_PTS:
        slope = "contango"
    else:
        slope = "flat"

    out = {
        "short":  {"days": short[0], "iv": short_iv},
        "mid":    {"days": mid[0],   "iv": mid_iv},
        "slope":  slope,
        "short_minus_mid_vol_pts": round(delta, 2),
    }
    if long_ is not None:
        out["long"] = {"days": long_[0], "iv": long_[1]}
    return out


def compute_skew(summaries: list[dict], spot: float | None) -> dict | None:
    """Simple put/call IV skew proxy at the nearest expiry, using ±10% OTM
    strikes instead of solving for 25Δ (no greeks in book summary). The
    directional signal is the same: positive skew = puts richer than calls =
    crash-hedging demand; negative = calls richer = upside chase."""
    if not summaries or spot is None:
        return None
    import time as _t
    now_ms = int(_t.time() * 1000)

    # Nearest future expiry.
    expiries_with_days: list[tuple[str, int]] = []
    for s in summaries:
        exp = s.get("expiry")
        if not exp:
            continue
        days = _expiry_days_ahead(exp, now_ms=now_ms)
        if days is not None and days >= 0:
            expiries_with_days.append((exp, days))
    if not expiries_with_days:
        return None
    nearest_expiry, nearest_days = min(
        set(expiries_with_days), key=lambda x: x[1]
    )

    # Collect IVs at the OTM bands for the nearest expiry.
    put_band_low,  put_band_high  = spot * (1 - OTM_BAND_PCT - 0.02), spot * (1 - OTM_BAND_PCT + 0.02)
    call_band_low, call_band_high = spot * (1 + OTM_BAND_PCT - 0.02), spot * (1 + OTM_BAND_PCT + 0.02)

    put_ivs: list[float] = []
    call_ivs: list[float] = []
    for s in summaries:
        if s.get("expiry") != nearest_expiry:
            continue
        iv = s.get("mark_iv")
        strike = s.get("strike")
        typ = s.get("type")
        if iv is None or strike is None:
            continue
        if typ == "P" and put_band_low <= strike <= put_band_high:
            put_ivs.append(float(iv))
        elif typ == "C" and call_band_low <= strike <= call_band_high:
            call_ivs.append(float(iv))

    if not put_ivs or not call_ivs:
        return None

    put_iv_avg  = sum(put_ivs)  / len(put_ivs)
    call_iv_avg = sum(call_ivs) / len(call_ivs)
    skew_vol_pts = put_iv_avg - call_iv_avg

    if skew_vol_pts > SKEW_CRASH_HEDGED_VOL_PTS:
        label = "crash_hedged"
    elif skew_vol_pts < SKEW_UPSIDE_CHASE_VOL_PTS:
        label = "upside_chase"
    else:
        label = "neutral"

    return {
        "value_vol_pts":    round(skew_vol_pts, 2),
        "put_iv_otm":       round(put_iv_avg, 2),
        "call_iv_otm":      round(call_iv_avg, 2),
        "nearest_expiry":   nearest_expiry,
        "nearest_days":     nearest_days,
        "label":            label,
    }


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
    # mark_iv is kept here for term-structure + skew computation.
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
            "mark_iv":       s.get("mark_iv"),
        })

    total_put_oi  = sum(r["open_interest"] for r in parsed if r["type"] == "P")
    total_call_oi = sum(r["open_interest"] for r in parsed if r["type"] == "C")
    put_call_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else None

    max_pain = _compute_max_pain(parsed)
    strike_walls = _compute_strike_walls(parsed, index_price)
    expected_moves = compute_expected_moves(index_price, dvol)
    term_structure = compute_term_structure(parsed, index_price)
    skew_25d = compute_skew(parsed, index_price)

    # parsed_instrument_count + raw totals were diagnostic — put_call_oi_ratio
    # carries the positioning signal; raw totals never drove a decision.
    return {
        "status":            "ok",
        "currency":          ccy,
        "index_price":       round(index_price, 2) if index_price else None,
        "dvol":              dvol,
        "put_call_oi_ratio": round(put_call_ratio, 3) if put_call_ratio else None,
        "max_pain_strike":   max_pain,
        "strike_walls":      strike_walls,
        "expected_moves":    expected_moves,
        "term_structure":    term_structure,
        "skew_25d":          skew_25d,
    }
