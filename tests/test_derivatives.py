import json
from pathlib import Path
import pytest
from src.derivatives import (
    aggregate_open_interest, aggregate_liquidations,
    detect_clusters, build_derivatives_payload, enrich_clusters_with_price,
)
from src.types import OHLC

FIX = Path(__file__).parent / "fixtures" / "coinalyze"

def _load(name):
    return json.loads((FIX / f"{name}.json").read_text())

def test_aggregate_open_interest_returns_none_change_when_no_shared_venues():
    # Current has A+6; history has only 3 → no overlap. Change pct must be
    # None (not 0.0) so the agent prompt degrades truthfully.
    raw_current = [
        {"symbol": "BTCUSDT_PERP.A", "value": 7_000_000_000.0},
        {"symbol": "BTCUSDT.6",      "value": 4_000_000_000.0},
    ]
    raw_history = [
        {"symbol": "BTCUSDT_PERP.3", "history": [
            {"t": 1000, "c": 1_000_000_000},
            {"t": 1001, "c": 1_100_000_000},
        ]},
    ]
    result = aggregate_open_interest(raw_current, raw_history, lookback_buckets=1)
    assert result["total_usd"] == 11_000_000_000.0
    assert result["change_24h_pct"] is None


def test_aggregate_open_interest_sums_across_venues():
    raw_current = [
        {"symbol": "BTCUSDT_PERP.A", "value": 7_000_000_000.0, "update": 1},
        {"symbol": "BTCUSDT.6",      "value": 4_000_000_000.0, "update": 1},
        {"symbol": "BTCUSDT_PERP.3", "value": 2_000_000_000.0, "update": 1},
    ]
    raw_history = [
        {"symbol": "BTCUSDT_PERP.A", "history": [
            {"t": 1000, "o": 6_500_000_000, "h": 0, "l": 0, "c": 6_800_000_000},
            {"t": 1001, "o": 6_800_000_000, "h": 0, "l": 0, "c": 7_000_000_000},
        ]},
        {"symbol": "BTCUSDT.6", "history": [
            {"t": 1000, "o": 3_800_000_000, "h": 0, "l": 0, "c": 3_900_000_000},
            {"t": 1001, "o": 3_900_000_000, "h": 0, "l": 0, "c": 4_000_000_000},
        ]},
        # OKX history missing — must not crash
    ]
    result = aggregate_open_interest(raw_current, raw_history, lookback_buckets=1)
    assert result["total_usd"] == 13_000_000_000.0
    # 24h-ago total across available venues: 6.8B + 3.9B = 10.7B
    # current-matching total (same venues only): 7B + 4B = 11B
    # pct change: (11B - 10.7B) / 10.7B * 100 ≈ 2.80%
    assert round(result["change_24h_pct"], 2) == 2.80
    assert set(result["venues_used"]) == {"A", "6"}


def test_aggregate_liquidations_24h_totals():
    raw_liq = [
        {"symbol": "BTCUSDT_PERP.A", "history": [
            {"t": t, "l": 10_000_000.0, "s": 2_000_000.0}
            for t in range(1000, 1018)  # 18 buckets
        ]},
    ]
    result = aggregate_liquidations(raw_liq, num_buckets=6)
    # Last 6 buckets: 6*10M long, 6*2M short
    assert result["long_usd"] == 60_000_000.0
    assert result["short_usd"] == 12_000_000.0
    assert result["dominant_side"] == "long"


def test_aggregate_liquidations_72h_totals():
    raw_liq = [
        {"symbol": "BTCUSDT_PERP.A", "history": [
            {"t": t, "l": 10_000_000.0, "s": 2_000_000.0}
            for t in range(1000, 1018)  # 18 buckets = full 72h window
        ]},
    ]
    result = aggregate_liquidations(raw_liq, num_buckets=18)
    assert result["long_usd"] == 180_000_000.0
    assert result["short_usd"] == 36_000_000.0
    assert result["dominant_side"] == "long"

def test_detect_clusters_flags_outlier_buckets():
    # 17 buckets with ~1M total liq, 1 bucket with 100M total
    history = [{"t": 1100 + i, "l": 500_000, "s": 500_000} for i in range(17)]
    history.insert(10, {"t": 1010, "l": 50_000_000, "s": 50_000_000})
    raw = [{"symbol": "BTCUSDT_PERP.A", "history": history}]
    clusters = detect_clusters(raw, stddev_threshold=2.0)
    assert len(clusters) >= 1
    assert clusters[0]["total_usd"] == 100_000_000
    assert clusters[0]["t"] == 1010


def test_mad_clusters_survive_second_outlier():
    # The core regression MAD fixes: two near-equal megabars would inflate
    # the old mean+stddev threshold enough to swallow a third real cluster.
    # With MAD the two megabars don't move the median — all three get
    # flagged.
    history = [{"t": 1100 + i, "l": 500_000, "s": 500_000} for i in range(15)]
    history.append({"t": 2001, "l": 100_000_000, "s": 0})
    history.append({"t": 2002, "l": 100_000_000, "s": 0})
    history.append({"t": 2003, "l":  10_000_000, "s": 0})
    raw = [{"symbol": "BTCUSDT_PERP.A", "history": history}]
    clusters = detect_clusters(raw, stddev_threshold=2.0)
    flagged_ts = {c["t"] for c in clusters}
    assert 2001 in flagged_ts
    assert 2002 in flagged_ts
    assert 2003 in flagged_ts

