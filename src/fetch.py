import asyncio
import httpx
from src.config import CONFIG
from src.types import OHLC, Timeframe

# How many of the most recent bars per TF contribute to the taker-delta
# roll-up. Picked so each TF covers a roughly comparable recency window
# (≈ last 24h on the short TFs, last N periods on the higher ones where
# there's no "24h equivalent").
TAKER_DELTA_LOOKBACK: dict[Timeframe, int] = {
    "1M": 3,
    "1w": 4,
    "1d": 7,
    "4h": 6,
    "1h": 24,
}

# data-api.binance.vision is Binance's public data mirror — same schema as
# api.binance.com/api/v3/klines but globally accessible (api.binance.com
# returns HTTP 451 from US-based cloud runtimes).
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = CONFIG.symbol

TF_LOOKBACK: dict[Timeframe, int] = {
    "1M": 36,
    "1w": 104,
    "1d": 200,
    "4h": 300,
    "1h": 500,
}

def parse_klines(raw: list[list]) -> list[OHLC]:
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

async def fetch_one(client: httpx.AsyncClient, tf: Timeframe) -> list[OHLC]:
    for attempt in range(3):
        try:
            r = await client.get(
                BINANCE_URL,
                params={"symbol": SYMBOL, "interval": tf, "limit": TF_LOOKBACK[tf]},
                timeout=10.0,
            )
            r.raise_for_status()
            return parse_klines(r.json())
        except (httpx.HTTPError, httpx.TimeoutException, ValueError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
    raise RuntimeError("unreachable")

async def fetch_all() -> dict[Timeframe, list[OHLC]]:
    async with httpx.AsyncClient() as client:
        tfs: list[Timeframe] = ["1M", "1w", "1d", "4h", "1h"]
        results = await asyncio.gather(*(fetch_one(client, tf) for tf in tfs))
        return dict(zip(tfs, results))


def taker_delta_per_tf(ohlc: dict[Timeframe, list[OHLC]]) -> dict[Timeframe, dict]:
    """Per-TF spot taker buy-vs-sell pressure. Uses Binance spot klines
    column `takerBuyBaseAssetVolume`; taker_sell = volume - taker_buy, so
    delta = 2*taker_buy - volume. `delta_pct` is signed: positive = taker
    buying dominant, negative = taker selling dominant. None when the fetched
    bars lack the field (legacy fixtures)."""
    out: dict[Timeframe, dict] = {}
    for tf, bars in ohlc.items():
        lookback = TAKER_DELTA_LOOKBACK.get(tf, 0)
        window = bars[-lookback:] if lookback else []
        if not window:
            continue
        if any(b.taker_buy_volume is None for b in window):
            continue
        total_vol = sum(b.volume for b in window)
        if total_vol <= 0:
            continue
        taker_buy = sum(b.taker_buy_volume or 0.0 for b in window)
        delta = 2 * taker_buy - total_vol
        out[tf] = {
            "delta_pct": round(delta / total_vol * 100, 2),
            "bars": len(window),
        }
    return out
