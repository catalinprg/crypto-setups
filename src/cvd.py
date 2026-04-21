"""Cumulative Volume Delta + price divergence detection.

Taker delta on each bar = 2 × taker_buy_volume − total_volume. Positive =
aggressive buyers dominated; negative = aggressive sellers. Summing deltas
over a rolling window gives CVD, a momentum-of-aggression proxy.

Divergence signal:
  - Bullish: price made a lower low vs the prior swing, but CVD made a higher
    low. Sellers are losing aggression — reversal tilt.
  - Bearish: price made a higher high vs the prior swing, but CVD made a
    lower high. Buyers are losing aggression — reversal tilt.

For this pipeline we keep it deliberately simple: 24h rolling CVD on 1h bars,
compare to the 24h rolling price trend, classify as bullish-divergence,
bearish-divergence, aligned-bullish, aligned-bearish, or flat.
"""
from __future__ import annotations

from src.types import OHLC


ROLLING_HOURS = 24


def _bar_delta(b: OHLC) -> float | None:
    if b.taker_buy_volume is None or b.volume <= 0:
        return None
    return 2.0 * b.taker_buy_volume - b.volume


def compute_cvd_series(h1_bars: list[OHLC], window_hours: int = ROLLING_HOURS) -> list[float]:
    """Return a rolling CVD series aligned with the last `window_hours` bars.
    cvd[i] = sum of bar_deltas from h1_bars[-window_hours+i-1] to h1_bars[-window_hours+i].
    Simple prefix-sum; missing taker_buy_volume treated as 0 delta."""
    if not h1_bars:
        return []
    window = h1_bars[-window_hours:] if len(h1_bars) >= window_hours else h1_bars
    series: list[float] = []
    running = 0.0
    for b in window:
        d = _bar_delta(b) or 0.0
        running += d
        series.append(running)
    return series


def _split_midpoint(values: list[float]) -> tuple[float, float]:
    """Return (first-half extremum, second-half extremum) used for trend comparison.
    First-half = min for price-low comparison, second-half = later extreme, etc.
    Caller decides which extremum it wants."""
    mid = len(values) // 2
    return values[:mid], values[mid:]


def compute_cvd_snapshot(h1_bars: list[OHLC], window_hours: int = ROLLING_HOURS) -> dict:
    """Produce the analyst-facing CVD snapshot.

    Output:
      {
        "window_hours":       24,
        "bars_used":          24,
        "cvd_end":            float,    # final CVD value (base-asset units)
        "cvd_delta_window":   float,    # cvd_end − cvd[0] (change over window)
        "trend":              "bullish" | "bearish" | "flat",
        "divergence":         "bullish" | "bearish" | null,
        "notes":              [str, ...]  # terse debug / context
      }
    Returns a status=unavailable shape when bars lack taker data.
    """
    if not h1_bars:
        return {"status": "unavailable", "reason": "no_bars"}
    window = h1_bars[-window_hours:] if len(h1_bars) >= window_hours else h1_bars
    if len(window) < 6:
        return {"status": "unavailable", "reason": "insufficient_bars"}

    bar_deltas = [_bar_delta(b) for b in window]
    missing = sum(1 for d in bar_deltas if d is None)
    if missing > len(bar_deltas) // 2:
        return {"status": "unavailable", "reason": "taker_data_missing"}

    cvd_series = compute_cvd_series(window, window_hours=len(window))
    cvd_end = cvd_series[-1]
    cvd_start = cvd_series[0]
    cvd_delta = cvd_end - cvd_start

    # Trend classification: compare first-half mean vs second-half mean of CVD.
    first, second = _split_midpoint(cvd_series)
    if first and second:
        mean1 = sum(first) / len(first)
        mean2 = sum(second) / len(second)
        spread = mean2 - mean1
    else:
        spread = 0.0

    # Scale trend threshold to the magnitude of the series so low-volume assets
    # are not flagged as flat prematurely. Use max absolute cvd as scale.
    scale = max(abs(v) for v in cvd_series) if cvd_series else 1.0
    thr = max(1.0, 0.05 * scale)
    if spread > thr:
        cvd_trend = "bullish"
    elif spread < -thr:
        cvd_trend = "bearish"
    else:
        cvd_trend = "flat"

    # Divergence: compare price high/low in first vs second half of window vs
    # CVD high/low in the same halves.
    prices_first = [b.high for b in window[:len(first)]]
    prices_second = [b.high for b in window[len(first):]]
    lows_first = [b.low for b in window[:len(first)]]
    lows_second = [b.low for b in window[len(first):]]

    divergence: str | None = None
    notes: list[str] = []
    if prices_first and prices_second and first and second:
        price_hh = max(prices_second) > max(prices_first)
        price_ll = min(lows_second) < min(lows_first)
        cvd_hh = max(second) > max(first)
        cvd_ll = min(second) < min(first)

        if price_hh and not cvd_hh:
            divergence = "bearish"
            notes.append("price HH, cvd LH")
        elif price_ll and not cvd_ll:
            divergence = "bullish"
            notes.append("price LL, cvd HL")

    return {
        "status":              "ok",
        "window_hours":        len(window),
        "bars_used":           len(window),
        "cvd_end":             round(cvd_end, 2),
        "cvd_delta_window":    round(cvd_delta, 2),
        "trend":               cvd_trend,
        "divergence":          divergence,
        "notes":               notes,
    }