def test_build_derivatives_payload_happy_path_from_fixtures():
    payload = build_derivatives_payload(
        open_interest_raw=_load("open_interest"),
        open_interest_history_raw=_load("open_interest_history"),
        liquidations_raw=_load("liquidation_history"),
        funding={"rate_8h_pct": 0.002, "annualized_pct": 2.19},
    )
    assert payload["status"] == "ok"
    assert payload["open_interest_usd"] > 1_000_000_000
    assert payload["funding_rate_annualized_pct"] is not None
    assert "long_usd" in payload["liquidations_24h"]
    assert isinstance(payload["liquidation_clusters_72h"], list)
    assert "long_usd" in payload["liquidations_72h"]
    # 72h total must be >= 24h total across same fixture
    assert payload["liquidations_72h"]["long_usd"] >= payload["liquidations_24h"]["long_usd"]

def test_build_derivatives_payload_handles_empty_inputs():
    payload = build_derivatives_payload(
        open_interest_raw=[],
        open_interest_history_raw=[],
        liquidations_raw=[],
        funding=None,
    )
    assert payload["status"] == "unavailable"


def test_build_derivatives_payload_degrades_when_oi_is_missing():
    # Simulates Coinalyze 503 on /open-interest while liquidations + funding
    # still came back. Must not discard the surviving sections.
    payload = build_derivatives_payload(
        open_interest_raw=[],
        open_interest_history_raw=[],
        liquidations_raw=_load("liquidation_history"),
        funding={"rate_8h_pct": 0.01, "annualized_pct": 10.95},
    )
    assert payload["status"] == "ok"
    assert payload["partial"] is True
    assert "oi" in payload["missing_sections"]
    assert payload["open_interest_usd"] is None
    assert payload["open_interest_change_24h_pct"] is None
    assert payload["funding_rate_annualized_pct"] == 10.95
    assert payload["liquidations_24h"] is not None
    assert "long_usd" in payload["liquidations_24h"]


def test_build_derivatives_payload_computes_basis():
    payload = build_derivatives_payload(
        open_interest_raw=[],
        open_interest_history_raw=[],
        liquidations_raw=[],
        funding=None,
        spot_mid=70000.0,
        perp_mark=70140.0,
    )
    assert payload["status"] == "ok"
    assert payload["spot_mid"] == 70000.0
    assert payload["perp_mark"] == 70140.0
    # (70140 - 70000) / 70000 * 100 ≈ 0.2
    assert payload["basis_vs_spot_pct"] == 0.2
    assert payload["basis_vs_spot_abs_usd"] == 140.0


def test_build_derivatives_payload_exposes_funding_by_venue_and_divergence():
    payload = build_derivatives_payload(
        open_interest_raw=[],
        open_interest_history_raw=[],
        liquidations_raw=[],
        funding={"rate_8h_pct": 0.01, "annualized_pct": 10.95},
        funding_hyperliquid={"rate_8h_pct": 0.03, "annualized_pct": 32.85},
    )
    assert payload["funding_by_venue"]["bybit"]["rate_8h_pct"] == 0.01
    assert payload["funding_by_venue"]["hyperliquid"]["rate_8h_pct"] == 0.03
    assert payload["funding_divergence_8h_pct"] == 0.02


def test_build_derivatives_payload_divergence_null_when_hl_missing():
    payload = build_derivatives_payload(
        open_interest_raw=[],
        open_interest_history_raw=[],
        liquidations_raw=[],
        funding={"rate_8h_pct": 0.01, "annualized_pct": 10.95},
        funding_hyperliquid=None,
    )
    assert payload["funding_divergence_8h_pct"] is None


def _bar(ts_ms, high, low, close):
    return OHLC(ts=ts_ms, open=close, high=high, low=low, close=close, volume=0)


def test_enrich_clusters_attaches_price_from_matching_4h_bar():
    # 4h bar at t=1000s, spans [1000s, 1000s + 4h) in ms terms
    bars = [
        _bar(1000 * 1000, high=70000, low=69000, close=69500),
        _bar((1000 + 4 * 3600) * 1000, high=71000, low=70500, close=70800),
    ]
    clusters = [
        {"t": 1000, "total_usd": 50_000_000, "dominant_side": "long"},
        {"t": 1000 + 4 * 3600 + 100, "total_usd": 30_000_000, "dominant_side": "short"},
    ]
    enriched = enrich_clusters_with_price(clusters, bars)
    assert enriched[0]["price_high"] == 70000
    assert enriched[0]["price_low"] == 69000
    assert enriched[0]["price_close"] == 69500
    assert enriched[1]["price_high"] == 71000
    assert enriched[1]["price_low"] == 70500


def test_enrich_clusters_leaves_unmatched_as_none():
    bars = [_bar(1000 * 1000, high=70000, low=69000, close=69500)]
    # Cluster timestamp far outside the bar window
    clusters = [{"t": 99999999, "total_usd": 10_000_000, "dominant_side": "long"}]
    enriched = enrich_clusters_with_price(clusters, bars)
    assert enriched[0]["price_high"] is None
    assert enriched[0]["price_low"] is None
    assert enriched[0]["price_close"] is None
    # Original fields preserved
    assert enriched[0]["total_usd"] == 10_000_000
