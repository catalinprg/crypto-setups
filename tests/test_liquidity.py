"""Tests for the liquidity-pool proxy layer."""
from src.liquidity import compute_pools, _cluster_by_price, _is_swept
from src.types import OHLC, SwingPair


def _sp(tf, h, h_ts, l, l_ts, direction="down"):
    return SwingPair(
        tf=tf,
        high_price=h, high_ts=h_ts,
        low_price=l, low_ts=l_ts,
        direction=direction,
    )


def _bar(ts, high, low, close=None, open_=None, volume=0.0):
    c = close if close is not None else (high + low) / 2
    o = open_ if open_ is not None else c
    return OHLC(ts=ts, open=o, high=high, low=low, close=c, volume=volume)


def test_cluster_by_price_groups_within_radius():
    pivots = [
        (100.0, "1h", 1),
        (100.3, "1h", 2),   # within radius 0.5 → same cluster
        (101.0, "1d", 3),   # outside radius → new cluster
    ]
    clusters = _cluster_by_price(pivots, radius=0.5)
    assert len(clusters) == 2
    assert len(clusters[0]) == 2
    assert clusters[1][0][0] == 101.0


def test_cluster_width_cap_prevents_chaining():
    # Chain of pivots each within-radius of the next, but the whole chain
    # exceeds 2*radius → must split, not chain.
    pivots = [(100.0, "1h", 1), (100.4, "1h", 2), (100.8, "1h", 3), (101.2, "1h", 4)]
    clusters = _cluster_by_price(pivots, radius=0.5)
    # With max_width = 1.0, 100→100.8 hits the cap, must break there.
    assert len(clusters) >= 2


def test_compute_pools_produces_buy_side_above_and_sell_side_below():
    # Current price 100. One swing high at 105 (unswept), one swing low at 95.
    pairs = [_sp("1d", h=105, h_ts=1_000_000, l=95, l_ts=1_500_000)]
    ohlc = {
        "1d": [
            _bar(500_000, high=104, low=94),
            _bar(1_000_000, high=105, low=94),   # forming the high
            _bar(1_500_000, high=103, low=95),   # forming the low
            _bar(2_000_000, high=101, low=99),   # no sweep since
        ],
    }
    pools = compute_pools(
        swing_pairs=pairs, ohlc=ohlc,
        current_price=100.0, daily_atr=2.0,
        now_ms=2_000_000,
    )
    assert len(pools["buy_side"]) == 1
    assert pools["buy_side"][0]["type"] == "BSL"
    assert pools["buy_side"][0]["swept"] is False
    assert pools["buy_side"][0]["distance_pct"] > 0   # above current
    assert len(pools["sell_side"]) == 1
    assert pools["sell_side"][0]["type"] == "SSL"
    assert pools["sell_side"][0]["distance_pct"] < 0


def test_compute_pools_marks_swept_when_price_traded_through():
    # Swing high at 105 at ts=1000; later bar at ts=2000 has high=110 →
    # pool should be marked swept.
    pairs = [_sp("1h", h=105, h_ts=1000, l=95, l_ts=1200)]
    ohlc = {
        "1h": [
            _bar(500, high=104, low=94),
            _bar(1000, high=105, low=94),
            _bar(2000, high=110, low=95),   # sweeps the 105 high
        ],
    }
    pools = compute_pools(
        swing_pairs=pairs, ohlc=ohlc,
        # With price at 108 the BSL pool at 105 is BELOW price → filtered out
        # by the side filter (already taken). Test swept logic via _is_swept
        # directly instead.
        current_price=108.0, daily_atr=2.0,
        now_ms=2000,
    )
    # Pool below price is not a BSL target anymore.
    assert pools["buy_side"] == []

    # Direct swept check:
    assert _is_swept(105, 1000, "BSL", ohlc) is True
    # An unswept SSL level — price never traded below 90 after ts=1000.
    assert _is_swept(90, 1000, "SSL", ohlc) is False


def test_compute_pools_stacks_multi_tf_touches_into_one_cluster():
    # Three swing highs at ~the same price across different TFs → one
    # stacked pool with 3 touches, multi-TF tfs list, higher strength.
    pairs = [
        _sp("1w", h=200.0, h_ts=1000, l=180, l_ts=1100),
        _sp("1d", h=200.2, h_ts=2000, l=185, l_ts=2100),
        _sp("1h", h=199.9, h_ts=3000, l=190, l_ts=3100),
    ]
    ohlc = {"1d": [_bar(500, 199, 179), _bar(3500, 198, 181)]}
    pools = compute_pools(
        swing_pairs=pairs, ohlc=ohlc,
        current_price=195.0, daily_atr=4.0,   # radius = 1.0
        now_ms=3500,
    )
    buy = pools["buy_side"]
    assert len(buy) == 1
    pool = buy[0]
    assert pool["touches"] == 3
    assert set(pool["tfs"]) == {"1w", "1d", "1h"}
    # TF_WEIGHTS for crypto: 1w=4, 1d=3, 1h=1 → sum=8 × 3 touches = 24
    assert pool["strength_score"] == 24


def test_compute_pools_filters_far_distance():
    # Swing high 50% above current price → dropped.
    pairs = [_sp("1d", h=150, h_ts=1000, l=95, l_ts=1100)]
    ohlc = {"1d": [_bar(500, 100, 90)]}
    pools = compute_pools(
        swing_pairs=pairs, ohlc=ohlc,
        current_price=100.0, daily_atr=2.0,
        now_ms=2000,
    )
    assert pools["buy_side"] == []
    # The 95 low is 5% below — kept.
    assert len(pools["sell_side"]) == 1


def test_compute_pools_ranks_unswept_before_swept_then_by_strength():
    # Unswept 1h pool + swept 1w pool on the buy side — unswept must come
    # first despite lower strength score.
    pairs = [
        _sp("1w", h=110, h_ts=1000, l=90, l_ts=1100),   # will be swept
        _sp("1h", h=108, h_ts=2000, l=95, l_ts=2100),   # unswept
    ]
    ohlc = {
        "1h": [
            _bar(500, 100, 90),
            _bar(1000, 110, 95),
            _bar(1500, 112, 95),   # sweeps the 110
            _bar(2000, 108, 95),
            _bar(2500, 107, 100),  # does NOT sweep the 108
        ],
    }
    pools = compute_pools(
        swing_pairs=pairs, ohlc=ohlc,
        current_price=105.0, daily_atr=3.0,
        now_ms=2500,
    )
    buy = pools["buy_side"]
    assert len(buy) == 2
    assert buy[0]["swept"] is False   # unswept first
    assert buy[1]["swept"] is True


def test_compute_pools_handles_empty_inputs():
    assert compute_pools([], {}, 100.0, 1.0) == {"buy_side": [], "sell_side": []}
    assert compute_pools([_sp("1h", 100, 1, 90, 2)], {}, 0, 1.0) == {"buy_side": [], "sell_side": []}
