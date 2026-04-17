"""Derivatives + positioning enrichment.

Env var (required for `fetch_all`, not for pure-function aggregators):
  COINALYZE_API_KEY

Fetches in parallel:
  - Coinalyze OI + liquidation-history across 3 USDT-M perps (Binance, Bybit, OKX).
  - Bybit public ticker — funding rate + mark price for the active asset.
  - Binance spot book-ticker — mid price for basis computation.
  - Hyperliquid metaAndAssetCtxs — hourly funding for cross-venue divergence.

Aggregates client-side and returns a single derivatives dict for the pipeline
payload. Gracefully degrades on failure or partial venue coverage.
"""
import asyncio
import os
import statistics
import time
from typing import Any

import httpx

from src.config import CONFIG

COINALYZE_BASE = "https://api.coinalyze.net/v1"
# Bybit's public tickers endpoint is globally accessible and returns the
# current funding rate for USDT-M perpetuals as a fraction per 8h. We prefer
# Bybit over Binance fapi because fapi.binance.com returns 451 from US-based
# cloud runtimes. Funding on Bybit vs Binance typically diverges by <2 bps
# in normal conditions — acceptable for positioning context.
BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"
# Binance spot mirror (same allowlist as klines fetch). Spot mid vs Bybit
# perp mark gives us basis — a strong premium / discount signal.
BINANCE_SPOT_TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
# Hyperliquid public info endpoint. metaAndAssetCtxs returns current
# hourly-funding + markPx for every asset in a single call.
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
SYMBOLS = list(CONFIG.coinalyze_symbols)
LIQUIDATION_WINDOW_HOURS = 72
LIQUIDATION_INTERVAL = "4hour"
BUCKETS_PER_24H = 6
CLUSTER_STDDEV_THRESHOLD = 2.0
TIMEOUT = 10.0


def _exchange_code(symbol: str) -> str:
    """BTCUSDT_PERP.A -> A;  BTCUSDT.6 -> 6;  BTCUSDT_PERP.3 -> 3"""
    return symbol.rsplit(".", 1)[-1]


def aggregate_open_interest(
    current_raw: list[dict], history_raw: list[dict], lookback_buckets: int = BUCKETS_PER_24H
) -> dict:
    """Sum current OI across venues. Compute 24h change using only venues
    with both current and historical data. `change_24h_pct` is None when
    the change cannot be computed (no shared venues, or the historical
    total is zero) — the agent prompt treats null as "unavailable" rather
    than "flat"."""
    current_by_venue = {
        _exchange_code(r["symbol"]): (r.get("value") or 0.0)
        for r in current_raw
    }
    total_usd = sum(current_by_venue.values())

    history_by_venue = {}
    for r in history_raw:
        ex = _exchange_code(r["symbol"])
        hist = r.get("history") or []
        if len(hist) > lookback_buckets:
            c = hist[-(lookback_buckets + 1)].get("c")
            if c is not None:
                history_by_venue[ex] = c

    shared = sorted(set(current_by_venue) & set(history_by_venue))
    if shared:
        now_sum = sum(current_by_venue[v] for v in shared)
        then_sum = sum(history_by_venue[v] for v in shared)
        change_pct: float | None = (now_sum - then_sum) / then_sum * 100 if then_sum else None
    else:
        change_pct = None

    return {
        "total_usd": total_usd,
        "change_24h_pct": change_pct,
        "venues_used": shared if shared else sorted(current_by_venue.keys()),
    }



def aggregate_liquidations(liquidations_raw: list[dict], num_buckets: int = BUCKETS_PER_24H) -> dict:
    """Sum long- and short-liquidation USD across venues over the last
    `num_buckets` buckets. Callers pass `BUCKETS_PER_24H` for a 24h sum
    and `BUCKETS_PER_24H * 3` for the full 72h window."""
    long_total = 0.0
    short_total = 0.0
    for r in liquidations_raw:
        hist = r.get("history") or []
        for bucket in hist[-num_buckets:]:
            long_total += bucket.get("l", 0.0) or 0.0
            short_total += bucket.get("s", 0.0) or 0.0
    if long_total == short_total == 0:
        side = "neutral"
    elif long_total > short_total:
        side = "long"
    else:
        side = "short"
    return {"long_usd": long_total, "short_usd": short_total, "dominant_side": side}


