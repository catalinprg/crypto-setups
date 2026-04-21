"""Chart-visual context extractor.

Turns the raw OHLC stream into the narrative features a human trader picks
up at a glance from the chart but a frozen level list can't express:

  1. Recent 1h bars window — lets the analyst describe the last few hours
     of price action ("3 consecutive green 1h bars with lower wicks = buyers
     defending").
  2. Current leg — "price is +X% from the swing low of $Y formed Z hours
     ago." Critical context a number doesn't convey.
  3. Recent swing cluster touches — scans the last ~120 × 1h bars for local
     swing lows/highs clustered within a price band, so the agent knows
     "$73,795 was tested twice in 4 days — double-bottom forming" without
     looking at a chart.
  4. BOS wick vs body quality — classifies the most recent break as either
     body-through (decisive) or wick-through (rejected on close) by testing
     the breaking bar's close against the prior swing high/low.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from src.types import OHLC


RECENT_BARS_N = 4  # last 4 × 1h bars cover the current tape narrative;
                   # beyond that, bar-level context is historical noise the
                   # chart already expressed via swings + clusters.
SWING_CLUSTER_LOOKBACK_BARS = 120        # last ~5 days of 1h data
SWING_CLUSTER_BAND_PCT = 0.5             # 0.5% price band to group touches
MIN_BARS_BETWEEN_SWINGS = 6              # filter adjacent micro-swings
BAR_CLASSIFICATION_MIN_RANGE = 1.0       # absolute; bars with <1 unit range get "flat"


def _bar_char(b: OHLC) -> dict:
    """Classify a bar: direction, body fraction, wick fractions.
    Body fraction = |close - open| / (high - low).
    Wick-heavy bars (body_pct < 0.35) signal indecision or rejection."""
    rng = b.high - b.low
    if rng < BAR_CLASSIFICATION_MIN_RANGE:
        return {
            "ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "direction": "flat", "body_pct": 0.0, "wick_top_pct": 0.0, "wick_bot_pct": 0.0,
        }
    body = abs(b.close - b.open)
    wick_top = b.high - max(b.open, b.close)
    wick_bot = min(b.open, b.close) - b.low
    return {
        "ts":            b.ts,
        "open":          round(b.open, 2),
        "high":          round(b.high, 2),
        "low":           round(b.low, 2),
        "close":         round(b.close, 2),
        "direction":     "green" if b.close > b.open else "red" if b.close < b.open else "flat",
        "body_pct":      round(body / rng, 2),
        "wick_top_pct":  round(wick_top / rng, 2),
        "wick_bot_pct":  round(wick_bot / rng, 2),
    }


def recent_bars(h1_bars: list[OHLC], n: int = RECENT_BARS_N) -> list[dict]:
    """Last `n` × 1h bars with character tags."""
    if not h1_bars:
        return []
    return [_bar_char(b) for b in h1_bars[-n:]]


def _find_local_extrema(h1_bars: list[OHLC], lookback: int, min_gap: int) -> tuple[list[tuple[int, float, int]], list[tuple[int, float, int]]]:
    """Simple 3-bar local min/max scanner over the last `lookback` bars.
    Returns (highs, lows) as lists of (ts, price, bar_index).
    `min_gap` filters adjacent pivots too close together."""
    if len(h1_bars) < 5:
        return [], []
    window = h1_bars[-lookback:] if len(h1_bars) > lookback else h1_bars
    highs: list[tuple[int, float, int]] = []
    lows: list[tuple[int, float, int]] = []
    for i in range(1, len(window) - 1):
        b = window[i]
        if b.high > window[i-1].high and b.high > window[i+1].high:
            highs.append((b.ts, b.high, i))
        if b.low < window[i-1].low and b.low < window[i+1].low:
            lows.append((b.ts, b.low, i))
    # Filter — keep only extrema at least min_gap apart from the prior one on same side
    def _thin(seq: list[tuple[int, float, int]]) -> list[tuple[int, float, int]]:
        out: list[tuple[int, float, int]] = []
        for item in seq:
            if not out or (item[2] - out[-1][2]) >= min_gap:
                out.append(item)
        return out
    return _thin(highs), _thin(lows)


MIN_LEG_PCT = 1.5   # ignore swings smaller than 1.5% (intrabar noise)


def current_leg(h1_bars: list[OHLC], current_price: float) -> dict:
    """Describes the current price leg from the most recent SIGNIFICANT swing
    low and swing high — swings smaller than MIN_LEG_PCT from their adjacent
    extreme are filtered as noise. Lets the analyst say "price is +3.3% from
    the $73,795 swing low formed 31h ago" without looking at a chart.

    Uses the same local-extrema scanner as swing_clusters but filters:
      - extrema must be in the last SWING_CLUSTER_LOOKBACK_BARS (~5 days)
      - each extremum must differ from its neighbour in the opposite
        direction by at least MIN_LEG_PCT, measured against the opposite
        extreme price.
    """
    if not h1_bars:
        return {"status": "unavailable", "reason": "no_bars"}
    highs, lows = _find_local_extrema(h1_bars, SWING_CLUSTER_LOOKBACK_BARS, MIN_BARS_BETWEEN_SWINGS)
    if not highs and not lows:
        return {"status": "unavailable", "reason": "no_extrema"}

    now_ms = int(time.time() * 1000)

    # Build merged chronological sequence of (kind, ts, price).
    merged: list[tuple[str, int, float]] = (
        [("H", t, p) for t, p, _ in highs] + [("L", t, p) for t, p, _ in lows]
    )
    merged.sort(key=lambda x: x[1])

    # Significant swings: walk forward, keep only extrema that moved > MIN_LEG_PCT
    # from the last kept opposite extreme.
    significant: list[tuple[str, int, float]] = []
    for kind, ts, price in merged:
        if not significant:
            significant.append((kind, ts, price))
            continue
        last = significant[-1]
        if kind == last[0]:
            # Same kind — keep the more extreme one.
            if (kind == "H" and price > last[2]) or (kind == "L" and price < last[2]):
                significant[-1] = (kind, ts, price)
        else:
            # Opposite kind — require sufficient leg size vs last opposite.
            delta_pct = abs(price - last[2]) / last[2] * 100
            if delta_pct >= MIN_LEG_PCT:
                significant.append((kind, ts, price))

    sig_highs = [(ts, p) for k, ts, p in significant if k == "H"]
    sig_lows = [(ts, p) for k, ts, p in significant if k == "L"]

    def _hrs(ts: int | None) -> int | None:
        return int((now_ms - ts) / 3_600_000) if ts is not None else None

    last_low = sig_lows[-1] if sig_lows else None
    last_high = sig_highs[-1] if sig_highs else None

    from_low_pct = round((current_price - last_low[1]) / last_low[1] * 100, 2) if last_low else None
    from_high_pct = round((last_high[1] - current_price) / last_high[1] * 100, 2) if last_high else None

    leg_dir = None
    if last_low and last_high:
        leg_dir = "up_from_low" if last_low[0] > last_high[0] else "down_from_high"
    elif last_low:
        leg_dir = "up_from_low"
    elif last_high:
        leg_dir = "down_from_high"

    return {
        "status":              "ok",
        "recent_swing_low":    {"price": round(last_low[1], 2), "hours_ago": _hrs(last_low[0])} if last_low else None,
        "recent_swing_high":   {"price": round(last_high[1], 2), "hours_ago": _hrs(last_high[0])} if last_high else None,
        "pct_from_low":        from_low_pct,
        "pct_from_high":       from_high_pct,
        "leg_direction":       leg_dir,
        "min_leg_pct":         MIN_LEG_PCT,
    }


def recent_swing_clusters(h1_bars: list[OHLC]) -> dict:
    """Group recent local extrema into clusters by price proximity. Surfaces
    "tested levels" — a price that has been swept twice or more in the last
    ~5 days is a real double/triple bottom or top, even if the structural
    pool layer rounded it into a different cluster.
    """
    if not h1_bars:
        return {"status": "unavailable", "reason": "no_bars"}
    highs, lows = _find_local_extrema(h1_bars, SWING_CLUSTER_LOOKBACK_BARS, MIN_BARS_BETWEEN_SWINGS)
    now_ms = int(time.time() * 1000)

    def _cluster(seq: list[tuple[int, float, int]]) -> list[dict]:
        seq_by_price = sorted(seq, key=lambda x: x[1])
        clusters: list[list[tuple[int, float, int]]] = []
        for item in seq_by_price:
            if not clusters:
                clusters.append([item])
                continue
            ref = clusters[-1][0][1]   # anchor = first pivot in cluster
            if abs(item[1] - ref) / ref * 100 <= SWING_CLUSTER_BAND_PCT:
                clusters[-1].append(item)
            else:
                clusters.append([item])
        out = []
        for c in clusters:
            if len(c) < 2:                  # only surface multi-touch clusters
                continue
            ts_list = [x[0] for x in c]
            prices = [x[1] for x in c]
            out.append({
                "price_mean":        round(sum(prices) / len(prices), 2),
                "touches":           len(c),
                "most_recent_hours": int((now_ms - max(ts_list)) / 3_600_000),
            })
        out.sort(key=lambda c: c["touches"], reverse=True)
        return out[:5]

    return {
        "status":           "ok",
        "high_clusters":    _cluster(highs),
        "low_clusters":     _cluster(lows),
        "lookback_bars":    min(len(h1_bars), SWING_CLUSTER_LOOKBACK_BARS),
    }


def classify_bos_quality(market_structure: dict, ohlc_by_tf: dict) -> dict:
    """For the last_bos on each TF, classify whether the break was achieved
    by a body close through the PRIOR swing extreme, or only by a wick that
    closed back below. Pure wick BOS are flagged — the market rejected the
    break on close, so the structural implication is weaker.

    Algorithm:
      - `last_bos` reports the pivot bar's ts and its high (bullish) or low
        (bearish). That pivot is the NEW HH / LL. Scan earlier bars for the
        PRIOR local swing extreme (the one that was taken out).
      - On the pivot bar, test: did `close` exceed the prior extreme (body
        break) or did only `high` exceed it while `close` stayed below
        (wick break)?

    Returns: {tf: {"quality": "body" | "wick", "prior_extreme": float,
                   "pivot_close": float, "delta_from_prior": float} | null}
    """
    out: dict[str, dict | None] = {}
    for tf, ms in (market_structure or {}).items():
        bos = ms.get("last_bos") if isinstance(ms, dict) else getattr(ms, "last_bos", None)
        if not bos:
            out[tf] = None
            continue
        bars = ohlc_by_tf.get(tf) or []
        if not bars:
            out[tf] = None
            continue
        bos_ts = bos.get("ts")
        bos_dir = bos.get("direction")
        if bos_ts is None:
            out[tf] = None
            continue
        pivot_bar = next((b for b in bars if b.ts == bos_ts), None)
        if pivot_bar is None:
            out[tf] = None
            continue
        try:
            pivot_idx = bars.index(pivot_bar)
        except ValueError:
            out[tf] = None
            continue
        # Find the prior local extreme (HH to break for bullish / LL for bearish).
        window = bars[max(0, pivot_idx - 40):pivot_idx]
        if not window:
            out[tf] = None
            continue
        if bos_dir == "bullish":
            prior_extreme = max(b.high for b in window)
            quality = "body" if pivot_bar.close > prior_extreme else "wick"
        else:  # bearish
            prior_extreme = min(b.low for b in window)
            quality = "body" if pivot_bar.close < prior_extreme else "wick"
        # pivot_high / pivot_low / delta_from_prior were never cited in
        # briefings — the analyst uses quality (body|wick) + prior_extreme
        # (the "real" resistance/support when wick-only) and no more.
        out[tf] = {
            "quality":       quality,
            "prior_extreme": round(prior_extreme, 2),
        }
    return out
