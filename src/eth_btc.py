"""ETH/BTC relative-strength context.

Only populated when the active asset is ETH. Feeds the ETH analyst one
regime-setting clause for Sinteză: "ETH bounce +3.3% but ETH/BTC drifting
bearish at 0.0521 — BTC-dominance limits long conviction."

Keeps the payload lean: only the fields the analyst actually cites in a
one-clause opener. Historical OHLC is pulled via the same Binance data
mirror already used for BTCUSDT / ETHUSDT klines — no new outbound host.

Emits:
  - ratio            : current ETHBTC close
  - change_24h_pct   : signed %
  - trend            : 'bullish' | 'bearish' | 'range' (HTF structure on 1d)
  - nearest_fib      : {price, ratio, side: 'above'|'below'} | None
  - rsi_1d_14        : round(RSI, 1) for momentum confirmation
"""
from __future__ import annotations

import httpx

from src.types import OHLC
from src.swings import atr, detect_swings, detect_pivots
from src.fibs import compute_all
from src.market_structure import analyze_structure

ETH_BTC_SYMBOL = "ETHBTC"
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
DEFAULT_LIMIT = 200
TIMEOUT = 10.0


def _parse_klines(raw: list[list]) -> list[OHLC]:
    return [
        OHLC(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            taker_buy_volume=float(row[9]) if len(row) > 9 else None,
        )
        for row in raw
    ]


async def _fetch_klines(client: httpx.AsyncClient, interval: str, limit: int) -> list[OHLC]:
    for attempt in range(2):
        try:
            r = await client.get(
                BINANCE_URL,
                params={"symbol": ETH_BTC_SYMBOL, "interval": interval, "limit": limit},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            return _parse_klines(r.json())
        except (httpx.HTTPError, ValueError):
            if attempt == 1:
                return []
    return []


def _compute_rsi_1d(bars_1d: list[OHLC], length: int = 14) -> float | None:
    """Inline Wilder RSI on 1d closes — avoids pulling pandas for one number.
    Returns None when there aren't enough bars."""
    if len(bars_1d) < length + 2:
        return None
    closes = [b.close for b in bars_1d]
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    if not gains:
        return None
    # Wilder-smoothed EWM equivalent: avg_gain[i] = avg_gain[i-1]*(n-1)/n + gain[i]/n.
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for g, l in zip(gains[length:], losses[length:]):
        avg_gain = (avg_gain * (length - 1) + g) / length
        avg_loss = (avg_loss * (length - 1) + l) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _nearest_fib(current: float, fibs) -> dict | None:
    """Closest fib retracement/extension to `current`. Emits side so the
    agent knows whether it's an overhead ceiling or support below."""
    if not fibs:
        return None
    best = min(fibs, key=lambda f: abs(f.price - current))
    side = "above" if best.price > current else "below"
    return {
        "price":    round(best.price, 6),
        "ratio":    best.ratio,
        "tf":       best.tf,
        "kind":     best.kind,
        "side":     side,
        "distance_pct": round((best.price - current) / current * 100, 2),
    }


async def fetch() -> dict:
    """Run the ETH/BTC context build. Returns status='ok' on success,
    'unavailable' on any upstream failure, 'unsupported' when called for a
    non-ETH asset (no-op; caller should guard with CONFIG.asset == 'eth'
    but the belt-and-suspenders check keeps this module self-contained)."""
    async with httpx.AsyncClient() as client:
        bars_1d, bars_1w = await _fetch_klines(client, "1d", DEFAULT_LIMIT), await _fetch_klines(client, "1w", 100)

    if not bars_1d or len(bars_1d) < 20:
        return {"status": "unavailable", "reason": "insufficient 1d klines"}

    current = bars_1d[-1].close
    prev = bars_1d[-2].close
    change_24h_pct = (current - prev) / prev * 100 if prev else 0.0

    # HTF structure on 1d.
    highs, lows = detect_pivots(bars_1d, n=None)
    ms = analyze_structure(highs, lows, current_price=current)

    # Fibs from swings across 1d + 1w (max_pairs=3 each, same contract used
    # in the main pipeline). Keep the nearest as the cited level.
    swings_1d = detect_swings(bars_1d, tf="1d", max_pairs=3)
    swings_1w = detect_swings(bars_1w, tf="1w", max_pairs=3) if bars_1w else []
    fibs = compute_all(swings_1d + swings_1w)
    nearest = _nearest_fib(current, fibs)

    rsi = _compute_rsi_1d(bars_1d)

    return {
        "status":          "ok",
        "ratio":           round(current, 6),
        "change_24h_pct":  round(change_24h_pct, 2),
        "trend":           ms.bias,                # bullish | bearish | range
        "invalidation":    ms.invalidation_level,
        "nearest_fib":     nearest,
        "rsi_1d_14":       rsi,
    }
