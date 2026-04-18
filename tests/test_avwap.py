import math

from src.avwap import compute_avwap, resolve_anchors, AnchoredVwap
from src.types import OHLC, SwingPair

def _b(ts, h, l, c, v, o=None):
    o = o if o is not None else c
    return OHLC(ts=ts, open=o, high=h, low=l, close=c, volume=v)

def test_avwap_single_bar_equals_typical_price():
    bars = [_b(0, 101, 99, 100, 10.0)]
    out = compute_avwap(bars, anchor_idx=0, anchor_type="AVWAP_SESSION", anchor_ts=0)
    # typical = (101+99+100)/3 = 100
    assert abs(out.vwap[-1] - 100.0) < 1e-9
    # Zero variance on single bar → bands == vwap
    assert abs(out.upper_1sd[-1] - 100.0) < 1e-9

def test_avwap_weighted_by_volume():
    # bar1 typ=100 vol=10, bar2 typ=110 vol=30 → VWAP = (100*10 + 110*30)/40 = 107.5
    bars = [_b(0, 101, 99, 100, 10.0), _b(1000, 111, 109, 110, 30.0)]
    out = compute_avwap(bars, anchor_idx=0, anchor_type="AVWAP_WEEK", anchor_ts=0)
    assert abs(out.vwap[-1] - 107.5) < 1e-6

def test_resolve_anchors_includes_session_week_month_and_swings():
    # 30 hourly bars, one swing pair with high at bar 10 and low at bar 20
    bars = [_b(i * 3_600_000, 101 + i*0.1, 99 + i*0.1, 100 + i*0.1, 1.0) for i in range(30)]
    pair = SwingPair(tf="1h", high_price=bars[10].high, high_ts=bars[10].ts,
                     low_price=bars[20].low, low_ts=bars[20].ts, direction="down")
    anchors = resolve_anchors(bars, [pair])
    types = {a[0] for a in anchors}
    assert "AVWAP_SESSION" in types
    assert "AVWAP_WEEK"    in types
    assert "AVWAP_MONTH"   in types
    assert "AVWAP_SWING_HH" in types
    assert "AVWAP_SWING_LL" in types

def test_avwap_pre_anchor_entries_are_nan():
    bars = [_b(i*1000, 101, 99, 100, 1.0) for i in range(5)]
    out = compute_avwap(bars, anchor_idx=2, anchor_type="AVWAP_SESSION", anchor_ts=2000)
    assert all(math.isnan(out.vwap[i]) for i in range(2))
    assert not math.isnan(out.vwap[2])
