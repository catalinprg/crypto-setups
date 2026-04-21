"""Session highs/lows and time-since-event tags.

Session boundaries (UTC):
  Asia    00:00 - 07:00
  London  07:00 - 12:00
  NY      12:00 - 21:00

Crypto trades 24/7, but institutional liquidity follows these session clocks.
The session high/low becomes an intraday liquidity pool — pros hunt stops
just beyond the session extreme.

Time-since-event turns frozen `ts` fields (BOS, CHoCH, swing pivots) into
hours-ago freshness tags so the analyst can distinguish stale structure from
fresh triggers.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal

from src.types import OHLC

SessionName = Literal["asia", "london", "ny"]

SESSION_BOUNDS_UTC: dict[SessionName, tuple[int, int]] = {
    "asia":   (0, 7),
    "london": (7, 12),
    "ny":     (12, 21),
}


def _ts_to_utc_hour(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour


def _ts_to_utc_date(ts_ms: int) -> tuple[int, int, int]:
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return (d.year, d.month, d.day)


def _session_of(ts_ms: int) -> SessionName | None:
    h = _ts_to_utc_hour(ts_ms)
    for name, (lo, hi) in SESSION_BOUNDS_UTC.items():
        if lo <= h < hi:
            return name
    return None


def current_session(now_ts_ms: int | None = None) -> SessionName | None:
    """Return the session the given moment falls in, or None if outside
    any session window (21:00 - 24:00 UTC = late NY wind-down → None)."""
    if now_ts_ms is None:
        now_ts_ms = int(time.time() * 1000)
    return _session_of(now_ts_ms)


def session_extremes(h1_bars: list[OHLC]) -> dict:
    """Compute session high/low for the CURRENT session and the PRIOR session
    (same session, previous UTC day). Returns only sessions for which we have
    at least one bar. Output uses ms timestamps for consistency with the rest
    of the payload.

    Result schema:
      {
        "current": {
          "session":   "london",
          "high":      76800.0,
          "low":       75900.0,
          "start_ts":  <ms>,
          "bar_count": 3
        },
        "prior": {
          "session":   "london",
          "high":      ...,
          "low":       ...,
          "date_utc":  "2026-04-20",
          "bar_count": 5
        }
      }
    If bars are empty, returns {"current": None, "prior": None}.
    """
    if not h1_bars:
        return {"current": None, "prior": None}

    last_ts = h1_bars[-1].ts
    now_session = _session_of(last_ts)
    now_date = _ts_to_utc_date(last_ts)

    # Current session: all bars with same date + same session as the newest bar.
    if now_session is None:
        current = None
    else:
        lo_h, hi_h = SESSION_BOUNDS_UTC[now_session]
        cur_bars = [
            b for b in h1_bars
            if _ts_to_utc_date(b.ts) == now_date
            and lo_h <= _ts_to_utc_hour(b.ts) < hi_h
        ]
        if cur_bars:
            current = {
                "session":   now_session,
                "high":      round(max(b.high for b in cur_bars), 2),
                "low":       round(min(b.low for b in cur_bars), 2),
                "start_ts":  cur_bars[0].ts,
                "bar_count": len(cur_bars),
            }
        else:
            current = None

    # Prior session: same session, previous UTC day.
    prior = None
    # Find a target session for "prior" — typically the most recent *completed*
    # session with the same name. If current session is None, use the last
    # session that appears in the bars.
    target_session = now_session
    if target_session is None:
        # Walk back to find the last session we were in.
        for b in reversed(h1_bars):
            s = _session_of(b.ts)
            if s is not None:
                target_session = s
                break

    if target_session is not None:
        lo_h, hi_h = SESSION_BOUNDS_UTC[target_session]
        # Find all bars in that session, group by date, pick the newest *prior*
        # date (strictly before now_date when current session is live).
        by_date: dict[tuple[int, int, int], list[OHLC]] = {}
        for b in h1_bars:
            if lo_h <= _ts_to_utc_hour(b.ts) < hi_h:
                by_date.setdefault(_ts_to_utc_date(b.ts), []).append(b)
        candidate_dates = sorted(by_date.keys())
        # Exclude the current session's date (already covered by `current`)
        candidate_dates = [d for d in candidate_dates if d < now_date]
        if candidate_dates:
            prior_date = candidate_dates[-1]
            pb = by_date[prior_date]
            prior = {
                "session":   target_session,
                "high":      round(max(b.high for b in pb), 2),
                "low":       round(min(b.low for b in pb), 2),
                "date_utc":  f"{prior_date[0]:04d}-{prior_date[1]:02d}-{prior_date[2]:02d}",
                "bar_count": len(pb),
            }

    return {"current": current, "prior": prior}


def _hours_since(ts: int | None, now_ms: int) -> int | None:
    """Return hours since the given timestamp. Accepts ms or seconds (magnitude-
    detected). Returns None if ts is None."""
    if ts is None:
        return None
    # Detect unit — anything > 10**12 is milliseconds.
    ts_ms = ts if ts > 10**12 else ts * 1000
    delta_s = (now_ms - ts_ms) / 1000
    if delta_s < 0:
        return 0
    return int(delta_s // 3600)


def time_since_events(
    market_structure: dict,
    liquidity_pools: dict,
    now_ms: int | None = None,
) -> dict:
    """Extract the freshest structural-event timestamps and return
    hours-since-now. Lets the analyst tell fresh triggers from stale levels.

    Output schema:
      {
        "last_bos_hours":   {"1M": 124, "4h": 2, ...},     # None when absent
        "last_choch_hours": {"1d": 38, ...},
        "last_bsl_pool_touch_hours": 10,     # min age across buy-side pools
        "last_ssl_pool_touch_hours": 30
      }
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    out: dict = {"last_bos_hours": {}, "last_choch_hours": {}}

    for tf, ms in (market_structure or {}).items():
        # `ms` here is the per-TF structure analysis object (StructureAnalysis),
        # not yet serialized. Access attrs defensively — upstream has dict or obj.
        bos = getattr(ms, "last_bos", None) if not isinstance(ms, dict) else ms.get("last_bos")
        choch = getattr(ms, "last_choch", None) if not isinstance(ms, dict) else ms.get("last_choch")
        if bos:
            out["last_bos_hours"][tf] = _hours_since(bos.get("ts"), now_ms)
        if choch:
            out["last_choch_hours"][tf] = _hours_since(choch.get("ts"), now_ms)

    buy_pools = (liquidity_pools or {}).get("buy_side") or []
    sell_pools = (liquidity_pools or {}).get("sell_side") or []
    bsl_ages = [p.get("age_hours") for p in buy_pools if p.get("age_hours") is not None]
    ssl_ages = [p.get("age_hours") for p in sell_pools if p.get("age_hours") is not None]
    out["last_bsl_pool_touch_hours"] = min(bsl_ages) if bsl_ages else None
    out["last_ssl_pool_touch_hours"] = min(ssl_ages) if ssl_ages else None

    return out
