"""Liquidity-pool proxy layer derived from swing pivots.

Liquidity zones are standard ICT / SMC concepts: resting stops cluster above
swing highs (buy-side liquidity, "BSL") and below swing lows (sell-side,
"SSL"). Price is drawn toward unswept pools because that's where size can
get filled. Pools that have already been swept lose their pulling power.

We derive a proxy for these pools directly from the pivots the swings
module already detects — no extra data source needed.

**This is a separate layer from Fibonacci confluence, not a replacement.**
A fib zone answers "where is the structural math?"; a liquidity pool
answers "where are the stops?". They're orthogonal — their real value is
reinforcement on overlap.

Pool shape:

    {
        "price":           float,   # representative cluster price
        "price_range":     [min, max],
        "type":            "BSL" | "SSL",
        "touches":         int,     # number of swings contributing
        "tfs":             ["1d", "1w"],   # contributing TFs, sorted
        "most_recent_ts":  int,     # ms since epoch, of newest contributor
        "age_hours":       int,     # relative to now
        "swept":           bool,    # did price trade beyond since formation
        "distance_pct":    float,   # signed vs current_price (+ = above)
        "strength_score":  int,     # TF_WEIGHTS sum × touches
    }
"""
from __future__ import annotations

import time
from typing import Iterable

from src.types import OHLC, SwingPair, TF_WEIGHTS, Timeframe

# Same cluster radius multiplier as fib confluence — one coherent sense of
# "near-equal" across the whole briefing.
RADIUS_ATR_MULTIPLIER = 0.25
MAX_ZONE_WIDTH_MULTIPLIER = 2.0
# Drop pools beyond this distance from current price — same cap as fib zones.
MAX_POOL_DISTANCE_PCT = 0.20
# Cap the number of pools surfaced per side, post-filter. The agent only
# uses the top-ranked ones — anything beyond this is noise.
MAX_POOLS_PER_SIDE = 6


def _cluster_by_price(
    pivots: list[tuple[float, Timeframe, int]],
    radius: float,
) -> list[list[tuple[float, Timeframe, int]]]:
    """Cluster pivots by price using the fib-confluence merging rule: a
    pivot joins the current cluster only if it's within `radius` of the
    last member AND the total cluster width stays <= 2 * radius."""
    if not pivots:
        return []
    sorted_pivots = sorted(pivots, key=lambda x: x[0])
    clusters: list[list[tuple[float, Timeframe, int]]] = [[sorted_pivots[0]]]
    max_width = radius * MAX_ZONE_WIDTH_MULTIPLIER
    for p in sorted_pivots[1:]:
        within_radius = p[0] - clusters[-1][-1][0] <= radius
        within_width = p[0] - clusters[-1][0][0] <= max_width
        if within_radius and within_width:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters


def _pick_sweep_tf(
    ohlc: dict[Timeframe, list[OHLC]], pivot_ts_ms: int
) -> Timeframe | None:
    """Return the smallest-resolution TF whose OHLC window covers
    `pivot_ts_ms` — i.e. has bars from before the pivot all the way to
    present. None when no TF covers the pivot (pivot predates every TF's
    fetched window — we conservatively treat that pool as unswept)."""
    tf_order: list[Timeframe] = ["1h", "4h", "1d", "1w", "1M"]
    for tf in tf_order:
        bars = ohlc.get(tf)
        if bars and bars[0].ts <= pivot_ts_ms:
            return tf
    return None


def _is_swept(
    pivot_price: float,
    pivot_ts_ms: int,
    pool_type: str,
    ohlc: dict[Timeframe, list[OHLC]],
) -> bool:
    """For a BSL pool, "swept" means any subsequent bar high > pivot_price.
    For SSL, subsequent bar low < pivot_price. Uses the smallest-resolution
    TF whose window covers the pivot's timestamp — trades precision for
    coverage on old swings."""
    tf = _pick_sweep_tf(ohlc, pivot_ts_ms)
    if tf is None:
        return False
    bars = ohlc[tf]
    for b in bars:
        if b.ts <= pivot_ts_ms:
            continue
        if pool_type == "BSL" and b.high > pivot_price:
            return True
        if pool_type == "SSL" and b.low < pivot_price:
            return True
    return False