# MAD scales to stddev-equivalent under normal via *1.4826; the public
# threshold stays in "sigma equivalents" so existing callers / tests keep
# their intuition.
_MAD_TO_SIGMA = 1.4826


def detect_clusters(
    liquidations_raw: list[dict], stddev_threshold: float = CLUSTER_STDDEV_THRESHOLD
) -> list[dict]:
    """Flag buckets where total (long+short) liquidation USD, summed across
    venues for that bucket, exceeds `median + k * MAD * 1.4826` of the
    full 72h window (k = `stddev_threshold`, scaled to sigma-equivalents).

    Switched from mean+stddev because a single mega-liquidation bar inflates
    the stddev enough to swallow every other real cluster. MAD is robust to
    one-off outliers. Falls back to "strictly above median" when MAD is 0
    (happens when most buckets carry identical totals)."""
    by_ts: dict[int, dict[str, float]] = {}
    for r in liquidations_raw:
        for bucket in r.get("history") or []:
            t = bucket["t"]
            by_ts.setdefault(t, {"l": 0.0, "s": 0.0})
            by_ts[t]["l"] += bucket.get("l", 0.0) or 0.0
            by_ts[t]["s"] += bucket.get("s", 0.0) or 0.0
    totals = {t: v["l"] + v["s"] for t, v in by_ts.items()}
    if len(totals) < 3:
        return []
    values = list(totals.values())
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad > 0:
        threshold = med + stddev_threshold * _MAD_TO_SIGMA * mad
    else:
        threshold = med
    clusters = []
    for t in sorted(by_ts.keys()):
        total = totals[t]
        if total > threshold:
            sums = by_ts[t]
            side = "long" if sums["l"] > sums["s"] else ("short" if sums["s"] > sums["l"] else "neutral")
            clusters.append({"t": t, "total_usd": total, "dominant_side": side})
    return clusters


def enrich_clusters_with_price(
    clusters: list[dict], bars_4h: list
) -> list[dict]:
    """Attach `price_high`, `price_low`, `price_close` to each cluster by
    matching its timestamp (Unix seconds) against the 4h OHLC bar whose
    open-time window contains it. Bars are OHLC objects (ts in ms).

    If no matching bar is found (cluster older than the fetched window),
    the price fields are set to None.
    """
    INTERVAL_MS = 4 * 3600 * 1000
    enriched = []
    for c in clusters:
        t_ms = int(c["t"]) * 1000
        match = None
        for b in bars_4h:
            if b.ts <= t_ms < b.ts + INTERVAL_MS:
                match = b
                break
        if match is None:
            enriched.append({**c, "price_high": None, "price_low": None, "price_close": None})
        else:
            enriched.append({
                **c,
                "price_high": match.high,
                "price_low": match.low,
                "price_close": match.close,
            })
    return enriched


def _empty_funding() -> dict:
    return {"rate_8h_pct": None, "annualized_pct": None}


def _compute_basis(spot_mid: float | None, perp_mark: float | None) -> dict:
    """Perp mark vs spot mid. `pct` is signed: positive = perp premium over
    spot, negative = perp discount. None when either input is missing."""
    if spot_mid is None or perp_mark is None or spot_mid <= 0:
        return {"pct": None, "abs_usd": None}
    abs_usd = perp_mark - spot_mid
    return {
        "pct": round(abs_usd / spot_mid * 100, 4),
        "abs_usd": round(abs_usd, 2),
    }


