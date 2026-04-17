import pytest
from src.fetch import parse_klines, TF_LOOKBACK, taker_delta_per_tf
from src.types import OHLC

def test_parse_klines_maps_raw_binance_response():
    # Binance klines row: [open_time, open, high, low, close, volume,
    # close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
    raw = [
        [1700000000000, "50000.00", "51000.00", "49500.00", "50500.00",
         "1000.5", 1700086399999, "x", 1, "600.25", "x", "x"],
    ]
    result = parse_klines(raw)
    assert len(result) == 1
    candle = result[0]
    assert candle.ts == 1700000000000
    assert candle.open == 50000.0
    assert candle.high == 51000.0
    assert candle.low == 49500.0
    assert candle.close == 50500.0
    assert candle.volume == 1000.5
    assert candle.taker_buy_volume == 600.25


def test_parse_klines_rows_shorter_than_10_cols_yield_none_taker():
    # Defensive: if the upstream shape ever shrinks we must not crash.
    raw = [[1700000000000, "1", "1", "1", "1", "1"]]
    result = parse_klines(raw)
    assert result[0].taker_buy_volume is None

def test_tf_lookback_has_all_five_timeframes():
    assert set(TF_LOOKBACK.keys()) == {"1M", "1w", "1d", "4h", "1h"}
    for v in TF_LOOKBACK.values():
        assert 1 <= v <= 1000


def _b(vol, taker_buy):
    return OHLC(ts=0, open=0, high=0, low=0, close=0, volume=vol, taker_buy_volume=taker_buy)


def test_taker_delta_per_tf_buy_dominant():
    # 10 bars of volume=100, taker_buy=70 → taker_sell=30, delta=40 per bar.
    # delta_pct = 40/100 = 40%.
    bars_1h = [_b(100, 70) for _ in range(24)]
    out = taker_delta_per_tf({"1h": bars_1h})
    assert out["1h"]["delta_pct"] == 40.0
    assert out["1h"]["bars"] == 24


def test_taker_delta_per_tf_skips_when_taker_volume_missing():
    bars = [OHLC(ts=0, open=0, high=0, low=0, close=0, volume=1.0) for _ in range(24)]
    assert taker_delta_per_tf({"1h": bars}) == {}


def test_taker_delta_per_tf_skips_zero_volume():
    bars = [_b(0, 0) for _ in range(24)]
    assert taker_delta_per_tf({"1h": bars}) == {}
