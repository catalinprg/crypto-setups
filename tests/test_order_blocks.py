from src.order_blocks import detect_order_blocks
from src.types import OHLC


def _b(ts, o, h, l, c, v=1.0):
    return OHLC(ts=ts, open=o, high=h, low=l, close=c, volume=v)


# ---------------------------------------------------------------------------
# Required tests (spec §6.1)
# ---------------------------------------------------------------------------

def test_bullish_ob_identified_at_last_down_candle_before_displacement():
    # Bars 0-2: ranging; bar 3 is a down candle; bar 4 is strong up candle
    # with range > 1.5×ATR breaking above prior swing high.
    atr = 1.0
    bars = [
        _b(0, 100, 101, 99, 100.5),
        _b(1, 100.5, 102, 100, 101.5),   # swing high at 102
        _b(2, 101.5, 101.8, 101, 101.2),
        _b(3, 101.2, 101.5, 100.5, 100.7),  # down candle (OB candidate)
        _b(4, 100.7, 104.5, 100.7, 104.3),  # range 3.8 > 1.5×ATR, breaks 102
    ]
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    bulls = [o for o in obs if o.type == "OB_BULL"]
    assert len(bulls) >= 1
    assert bulls[0].formation_ts == bars[3].ts  # the down candle BEFORE displacement


def test_no_ob_when_displacement_below_threshold():
    atr = 2.0
    bars = [
        _b(0, 100, 101, 99, 100),
        _b(1, 100, 102, 99, 101),
        _b(2, 101, 101, 100, 100.5),   # small down candle
        _b(3, 100.5, 102, 100.3, 101.8),   # up candle range 1.7 < 1.5×ATR=3.0
    ]
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    assert obs == []


# ---------------------------------------------------------------------------
# Extra tests (code review patterns)
# ---------------------------------------------------------------------------

def test_bearish_ob_identified_at_last_up_candle_before_down_displacement():
    # Mirror of bullish: bar 3 is up candle, bar 4 is strong down that breaks prior low.
    atr = 1.0
    bars = [
        _b(0, 102, 103, 101, 101.5),
        _b(1, 101.5, 102, 100, 100.5),   # swing low at 100
        _b(2, 100.5, 101, 100.2, 100.8),
        _b(3, 100.8, 101.5, 100.5, 101.3),  # up candle (OB candidate)
        _b(4, 101.3, 101.3, 97.5, 97.7),    # range 3.8 > 1.5×ATR, breaks 100
    ]
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    bears = [o for o in obs if o.type == "OB_BEAR"]
    assert len(bears) >= 1
    assert bears[0].formation_ts == bars[3].ts  # the up candle BEFORE displacement


def test_ob_marked_mitigated_when_price_trades_into_ob_range():
    # OB at bar 3 (low=100.5, high=101.5).  Bar 5 dips back into that range.
    atr = 1.0
    bars = [
        _b(0, 100, 101, 99, 100.5),
        _b(1, 100.5, 102, 100, 101.5),   # swing high at 102
        _b(2, 101.5, 101.8, 101, 101.2),
        _b(3, 101.2, 101.5, 100.5, 100.7),  # down candle → OB lo=100.5, hi=101.5
        _b(4, 100.7, 104.5, 100.7, 104.3),  # displacement
        _b(5, 104.3, 104.5, 100.8, 101.0),  # trades back into OB [100.5, 101.5]
    ]
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    bulls = [o for o in obs if o.type == "OB_BULL"]
    assert len(bulls) >= 1
    assert bulls[0].mitigated is True


def test_ob_marked_stale_after_many_unmitigated_bars():
    # Same bullish setup, then 150 bars far above the OB without touching it.
    atr = 1.0
    bars = [
        _b(0, 100, 101, 99, 100.5),
        _b(1, 100.5, 102, 100, 101.5),
        _b(2, 101.5, 101.8, 101, 101.2),
        _b(3, 101.2, 101.5, 100.5, 100.7),
        _b(4, 100.7, 104.5, 100.7, 104.3),  # displacement
    ]
    for i in range(5, 160):
        bars.append(_b(i, 110, 112, 109, 111))  # far above OB, no mitigation
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    bulls = [o for o in obs if o.type == "OB_BULL"]
    assert len(bulls) >= 1
    ob = bulls[0]
    assert ob.stale is True
    assert ob.mitigated is False


def test_no_ob_when_displacement_does_not_break_prior_swing_high():
    # Strong displacement (range > 1.5×ATR) but close stays below the prior high.
    atr = 1.0
    bars = [
        _b(0, 100, 105, 99, 104),          # swing high at 105
        _b(1, 104, 104.5, 103, 103.5),
        _b(2, 103.5, 104, 103, 103.2),     # down candle
        _b(3, 103.2, 104.8, 103.0, 104.7), # range 1.8 > 1.5×ATR, but closes at 104.7 < 105
    ]
    obs = detect_order_blocks(bars, tf="1h", atr_14=atr, stale_after=100)
    bulls = [o for o in obs if o.type == "OB_BULL"]
    assert bulls == []