def _strength_score(tfs: list[Timeframe], touches: int) -> int:
    """TF-weight sum × touches. Pool with 2 contributors on 1w + 1d scores
    higher than 2 on 1h + 1h. Max_pairs=3 on swings + 5 TFs caps the upper
    range naturally."""
    return sum(TF_WEIGHTS.get(tf, 1) for tf in tfs) * touches


def compute_pools(
    swing_pairs: list[SwingPair],
    ohlc: dict[Timeframe, list[OHLC]],
    current_price: float,
    daily_atr: float,
    *,
    now_ms: int | None = None,
) -> dict[str, list[dict]]:
    """Derive liquidity pools from swing pivots.

    Returns {"buy_side": [...pools above], "sell_side": [...pools below]}.
    Pools are filtered to within MAX_POOL_DISTANCE_PCT of current_price.
    Unswept pools come first; within swept/unswept tiers, higher
    strength_score ranks first."""
    if not swing_pairs or current_price <= 0 or daily_atr <= 0:
        return {"buy_side": [], "sell_side": []}

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    radius = daily_atr * RADIUS_ATR_MULTIPLIER

    # Extract (price, tf, ts) tuples for highs and lows separately.
    highs: list[tuple[float, Timeframe, int]] = [
        (p.high_price, p.tf, p.high_ts) for p in swing_pairs
    ]
    lows: list[tuple[float, Timeframe, int]] = [
        (p.low_price, p.tf, p.low_ts) for p in swing_pairs
    ]

    buy_side = _build_pools(highs, "BSL", radius, ohlc, current_price, now_ms)
    sell_side = _build_pools(lows, "SSL", radius, ohlc, current_price, now_ms)

    return {
        "buy_side": buy_side[:MAX_POOLS_PER_SIDE],
        "sell_side": sell_side[:MAX_POOLS_PER_SIDE],
    }


def _build_pools(
    pivots: list[tuple[float, Timeframe, int]],
    pool_type: str,
    radius: float,
    ohlc: dict[Timeframe, list[OHLC]],
    current_price: float,
    now_ms: int,
) -> list[dict]:
    clusters = _cluster_by_price(pivots, radius)
    pools: list[dict] = []
    for cluster in clusters:
        prices = [p[0] for p in cluster]
        tfs = sorted({p[1] for p in cluster}, key=lambda tf: TF_WEIGHTS.get(tf, 1), reverse=True)
        tss = [p[2] for p in cluster]
        most_recent_ts = max(tss)

        # Representative price: for BSL, the top of the cluster (stops sit
        # ABOVE the pool's upper edge most often). For SSL, bottom of the
        # cluster. Agent uses `price_range` to show the band.
        rep_price = max(prices) if pool_type == "BSL" else min(prices)
        price_range = [round(min(prices), 6), round(max(prices), 6)]

        # Swept if ANY contributing pivot has been breached. Using the most
        # recent pivot's price as the "upper lip" of the pool; if that's
        # been taken, the pool is spent.
        ref_price = rep_price
        ref_ts = most_recent_ts
        swept = _is_swept(ref_price, ref_ts, pool_type, ohlc)

        distance_pct = (rep_price - current_price) / current_price * 100
        if abs(distance_pct) > MAX_POOL_DISTANCE_PCT * 100:
            continue

        # Side filter — BSL must be above price, SSL below. Pivots that
        # crossed (e.g. an old swing high now BELOW current price) are
        # already swept and uninteresting.
        if pool_type == "BSL" and rep_price <= current_price:
            continue
        if pool_type == "SSL" and rep_price >= current_price:
            continue

        age_hours = max(0, int((now_ms - most_recent_ts) / 3_600_000))
        touches = len(cluster)
        strength = _strength_score(tfs, touches)

        pools.append({
            "price": round(rep_price, 6),
            "price_range": price_range,
            "type": pool_type,
            "touches": touches,
            "tfs": list(tfs),
            "most_recent_ts": most_recent_ts,
            "age_hours": age_hours,
            "swept": swept,
            "distance_pct": round(distance_pct, 2),
            "strength_score": strength,
        })

    # Ranking: unswept first, then strength desc, then nearer (smaller
    # abs distance) to break ties.
    pools.sort(key=lambda p: (p["swept"], -p["strength_score"], abs(p["distance_pct"])))
    return pools