def build_derivatives_payload(
    *,
    open_interest_raw: list[dict],
    open_interest_history_raw: list[dict],
    liquidations_raw: list[dict],
    funding: dict | None = None,
    funding_hyperliquid: dict | None = None,
    spot_mid: float | None = None,
    perp_mark: float | None = None,
) -> dict:
    """Assemble the derivatives payload. Degrades per-section on partial
    upstream failure: fields from a missing source are set to None instead
    of faking a zero. status=unavailable only when every source is empty."""
    funding = funding or _empty_funding()
    funding_hyperliquid = funding_hyperliquid or _empty_funding()
    has_oi = bool(open_interest_raw)
    has_liq = bool(liquidations_raw)
    has_funding = funding.get("annualized_pct") is not None
    has_basis = spot_mid is not None and perp_mark is not None

    if not (has_oi or has_liq or has_funding or has_basis):
        return {"status": "unavailable", "error": "no data"}

    if has_oi:
        oi = aggregate_open_interest(open_interest_raw, open_interest_history_raw)
        oi_total = oi["total_usd"]
        oi_change = oi["change_24h_pct"]
        oi_venues = oi["venues_used"]
    else:
        oi_total = None
        oi_change = None
        oi_venues = []

    if has_liq:
        liq_24h = aggregate_liquidations(liquidations_raw, num_buckets=BUCKETS_PER_24H)
        liq_72h = aggregate_liquidations(liquidations_raw, num_buckets=BUCKETS_PER_24H * 3)
        clusters = detect_clusters(liquidations_raw)
    else:
        liq_24h = None
        liq_72h = None
        clusters = []

    missing = [
        name for name, present in (
            ("oi", has_oi),
            ("liq", has_liq),
            ("funding", has_funding),
            ("basis", has_basis),
        )
        if not present
    ]

    basis = _compute_basis(spot_mid, perp_mark)

    # Cross-venue funding divergence — abs delta in 8h terms between Bybit
    # and Hyperliquid. Non-null only when both sides reported. Used by the
    # agent to flag single-venue funding anomalies.
    hl_8h = funding_hyperliquid.get("rate_8h_pct")
    bybit_8h = funding.get("rate_8h_pct")
    if hl_8h is not None and bybit_8h is not None:
        funding_divergence_8h_pct = round(abs(bybit_8h - hl_8h), 4)
    else:
        funding_divergence_8h_pct = None

    return {
        "status": "ok",
        "partial": bool(missing),
        "missing_sections": missing,
        "open_interest_usd": oi_total,
        "open_interest_change_24h_pct": oi_change,
        "funding_rate_8h_pct": funding["rate_8h_pct"],
        "funding_rate_annualized_pct": funding["annualized_pct"],
        "funding_by_venue": {
            "bybit": funding,
            "hyperliquid": funding_hyperliquid,
        },
        "funding_divergence_8h_pct": funding_divergence_8h_pct,
        "spot_mid": spot_mid,
        "perp_mark": perp_mark,
        "basis_vs_spot_pct": basis["pct"],
        "basis_vs_spot_abs_usd": basis["abs_usd"],
        "liquidations_24h": liq_24h,
        "liquidations_72h": liq_72h,
        "liquidation_clusters_72h": clusters,
        "venues_used": oi_venues,
    }


