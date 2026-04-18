"""Anchored VWAP with volume-weighted standard deviation bands.

VWAP(t) from anchor A = Σ(typical_price_i * volume_i) / Σ(volume_i) for i>=A.
Bands use the volume-weighted variance:
    var(t) = Σ(vol_i * (typ_i - vwap_t)^2) / Σ(vol_i).

Anchors emitted per asset (from `resolve_anchors`):
  - AVWAP_SESSION: last UTC-day open
  - AVWAP_WEEK:    last Monday 00:00 UTC
  - AVWAP_MONTH:   last 1st-of-month 00:00 UTC
  - AVWAP_SWING_HH: most recent significant swing high pivot
  - AVWAP_SWING_LL: most recent significant swing low pivot
  - AVWAP_EVENT:   fixed-list events (halving 2024-04-20, spot ETF 2024-01-10)
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.types import OHLC, SwingPair

# Fixed event anchors (Unix ms UTC). Extend cautiously — each entry becomes
# an AVWAP line on every chart.
_HALVING_2024_TS  = int(datetime(2024, 4, 20, tzinfo=timezone.utc).timestamp() * 1000)
_SPOT_ETF_2024_TS = int(datetime(2024, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)

EVENT_ANCHORS: list[tuple[str, int]] = [
    ("halving_2024",  _HALVING_2024_TS),
    ("spot_etf_2024", _SPOT_ETF_2024_TS),
]


@dataclass(frozen=True)
class AnchoredVwap:
    anchor_type: str       # LevelSource value (AVWAP_SESSION, AVWAP_WEEK, …)
    anchor_ts: int
    vwap: list[float]      # same length as input bars; pre-anchor entries = NaN
    upper_1sd: list[float]
    lower_1sd: list[float]
    upper_2sd: list[float]
    lower_2sd: list[float]


def compute_avwap(
    bars: list[OHLC], *, anchor_idx: int, anchor_type: str, anchor_ts: int,
) -> AnchoredVwap:
    """Compute AVWAP + ±1σ and ±2σ bands from `anchor_idx` onward."""
    n = len(bars)
    vwap    = [math.nan] * n
    upper_1 = [math.nan] * n
    lower_1 = [math.nan] * n
    upper_2 = [math.nan] * n
    lower_2 = [math.nan] * n

    cum_pv  = 0.0
    cum_v   = 0.0
    cum_pv2 = 0.0   # Σ(v * typ^2), enables variance via E[X^2] - E[X]^2

    for i in range(anchor_idx, n):
        b = bars[i]
        typ = (b.high + b.low + b.close) / 3.0
        v = b.volume
        cum_pv  += typ * v
        cum_v   += v
        cum_pv2 += typ * typ * v
        if cum_v > 0:
            mean = cum_pv / cum_v
            var  = max(0.0, (cum_pv2 / cum_v) - (mean * mean))
            sd   = math.sqrt(var)
            vwap[i]    = mean
            upper_1[i] = mean + sd
            lower_1[i] = mean - sd
            upper_2[i] = mean + 2 * sd
            lower_2[i] = mean - 2 * sd

    return AnchoredVwap(
        anchor_type=anchor_type, anchor_ts=anchor_ts,
        vwap=vwap, upper_1sd=upper_1, lower_1sd=lower_1,
        upper_2sd=upper_2, lower_2sd=lower_2,
    )


def _find_idx_for_ts(bars: list[OHLC], target_ts: int) -> int | None:
    """First bar whose timestamp >= target_ts. None if all bars are before."""
    for i, b in enumerate(bars):
        if b.ts >= target_ts:
            return i
    return None


def _session_start_ts(last_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp() * 1000)


def _week_start_ts(last_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    monday = (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return int(monday.timestamp() * 1000)


def _month_start_ts(last_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def resolve_anchors(
    bars: list[OHLC], swing_pairs: list[SwingPair],
) -> list[tuple[str, int, int]]:
    """Return list of (anchor_type, anchor_idx, anchor_ts). Skips anchors
    that fall outside the bar window (too-old anchor has no coverage)."""
    if not bars:
        return []
    out: list[tuple[str, int, int]] = []
    last_ts = bars[-1].ts

    for anchor_type, ts in [
        ("AVWAP_SESSION", _session_start_ts(last_ts)),
        ("AVWAP_WEEK",    _week_start_ts(last_ts)),
        ("AVWAP_MONTH",   _month_start_ts(last_ts)),
    ]:
        idx = _find_idx_for_ts(bars, ts)
        if idx is not None:
            out.append((anchor_type, idx, bars[idx].ts))

    # Swing anchors — take the most recent HH and most recent LL
    if swing_pairs:
        latest = swing_pairs[-1]
        idx_hh = _find_idx_for_ts(bars, latest.high_ts)
        if idx_hh is not None:
            out.append(("AVWAP_SWING_HH", idx_hh, bars[idx_hh].ts))
        idx_ll = _find_idx_for_ts(bars, latest.low_ts)
        if idx_ll is not None:
            out.append(("AVWAP_SWING_LL", idx_ll, bars[idx_ll].ts))

    # Event anchors (fixed list)
    for _, event_ts in EVENT_ANCHORS:
        idx = _find_idx_for_ts(bars, event_ts)
        if idx is not None:
            out.append(("AVWAP_EVENT", idx, bars[idx].ts))

    return out