async def fetch_bybit_ticker(client: httpx.AsyncClient) -> dict:
    """Fetch Bybit ticker for the active asset perp: funding rate + mark
    price in one call. Returns {rate_8h_pct, annualized_pct, mark_price}
    or all-None on failure."""
    empty = {"rate_8h_pct": None, "annualized_pct": None, "mark_price": None}
    for attempt in range(2):
        try:
            r = await client.get(
                BYBIT_TICKER_URL,
                params={"category": "linear", "symbol": CONFIG.symbol},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            ticker = data["result"]["list"][0]
            fr = float(ticker.get("fundingRate") or 0.0)
            mark = ticker.get("markPrice")
            return {
                "rate_8h_pct": fr * 100,
                "annualized_pct": fr * 3 * 365 * 100,
                "mark_price": float(mark) if mark is not None else None,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            if attempt == 1:
                return empty
            await asyncio.sleep(2)
    return empty


async def fetch_binance_spot_mid(client: httpx.AsyncClient) -> float | None:
    """Fetch Binance spot book ticker for the active asset and return the
    mid price. None on failure. Same CDN as the klines fetch — no extra
    outbound host required."""
    for attempt in range(2):
        try:
            r = await client.get(
                BINANCE_SPOT_TICKER_URL,
                params={"symbol": CONFIG.symbol},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            bid = float(data["bidPrice"])
            ask = float(data["askPrice"])
            if bid <= 0 or ask <= 0:
                return None
            return (bid + ask) / 2
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if attempt == 1:
                return None
            await asyncio.sleep(2)
    return None


async def fetch_hyperliquid_funding(client: httpx.AsyncClient) -> dict:
    """Hyperliquid publishes HOURLY funding (not 8h like CEX perps). We
    normalize to the CEX convention so the agent can compare like-for-like.
    Maps the active asset to HL's coin symbol (btc→BTC, eth→ETH)."""
    empty = {"rate_8h_pct": None, "annualized_pct": None}
    coin = CONFIG.asset.upper()
    for attempt in range(2):
        try:
            r = await client.post(
                HYPERLIQUID_INFO_URL,
                json={"type": "metaAndAssetCtxs"},
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            meta, ctxs = data[0], data[1]
            universe = meta.get("universe", [])
            idx = next((i for i, a in enumerate(universe) if a.get("name") == coin), None)
            if idx is None or idx >= len(ctxs):
                return empty
            hourly = float(ctxs[idx].get("funding") or 0.0)
            # Hourly → 8h equivalent, and hourly → annualized (24 * 365).
            return {
                "rate_8h_pct": hourly * 8 * 100,
                "annualized_pct": hourly * 24 * 365 * 100,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            if attempt == 1:
                return empty
            await asyncio.sleep(2)
    return empty


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> Any:
    api_key = os.environ.get("COINALYZE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("COINALYZE_API_KEY not set")
    for attempt in range(2):
        try:
            r = await client.get(
                f"{COINALYZE_BASE}{path}",
                params=params,
                headers={"api_key": api_key},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 1:
                raise
            await asyncio.sleep(2)


async def fetch_all() -> dict:
    """Fetch Coinalyze OI/liquidation endpoints, Bybit ticker, Binance spot
    mid, and Hyperliquid funding — all in parallel. Uses return_exceptions
    so a single endpoint outage (e.g. Coinalyze 503 on /open-interest only)
    does not discard the other sections. The builder then degrades
    per-section with explicit nulls."""
    now = int(time.time())
    from_ts = now - LIQUIDATION_WINDOW_HOURS * 3600
    syms = ",".join(SYMBOLS)

    async with httpx.AsyncClient() as client:
        oi, oi_hist, liq, bybit_ticker, spot_mid, hl_funding = await asyncio.gather(
            _get(client, "/open-interest", {"symbols": syms, "convert_to_usd": "true"}),
            _get(client, "/open-interest-history", {
                "symbols": syms, "interval": LIQUIDATION_INTERVAL,
                "from": from_ts, "to": now, "convert_to_usd": "true",
            }),
            _get(client, "/liquidation-history", {
                "symbols": syms, "interval": LIQUIDATION_INTERVAL,
                "from": from_ts, "to": now, "convert_to_usd": "true",
            }),
            fetch_bybit_ticker(client),
            fetch_binance_spot_mid(client),
            fetch_hyperliquid_funding(client),
            return_exceptions=True,
        )

    def _ok_list(v):
        return v if isinstance(v, list) else []

    def _ok_dict(v, default):
        return v if isinstance(v, dict) else default

    def _ok_float(v):
        return v if isinstance(v, (int, float)) else None

    bybit = _ok_dict(bybit_ticker, {"rate_8h_pct": None, "annualized_pct": None, "mark_price": None})
    funding = {"rate_8h_pct": bybit.get("rate_8h_pct"), "annualized_pct": bybit.get("annualized_pct")}
    perp_mark = bybit.get("mark_price")
    spot = _ok_float(spot_mid)

    return build_derivatives_payload(
        open_interest_raw=_ok_list(oi),
        open_interest_history_raw=_ok_list(oi_hist),
        liquidations_raw=_ok_list(liq),
        funding=funding,
        funding_hyperliquid=_ok_dict(hl_funding, _empty_funding()),
        spot_mid=spot,
        perp_mark=perp_mark,
    )
