# Crypto-Swings Price-Action & Volume Intelligence Layer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five orthogonal price-action layers (volume-aggregated VP, anchored VWAP, FVG, order blocks, market structure) to the BTC/ETH pipeline, unify them with existing fib + liquidity-pool layers via a single confluence scorer, and rewrite the analyst agent to reason with all sources (not just fib).

**Architecture:** Pure-compute modules that consume the existing OHLC/swing-pair infrastructure. A new `venue_aggregator` fetches from Binance + Bybit + Coinbase and produces a single aggregated OHLCV per TF per asset for VP/AVWAP use (swings/fibs continue to use Binance-only to keep historic comparability). A unified `Level` schema lets all sources feed the same clustering engine; zones are classified by distinct-source count and the analyst prompt reads source-tagged confluence, not just fib math.

**Tech Stack:** Python 3.12, httpx async, pandas, pytest, pytest-asyncio. No new runtime dependencies.

---

## File Structure

**New modules (all under `src/`):**
- `src/venue_aggregator.py` — Binance + Bybit + Coinbase OHLCV fetch + aggregation (for VP/AVWAP only)
- `src/avwap.py` — Anchored VWAP with bands
- `src/volume_profile.py` — Composite VP + periodic naked POCs
- `src/fvg.py` — Fair Value Gap detection with mitigation tracking
- `src/order_blocks.py` — ICT order blocks with 1.5×ATR displacement filter
- `src/market_structure.py` — BOS / CHoCH from swing pivots
- `src/levels.py` — Unified Level schema + multi-source confluence clustering

**Modified:**
- `src/types.py` — Extend TFs unchanged; add Level dataclass
- `src/main.py` — Wire new modules into pipeline; replace fib-only confluence with unified
- `scripts/emit_payload.py` — Extend payload with new source-tagged zones + diagnostics
- `.claude/agents/crypto-swings-analyst.md` — Full rewrite of Analysis Framework

**New tests (under `tests/`):**
- `tests/test_venue_aggregator.py`
- `tests/test_avwap.py`
- `tests/test_volume_profile.py`
- `tests/test_fvg.py`
- `tests/test_order_blocks.py`
- `tests/test_market_structure.py`
- `tests/test_levels.py`

---

## Worktree Setup

- [ ] **Step 0.1: Create worktree**

Run:
```bash
cd ~/Documents/Intelligence/crypto-swings
git worktree add -b feature/price-action-layer ../crypto-swings-pa
cd ../crypto-swings-pa
uv sync
```
Expected: `../crypto-swings-pa` created with a fresh branch `feature/price-action-layer` off `main`. All subsequent paths are relative to this worktree.

- [ ] **Step 0.2: Verify tests pass on the new branch**

Run: `uv run pytest -x`
Expected: all existing tests pass (baseline before we change anything).

---

## Task 1: Unified Level schema

**Files:**
- Modify: `src/types.py`
- Create: `src/levels.py`
- Create: `tests/test_levels.py`

- [ ] **Step 1.1: Add Level + LevelSource to types.py**

Append to `src/types.py`:
```python
from typing import Literal

LevelSource = Literal[
    # Fibonacci
    "FIB_236", "FIB_382", "FIB_500", "FIB_618", "FIB_786",
    "FIB_1272", "FIB_1618",
    # Liquidity pools (from swing pivots)
    "LIQ_BSL", "LIQ_SSL",
    # Volume profile
    "POC", "VAH", "VAL", "HVN", "LVN",
    "NAKED_POC_D", "NAKED_POC_W", "NAKED_POC_M",
    # Anchored VWAP
    "AVWAP_SESSION", "AVWAP_WEEK", "AVWAP_MONTH",
    "AVWAP_SWING_HH", "AVWAP_SWING_LL", "AVWAP_EVENT",
    "AVWAP_BAND_1SD_UP", "AVWAP_BAND_1SD_DOWN",
    "AVWAP_BAND_2SD_UP", "AVWAP_BAND_2SD_DOWN",
    # FVG / Order Blocks
    "FVG_BULL", "FVG_BEAR",
    "OB_BULL", "OB_BEAR",
    # Market structure key levels
    "MS_BOS_LEVEL", "MS_CHOCH_LEVEL", "MS_INVALIDATION",
]

@dataclass(frozen=True)
class Level:
    """Canonical source-tagged level, used for unified confluence clustering."""
    price: float              # representative price (single-point or zone midpoint)
    min_price: float          # for zone sources (FVG, OB, VP value area); = price for point sources
    max_price: float          # idem
    source: LevelSource
    tf: Timeframe
    strength: float           # 0-1 normalized source-specific strength
    age_bars: int
    meta: dict = field(default_factory=dict)
```

And add the import `from dataclasses import field` at the top if missing.

- [ ] **Step 1.2: Write failing test for multi-source clustering**

Create `tests/test_levels.py`:
```python
from src.levels import cluster_levels, MultiSourceZone
from src.types import Level

def _lvl(price, source, tf="1d", strength=0.5, age=0, lo=None, hi=None):
    lo = lo if lo is not None else price
    hi = hi if hi is not None else price
    return Level(
        price=price, min_price=lo, max_price=hi,
        source=source, tf=tf, strength=strength, age_bars=age,
    )

def test_cluster_levels_groups_within_radius():
    levels = [
        _lvl(100.0, "FIB_618", tf="1d"),
        _lvl(100.2, "LIQ_BSL", tf="1d"),
        _lvl(102.0, "POC",     tf="1d"),
    ]
    zones = cluster_levels(levels, radius=0.5)
    assert len(zones) == 2
    assert zones[0].source_count == 2
    assert {"FIB_618", "LIQ_BSL"} <= {l.source for l in zones[0].levels}

def test_cluster_levels_score_rewards_source_diversity():
    # Two sources > three same-source hits
    two_src = cluster_levels([
        _lvl(100, "FIB_618", tf="1w"),
        _lvl(100, "LIQ_BSL", tf="1w"),
    ], radius=0.1)[0]
    three_same = cluster_levels([
        _lvl(100, "FIB_618", tf="1w"),
        _lvl(100, "FIB_618", tf="1d"),
        _lvl(100, "FIB_618", tf="4h"),
    ], radius=0.1)[0]
    assert two_src.score > three_same.score
```

- [ ] **Step 1.3: Run failing test**

Run: `uv run pytest tests/test_levels.py -v`
Expected: FAIL — `src.levels` module not found.

- [ ] **Step 1.4: Implement src/levels.py**

Create `src/levels.py`:
```python
"""Unified multi-source confluence clustering.

Groups Levels from heterogeneous sources (fib, liquidity, VP, AVWAP, FVG,
OB, market structure) into zones. Zone score rewards DISTINCT source count
far more than raw level count — two sources agreeing is a stronger signal
than five fib retracements from the same swing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable

from src.types import Level, TF_WEIGHTS, Timeframe

# Canonical single-source family groupings — rules out "two kinds of FIB"
# being counted as multi-source confluence.
SOURCE_FAMILY: dict[str, str] = {
    **{f"FIB_{r}": "FIB" for r in ("236", "382", "500", "618", "786", "1272", "1618")},
    "LIQ_BSL": "LIQ", "LIQ_SSL": "LIQ",
    "POC": "VP", "VAH": "VP", "VAL": "VP", "HVN": "VP", "LVN": "VP",
    "NAKED_POC_D": "NAKED_POC", "NAKED_POC_W": "NAKED_POC", "NAKED_POC_M": "NAKED_POC",
    "AVWAP_SESSION": "AVWAP", "AVWAP_WEEK": "AVWAP", "AVWAP_MONTH": "AVWAP",
    "AVWAP_SWING_HH": "AVWAP_SWING", "AVWAP_SWING_LL": "AVWAP_SWING",
    "AVWAP_EVENT": "AVWAP_EVENT",
    "AVWAP_BAND_1SD_UP": "AVWAP_BAND", "AVWAP_BAND_1SD_DOWN": "AVWAP_BAND",
    "AVWAP_BAND_2SD_UP": "AVWAP_BAND", "AVWAP_BAND_2SD_DOWN": "AVWAP_BAND",
    "FVG_BULL": "FVG", "FVG_BEAR": "FVG",
    "OB_BULL": "OB", "OB_BEAR": "OB",
    "MS_BOS_LEVEL": "MS", "MS_CHOCH_LEVEL": "MS", "MS_INVALIDATION": "MS",
}

MAX_ZONE_WIDTH_MULTIPLIER = 2.0


@dataclass(frozen=True)
class MultiSourceZone:
    min_price: float
    max_price: float
    levels: tuple[Level, ...]
    source_count: int         # distinct families
    score: float
    classification: str       # "strong" | "confluence" | "level"

    @property
    def mid(self) -> float:
        return (self.min_price + self.max_price) / 2


def cluster_levels(levels: Iterable[Level], radius: float) -> list[MultiSourceZone]:
    """Cluster by price using the same radius+width cap as fib clustering.
    Returns zones sorted by price ascending."""
    lvl_list = sorted(levels, key=lambda l: l.price)
    if not lvl_list:
        return []
    groups: list[list[Level]] = [[lvl_list[0]]]
    max_width = radius * MAX_ZONE_WIDTH_MULTIPLIER
    for l in lvl_list[1:]:
        near = l.price - groups[-1][-1].price <= radius
        within = l.price - groups[-1][0].price <= max_width
        if near and within:
            groups[-1].append(l)
        else:
            groups.append([l])
    return [_build_zone(g) for g in groups]


def _build_zone(group: list[Level]) -> MultiSourceZone:
    min_p = min(l.min_price for l in group)
    max_p = max(l.max_price for l in group)
    families = {SOURCE_FAMILY.get(l.source, l.source) for l in group}
    source_count = len(families)
    score = _score(group, families)
    cls = _classify(source_count, families)
    return MultiSourceZone(
        min_price=min_p, max_price=max_p,
        levels=tuple(group),
        source_count=source_count,
        score=round(score, 2),
        classification=cls,
    )


def _score(group: list[Level], families: set[str]) -> float:
    """Scoring:
      - +3 per distinct source family (heavily rewards orthogonal agreement)
      - +TF_WEIGHT * strength per individual level contribution
      - +HTF bonus multiplier applied to the zone total when any HTF source
        (1w, 1M) contributes: 1.25x for 1w, 1.5x for 1M.
    """
    base = 3.0 * len(families)
    base += sum(TF_WEIGHTS.get(l.tf, 1) * l.strength for l in group)
    tfs = {l.tf for l in group}
    if "1M" in tfs:
        base *= 1.5
    elif "1w" in tfs:
        base *= 1.25
    return base


def _classify(source_count: int, families: set[str]) -> str:
    if source_count >= 3:
        return "strong"
    if source_count == 2:
        # Structural pivot = MS present with any other source
        if "MS" in families:
            return "structural_pivot"
        return "confluence"
    return "level"


def split_by_price(
    zones: list[MultiSourceZone], current_price: float
) -> tuple[list[MultiSourceZone], list[MultiSourceZone]]:
    """Support below, resistance above; straddling zones go by midpoint."""
    support, resistance = [], []
    for z in zones:
        if z.mid < current_price:
            support.append(z)
        else:
            resistance.append(z)
    support.sort(key=lambda z: z.score, reverse=True)
    resistance.sort(key=lambda z: z.score, reverse=True)
    return support, resistance
```

- [ ] **Step 1.5: Run tests to verify**

Run: `uv run pytest tests/test_levels.py -v`
Expected: PASS.

- [ ] **Step 1.6: Commit**

```bash
git add src/types.py src/levels.py tests/test_levels.py
git commit -m "feat(levels): add unified source-tagged Level schema + multi-source confluence"
```

---

## Task 2: Venue aggregator (Binance + Bybit + Coinbase)

**Files:**
- Create: `src/venue_aggregator.py`
- Create: `tests/test_venue_aggregator.py`

- [ ] **Step 2.1: Failing test — aggregator merges per-bar volume across venues**

Create `tests/test_venue_aggregator.py`:
```python
from src.venue_aggregator import aggregate_bars
from src.types import OHLC

def _b(ts, o, h, l, c, v):
    return OHLC(ts=ts, open=o, high=h, low=l, close=c, volume=v)

def test_aggregate_bars_sums_volume_across_venues():
    binance = [_b(1000, 100, 101, 99, 100.5, 10.0)]
    bybit   = [_b(1000, 100, 101, 99, 100.5,  3.0)]
    coinbase= [_b(1000, 100, 101, 99, 100.5,  2.0)]
    out = aggregate_bars({"binance": binance, "bybit": bybit, "coinbase": coinbase})
    assert len(out) == 1
    assert out[0].ts == 1000
    assert out[0].volume == 15.0
    # OHLC taken from the PRIMARY (binance) for reference-price stability
    assert out[0].close == 100.5

def test_aggregate_bars_uses_union_of_timestamps():
    binance = [_b(1000, 100, 101, 99, 100.5, 10.0)]
    bybit   = [_b(2000, 101, 102, 100, 101.5, 5.0)]
    out = aggregate_bars({"binance": binance, "bybit": bybit})
    timestamps = [b.ts for b in out]
    assert timestamps == [1000, 2000]

def test_aggregate_bars_missing_primary_falls_back_to_first_present():
    # No Binance — should still return bars, OHLC from bybit
    bybit = [_b(1000, 100, 101, 99, 100.5, 3.0)]
    out = aggregate_bars({"binance": [], "bybit": bybit})
    assert len(out) == 1
    assert out[0].volume == 3.0
```

- [ ] **Step 2.2: Run failing test**

Run: `uv run pytest tests/test_venue_aggregator.py::test_aggregate_bars_sums_volume_across_venues -v`
Expected: FAIL — module missing.

- [ ] **Step 2.3: Implement src/venue_aggregator.py**

Create `src/venue_aggregator.py`:
```python
"""Cross-venue OHLCV aggregation for volume-profile and AVWAP.

Binance remains the primary (reference) venue: OHLC per timestamp is taken
from Binance when present (stable reference for price levels). Volume is
SUMMED across Binance + Bybit + Coinbase — that's the whole point of
aggregation.

For swing/fib computation we continue to use Binance-only (keeps historic
comparability and is already well-tested). Aggregation is specifically for
VP + AVWAP where volume fidelity dominates.

Fetch endpoints (all public, no auth, US-cloud accessible):
  - Binance spot:  data-api.binance.vision/api/v3/klines
  - Bybit spot:    api.bybit.com/v5/market/kline?category=spot
  - Coinbase:      api.exchange.coinbase.com/products/{product}/candles

Coinbase supports granularities {60, 300, 900, 3600, 21600, 86400}s —
1h + 1d native. 4h/1w/1M get resampled from 1h (4h) or 1d (1w/1M).
"""
from __future__ import annotations
import asyncio
import httpx
from collections import defaultdict
from typing import Iterable

from src.types import OHLC, Timeframe

BINANCE_URL  = "https://data-api.binance.vision/api/v3/klines"
BYBIT_URL    = "https://api.bybit.com/v5/market/kline"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

# Bybit interval codes
BYBIT_INTERVAL: dict[Timeframe, str] = {
    "1M": "M", "1w": "W", "1d": "D", "4h": "240", "1h": "60",
}

# Coinbase supports only native granularities. 4h / 1w / 1M resampled.
COINBASE_GRANULARITY: dict[Timeframe, int | None] = {
    "1M": None,    # resample from 1d
    "1w": None,    # resample from 1d
    "1d": 86400,
    "4h": None,    # resample from 1h
    "1h": 3600,
}

TF_BARS_PER_COARSER: dict[Timeframe, dict[Timeframe, int]] = {
    # How many Xh bars fit into one of the coarser TF
    "4h": {"1h": 4},
    "1w": {"1d": 7},
    "1M": {"1d": 30},   # nominal; month boundaries handled by timestamp floor
}


def _coinbase_product(symbol: str) -> str:
    """BTCUSDT -> BTC-USD, ETHUSDT -> ETH-USD.
    Coinbase trades vs USD, not USDT. Quote-currency drift accepted as <1 bin
    noise given ATR-relative bin width used in VP."""
    base = symbol.replace("USDT", "")
    return f"{base}-USD"


def aggregate_bars(
    by_venue: dict[str, list[OHLC]],
    primary: str = "binance",
) -> list[OHLC]:
    """Merge per-venue bars by timestamp. Volume SUMMED. OHLC taken from
    `primary` when present, otherwise from the first venue that has a bar
    at that timestamp (stable preference order)."""
    buckets: dict[int, dict[str, OHLC]] = defaultdict(dict)
    for venue, bars in by_venue.items():
        for b in bars:
            buckets[b.ts][venue] = b
    preference_order = [primary] + [v for v in by_venue if v != primary]
    out: list[OHLC] = []
    for ts in sorted(buckets):
        venue_bars = buckets[ts]
        ref: OHLC | None = None
        for v in preference_order:
            if v in venue_bars:
                ref = venue_bars[v]
                break
        if ref is None:
            continue
        total_vol = sum(b.volume for b in venue_bars.values())
        out.append(OHLC(
            ts=ts, open=ref.open, high=ref.high, low=ref.low,
            close=ref.close, volume=total_vol,
            taker_buy_volume=ref.taker_buy_volume,
        ))
    return out


# ---- Fetch adapters ----

async def fetch_bybit(
    client: httpx.AsyncClient, symbol: str, tf: Timeframe, limit: int
) -> list[OHLC]:
    """Bybit V5 spot kline. Returns [] on error."""
    try:
        r = await client.get(
            BYBIT_URL,
            params={
                "category": "spot", "symbol": symbol,
                "interval": BYBIT_INTERVAL[tf], "limit": str(limit),
            },
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("result", {}).get("list", [])
        # Bybit returns newest-first; reverse for chronological
        out: list[OHLC] = []
        for row in reversed(rows):
            out.append(OHLC(
                ts=int(row[0]),
                open=float(row[1]), high=float(row[2]),
                low=float(row[3]),  close=float(row[4]),
                volume=float(row[5]),
            ))
        return out
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return []


async def fetch_coinbase_native(
    client: httpx.AsyncClient, product: str, granularity: int, limit: int = 300
) -> list[OHLC]:
    """Coinbase exchange candles. Returns [[ts, low, high, open, close, vol], ...].
    Response is newest-first. Limit capped at 300 per request."""
    try:
        r = await client.get(
            COINBASE_URL.format(product=product),
            params={"granularity": str(granularity)},
            timeout=10.0,
        )
        r.raise_for_status()
        rows = r.json()
        out: list[OHLC] = []
        for row in reversed(rows):
            # timestamp is in seconds → convert to ms for consistency
            out.append(OHLC(
                ts=int(row[0]) * 1000,
                open=float(row[3]), high=float(row[2]),
                low=float(row[1]),  close=float(row[4]),
                volume=float(row[5]),
            ))
        return out
    except (httpx.HTTPError, ValueError, IndexError):
        return []


def resample(bars: list[OHLC], tf_from: Timeframe, tf_to: Timeframe) -> list[OHLC]:
    """Group `tf_from` bars into `tf_to` bars by timestamp floor. OHLC rebuilt
    from first/max/min/last; volume summed. Used for Coinbase 4h (from 1h),
    1w (from 1d), 1M (from 1d)."""
    if not bars:
        return []
    bucket_ms = _bucket_ms(tf_to)
    buckets: dict[int, list[OHLC]] = defaultdict(list)
    for b in bars:
        key = (b.ts // bucket_ms) * bucket_ms
        buckets[key].append(b)
    out: list[OHLC] = []
    for ts in sorted(buckets):
        grp = buckets[ts]
        out.append(OHLC(
            ts=ts,
            open=grp[0].open,
            high=max(b.high for b in grp),
            low=min(b.low for b in grp),
            close=grp[-1].close,
            volume=sum(b.volume for b in grp),
        ))
    return out


def _bucket_ms(tf: Timeframe) -> int:
    return {
        "1h":  3_600_000,
        "4h":  14_400_000,
        "1d":  86_400_000,
        "1w":  7 * 86_400_000,
        "1M":  30 * 86_400_000,   # nominal; OK for bucketing into 30d windows
    }[tf]


async def fetch_coinbase(
    client: httpx.AsyncClient, symbol: str, tf: Timeframe
) -> list[OHLC]:
    product = _coinbase_product(symbol)
    g = COINBASE_GRANULARITY[tf]
    if g is not None:
        return await fetch_coinbase_native(client, product, g)
    # Resampled path: 4h from 1h; 1w/1M from 1d
    if tf == "4h":
        src = await fetch_coinbase_native(client, product, 3600)
        return resample(src, "1h", "4h")
    # 1w or 1M → from 1d
    src = await fetch_coinbase_native(client, product, 86400)
    return resample(src, "1d", tf)


async def fetch_all_venues(
    symbol: str, tf: Timeframe, limit: int,
) -> dict[str, list[OHLC]]:
    """Fetch one TF across Binance, Bybit, Coinbase in parallel. Binance is
    fetched by the caller via src.fetch — we do not re-fetch it here; the
    caller passes Binance bars in.

    This function returns Bybit + Coinbase bars only; merge with Binance at
    the call site using `aggregate_bars`."""
    async with httpx.AsyncClient() as client:
        bybit, coinbase = await asyncio.gather(
            fetch_bybit(client, symbol, tf, limit),
            fetch_coinbase(client, symbol, tf),
            return_exceptions=True,
        )
    return {
        "bybit": bybit if isinstance(bybit, list) else [],
        "coinbase": coinbase if isinstance(coinbase, list) else [],
    }
```

- [ ] **Step 2.4: Write integration test for resample**

Append to `tests/test_venue_aggregator.py`:
```python
from src.venue_aggregator import resample

def test_resample_1h_to_4h():
    # 4 consecutive 1h bars → one 4h bar
    bars = [
        _b(0,            100, 105, 99,  103, 10.0),
        _b(3_600_000,    103, 108, 102, 107, 12.0),
        _b(7_200_000,    107, 110, 106, 109, 15.0),
        _b(10_800_000,   109, 112, 108, 111, 8.0),
    ]
    out = resample(bars, "1h", "4h")
    assert len(out) == 1
    assert out[0].open == 100
    assert out[0].high == 112
    assert out[0].low == 99
    assert out[0].close == 111
    assert out[0].volume == 45.0
```

- [ ] **Step 2.5: Run tests to verify**

Run: `uv run pytest tests/test_venue_aggregator.py -v`
Expected: all PASS.

- [ ] **Step 2.6: Live smoke-test against Bybit + Coinbase**

Run:
```bash
uv run python -c "
import asyncio
from src.venue_aggregator import fetch_all_venues
bars = asyncio.run(fetch_all_venues('BTCUSDT', '1h', 10))
print('bybit:',    len(bars['bybit']),    'sample:', bars['bybit'][-1] if bars['bybit'] else 'EMPTY')
print('coinbase:', len(bars['coinbase']), 'sample:', bars['coinbase'][-1] if bars['coinbase'] else 'EMPTY')
"
```
Expected: both venues return ≥1 bar. If either returns empty on multiple retries, halt — do NOT commit aggregator until both endpoints are verified reachable.

- [ ] **Step 2.7: Commit**

```bash
git add src/venue_aggregator.py tests/test_venue_aggregator.py
git commit -m "feat(venue_aggregator): add Binance+Bybit+Coinbase OHLCV aggregation"
```

---

## Task 3: Anchored VWAP

**Files:**
- Create: `src/avwap.py`
- Create: `tests/test_avwap.py`

- [ ] **Step 3.1: Failing test — AVWAP with bands**

Create `tests/test_avwap.py`:
```python
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
    # 24 hourly bars, one swing pair with high at bar 10 and low at bar 20
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
```

- [ ] **Step 3.2: Run test to fail**

Run: `uv run pytest tests/test_avwap.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3.3: Implement src/avwap.py**

Create `src/avwap.py`:
```python
"""Anchored VWAP with volume-weighted standard deviation bands.

VWAP(t) from anchor A = Σ(typical_price_i * volume_i) / Σ(volume_i) for i>=A.
Bands use the volume-weighted variance:
    var(t) = Σ(vol_i * (typ_i - vwap_t)^2) / Σ(vol_i).

Anchors emitted per asset (from `resolve_anchors`):
  - AVWAP_SESSION: last UTC-day open
  - AVWAP_WEEK:    last Monday 00:00 UTC
  - AVWAP_MONTH:   last 1st-of-month 00:00 UTC
  - AVWAP_SWING_HH: most recent significant swing high pivot
  - AVWAP_SWING_LL: most recent significant swing low pivot
  - AVWAP_EVENT:   fixed-list events (halving 2024-04-20, spot ETF 2024-01-10)
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from src.types import OHLC, SwingPair

# Fixed event anchors (Unix ms UTC). Extend cautiously — each entry becomes
# an AVWAP line on every chart.
EVENT_ANCHORS: list[tuple[str, int]] = [
    ("halving_2024",   int(datetime(2024, 4, 20, tzinfo=timezone.utc).timestamp() * 1000)),
    ("spot_etf_2024",  int(datetime(2024, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)),
]


@dataclass(frozen=True)
class AnchoredVwap:
    anchor_type: str       # LevelSource value (AVWAP_SESSION, AVWAP_WEEK, …)
    anchor_ts: int
    vwap: list[float]      # same length as input bars; pre-anchor entries = NaN
    upper_1sd: list[float]
    lower_1sd: list[float]
    upper_2sd: list[float]
    lower_2sd: list[float]


def compute_avwap(
    bars: list[OHLC], *, anchor_idx: int, anchor_type: str, anchor_ts: int,
) -> AnchoredVwap:
    """Compute AVWAP + ±1σ and ±2σ bands from `anchor_idx` onward."""
    n = len(bars)
    vwap = [math.nan] * n
    upper_1 = [math.nan] * n
    lower_1 = [math.nan] * n
    upper_2 = [math.nan] * n
    lower_2 = [math.nan] * n

    cum_pv = 0.0
    cum_v = 0.0
    cum_pv2 = 0.0   # Σ(v * typ^2), enables variance via E[X^2] - E[X]^2
    for i in range(anchor_idx, n):
        b = bars[i]
        typ = (b.high + b.low + b.close) / 3.0
        v = b.volume
        cum_pv  += typ * v
        cum_v   += v
        cum_pv2 += typ * typ * v
        if cum_v > 0:
            mean = cum_pv / cum_v
            var = max(0.0, (cum_pv2 / cum_v) - (mean * mean))
            sd = math.sqrt(var)
            vwap[i]    = mean
            upper_1[i] = mean + sd
            lower_1[i] = mean - sd
            upper_2[i] = mean + 2 * sd
            lower_2[i] = mean - 2 * sd
    return AnchoredVwap(
        anchor_type=anchor_type, anchor_ts=anchor_ts,
        vwap=vwap, upper_1sd=upper_1, lower_1sd=lower_1,
        upper_2sd=upper_2, lower_2sd=lower_2,
    )


def _find_idx_for_ts(bars: list[OHLC], target_ts: int) -> int | None:
    """First bar whose timestamp >= target_ts. None if all bars are before."""
    for i, b in enumerate(bars):
        if b.ts >= target_ts:
            return i
    return None


def _session_start_ts(last_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp() * 1000)


def _week_start_ts(last_ts_ms: int) -> int:
    from datetime import timedelta
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    # Monday = weekday() == 0. Roll back to Monday then floor to 00:00 UTC.
    monday = (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return int(monday.timestamp() * 1000)


def _month_start_ts(last_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def resolve_anchors(
    bars: list[OHLC], swing_pairs: list[SwingPair],
) -> list[tuple[str, int, int]]:
    """Return list of (anchor_type, anchor_idx, anchor_ts). Skips anchors
    that fall outside the bar window (too-old anchor has no coverage)."""
    if not bars:
        return []
    out: list[tuple[str, int, int]] = []
    last_ts = bars[-1].ts
    candidates = [
        ("AVWAP_SESSION", _session_start_ts(last_ts)),
        ("AVWAP_WEEK",    _week_start_ts(last_ts)),
        ("AVWAP_MONTH",   _month_start_ts(last_ts)),
    ]
    for anchor_type, ts in candidates:
        idx = _find_idx_for_ts(bars, ts)
        if idx is not None:
            out.append((anchor_type, idx, bars[idx].ts))

    # Swing anchors — take the most recent HH and most recent LL
    if swing_pairs:
        latest = swing_pairs[-1]
        idx_hh = _find_idx_for_ts(bars, latest.high_ts)
        if idx_hh is not None:
            out.append(("AVWAP_SWING_HH", idx_hh, bars[idx_hh].ts))
        idx_ll = _find_idx_for_ts(bars, latest.low_ts)
        if idx_ll is not None:
            out.append(("AVWAP_SWING_LL", idx_ll, bars[idx_ll].ts))

    # Event anchors (fixed list)
    for _, event_ts in EVENT_ANCHORS:
        idx = _find_idx_for_ts(bars, event_ts)
        if idx is not None:
            out.append(("AVWAP_EVENT", idx, bars[idx].ts))

    return out
```

- [ ] **Step 3.4: Run tests**

Run: `uv run pytest tests/test_avwap.py -v`
Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/avwap.py tests/test_avwap.py
git commit -m "feat(avwap): add anchored VWAP with ±1σ/±2σ bands and session/week/month/swing/event anchors"
```

---

## Task 4: Volume Profile (composite + naked POC tracker)

**Files:**
- Create: `src/volume_profile.py`
- Create: `tests/test_volume_profile.py`

- [ ] **Step 4.1: Failing test — composite VP emits POC/VAH/VAL**

Create `tests/test_volume_profile.py`:
```python
from src.volume_profile import compute_profile, compute_naked_pocs
from src.types import OHLC

def _b(ts, h, l, v, c=None):
    c = c if c is not None else (h + l) / 2
    return OHLC(ts=ts, open=c, high=h, low=l, close=c, volume=v)

def test_compute_profile_finds_poc_at_concentration():
    # 10 bars all at price 100 with 1 unit vol; one bar at 110 with 50 units.
    # POC must be at 110.
    bars = [_b(i, 101, 99, 1.0) for i in range(10)]
    bars.append(_b(11, 111, 109, 50.0))
    profile = compute_profile(bars, atr_14=2.0)
    assert 109 <= profile.poc <= 111

def test_compute_profile_value_area_brackets_poc():
    bars = [_b(i, 101, 99, 10.0) for i in range(20)]
    profile = compute_profile(bars, atr_14=2.0)
    assert profile.val <= profile.poc <= profile.vah

def test_naked_poc_flagged_when_price_never_returned():
    # First 10 bars form a daily window with POC ~100
    # Next 10 bars trade entirely above 105 (never back to POC)
    day1 = [_b(i, 101, 99, 10.0)          for i in range(10)]
    day2 = [_b(10 + i, 110, 106, 10.0)    for i in range(10)]
    all_bars = day1 + day2
    pocs = compute_naked_pocs(all_bars, period_ms=10, lookback=2, atr_14=2.0)
    # At least one naked POC
    assert any(p.is_naked for p in pocs)
```

- [ ] **Step 4.2: Run test to fail**

Run: `uv run pytest tests/test_volume_profile.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4.3: Implement src/volume_profile.py**

Create `src/volume_profile.py`:
```python
"""Volume profile (composite + periodic naked POCs).

Composite profile: distribute each bar's volume uniformly across the bins
its [low, high] range covers. POC = bin with max volume. Value area =
expand outward from POC until 70% of total volume is captured; VAH/VAL =
top/bottom of that band.

Naked POC: periodic (daily / weekly / monthly) POC that price has NOT
revisited within ±0.25 × ATR since the period closed. Strong magnet.

Bin width = 0.1 × ATR(14) → normalizes across BTC-at-$100k vs ETH-at-$3k.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

from src.types import OHLC

BIN_ATR_MULT = 0.1
VALUE_AREA_PCT = 0.70


@dataclass(frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    hvn: list[float]    # high-volume nodes: local-max bins with z-score > 1.5
    lvn: list[float]    # low-volume nodes (rejection zones)
    bin_width: float


@dataclass(frozen=True)
class NakedPOC:
    price: float
    period_start_ts: int
    period_end_ts: int
    is_naked: bool
    distance_atr: float | None   # abs distance from current price in ATR units


def compute_profile(bars: list[OHLC], atr_14: float) -> VolumeProfile:
    if not bars or atr_14 <= 0:
        return VolumeProfile(poc=0, vah=0, val=0, hvn=[], lvn=[], bin_width=0)
    lo = min(b.low for b in bars)
    hi = max(b.high for b in bars)
    bw = atr_14 * BIN_ATR_MULT
    if bw <= 0 or hi <= lo:
        return VolumeProfile(poc=(hi + lo) / 2, vah=hi, val=lo, hvn=[], lvn=[], bin_width=bw)
    n_bins = max(1, int(math.ceil((hi - lo) / bw)))
    mass = [0.0] * n_bins
    for b in bars:
        span = max(1e-12, b.high - b.low)
        vpp = b.volume / span  # volume per unit price
        # Contribute to every bin overlapped by [b.low, b.high]
        for i in range(n_bins):
            bin_lo = lo + i * bw
            bin_hi = bin_lo + bw
            overlap = max(0.0, min(bin_hi, b.high) - max(bin_lo, b.low))
            if overlap > 0:
                mass[i] += vpp * overlap
    # POC
    poc_idx = max(range(n_bins), key=lambda i: mass[i])
    poc_price = lo + (poc_idx + 0.5) * bw
    # Value area — expand from POC outward
    total = sum(mass)
    target = total * VALUE_AREA_PCT
    lo_i, hi_i = poc_idx, poc_idx
    acc = mass[poc_idx]
    while acc < target and (lo_i > 0 or hi_i < n_bins - 1):
        left = mass[lo_i - 1] if lo_i > 0 else -1
        right = mass[hi_i + 1] if hi_i < n_bins - 1 else -1
        if right >= left:
            hi_i += 1; acc += mass[hi_i]
        else:
            lo_i -= 1; acc += mass[lo_i]
    vah = lo + (hi_i + 1) * bw
    val = lo + lo_i * bw
    # HVN / LVN via simple z-score on bin mass
    if len(mass) >= 3:
        mean = sum(mass) / len(mass)
        var = sum((m - mean) ** 2 for m in mass) / len(mass)
        sd = math.sqrt(var) if var > 0 else 0.0
        hvn = [lo + (i + 0.5) * bw for i, m in enumerate(mass) if sd > 0 and (m - mean) / sd > 1.5]
        lvn = [lo + (i + 0.5) * bw for i, m in enumerate(mass) if sd > 0 and (m - mean) / sd < -1.0]
    else:
        hvn, lvn = [], []
    return VolumeProfile(poc=poc_price, vah=vah, val=val, hvn=hvn, lvn=lvn, bin_width=bw)


def compute_naked_pocs(
    bars: list[OHLC], *, period_ms: int, lookback: int, atr_14: float,
) -> list[NakedPOC]:
    """Slice bars into `lookback` most recent complete periods of `period_ms`,
    compute per-period POC, flag as naked if price never re-visited within
    ±0.25 ATR since period close."""
    if not bars or atr_14 <= 0 or lookback <= 0:
        return []
    end_ts = bars[-1].ts
    touch_radius = atr_14 * 0.25
    out: list[NakedPOC] = []
    for k in range(1, lookback + 1):
        period_end = end_ts - (k - 1) * period_ms
        period_start = period_end - period_ms
        window = [b for b in bars if period_start <= b.ts < period_end]
        if not window:
            continue
        vp = compute_profile(window, atr_14)
        post = [b for b in bars if b.ts >= period_end]
        visited = any(abs((b.high + b.low) / 2 - vp.poc) <= touch_radius
                      or (b.low <= vp.poc <= b.high) for b in post)
        out.append(NakedPOC(
            price=vp.poc,
            period_start_ts=period_start,
            period_end_ts=period_end,
            is_naked=not visited,
            distance_atr=abs(bars[-1].close - vp.poc) / atr_14,
        ))
    return out
```

- [ ] **Step 4.4: Run tests**

Run: `uv run pytest tests/test_volume_profile.py -v`
Expected: PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/volume_profile.py tests/test_volume_profile.py
git commit -m "feat(volume_profile): add composite VP (POC/VAH/VAL/HVN/LVN) + naked POC tracker"
```

---

## Task 5: Fair Value Gaps

**Files:**
- Create: `src/fvg.py`
- Create: `tests/test_fvg.py`

- [ ] **Step 5.1: Failing test — FVG detection + mitigation + stale flag**

Create `tests/test_fvg.py`:
```python
from src.fvg import detect_fvgs, FVG
from src.types import OHLC

def _b(ts, h, l, c=None):
    c = c if c is not None else (h + l) / 2
    return OHLC(ts=ts, open=c, high=h, low=l, close=c, volume=1.0)

def test_bullish_fvg_formed_when_bar_after_gaps_above_bar_before():
    # Classic 3-bar bull FVG: bar[0] high=100, bar[1] displacement,
    # bar[2] low=102 > bar[0] high.
    bars = [_b(0, 100, 98), _b(1, 103, 99, c=102.5), _b(2, 105, 102)]
    fvgs = detect_fvgs(bars, tf="1h", atr_14=1.0, stale_after=100)
    bulls = [f for f in fvgs if f.type == "FVG_BULL"]
    assert len(bulls) == 1
    assert bulls[0].lo == 100  # bar[0].high
    assert bulls[0].hi == 102  # bar[2].low

def test_bearish_fvg_formed_when_bar_after_gaps_below_bar_before():
    bars = [_b(0, 105, 100), _b(1, 102, 95, c=97), _b(2, 98, 93)]
    fvgs = detect_fvgs(bars, tf="1h", atr_14=1.0, stale_after=100)
    bears = [f for f in fvgs if f.type == "FVG_BEAR"]
    assert len(bears) == 1
    assert bears[0].hi == 100
    assert bears[0].lo == 98

def test_fvg_marked_mitigated_when_price_returns_into_gap():
    bars = [
        _b(0, 100, 98),  _b(1, 103, 99, c=102.5),  _b(2, 105, 102),
        _b(3, 106, 103), _b(4, 104, 100),   # bar[4] trades back into [100,102] gap
    ]
    fvgs = detect_fvgs(bars, tf="1h", atr_14=1.0, stale_after=100)
    assert any(f.type == "FVG_BULL" and f.mitigated for f in fvgs)

def test_fvg_stale_flag_triggers_past_threshold():
    # Construct an FVG at bar 2 then append 150 untouched bars far above the gap
    bars = [_b(0, 100, 98), _b(1, 103, 99, c=102.5), _b(2, 105, 102)]
    for i in range(3, 200):
        bars.append(_b(i, 110, 108))
    fvgs = detect_fvgs(bars, tf="1h", atr_14=1.0, stale_after=100)
    bull = next(f for f in fvgs if f.type == "FVG_BULL")
    assert bull.stale is True
    assert bull.mitigated is False
```

- [ ] **Step 5.2: Run test to fail**

Run: `uv run pytest tests/test_fvg.py -v`
Expected: FAIL — module missing.

- [ ] **Step 5.3: Implement src/fvg.py**

Create `src/fvg.py`:
```python
"""Fair Value Gap detection (3-bar imbalance).

Bullish FVG: bars[i+1].low > bars[i-1].high → gap [bars[i-1].high, bars[i+1].low].
Bearish FVG: bars[i+1].high < bars[i-1].low → gap [bars[i+1].high, bars[i-1].low].

Lifecycle:
  - `mitigated`: any subsequent bar traded INTO the gap range → considered
    filled (pool spent).
  - `stale`: age (in bars) > stale_after and still unmitigated → still live
    but flagged — very old unfilled gaps lose magnet power over time.

We keep stale but unmitigated FVGs in the output (user preference) —
the `stale` flag lets the analyst de-weight them.
"""
from __future__ import annotations
from dataclasses import dataclass

from src.types import OHLC, Timeframe


@dataclass(frozen=True)
class FVG:
    type: str              # "FVG_BULL" | "FVG_BEAR"
    tf: Timeframe
    lo: float
    hi: float
    formation_ts: int
    age_bars: int
    mitigated: bool
    stale: bool


def detect_fvgs(
    bars: list[OHLC], *, tf: Timeframe, atr_14: float, stale_after: int = 100,
) -> list[FVG]:
    """Scan 3-bar windows; report every FVG formed in the window, with
    mitigation and stale flags computed against all subsequent bars."""
    if len(bars) < 3:
        return []
    out: list[FVG] = []
    n = len(bars)
    for i in range(1, n - 1):
        prev, mid, nxt = bars[i - 1], bars[i], bars[i + 1]
        # Bullish FVG
        if nxt.low > prev.high:
            gap_lo, gap_hi = prev.high, nxt.low
            mit = _is_mitigated(bars, i + 1, gap_lo, gap_hi)
            age = n - 1 - (i + 1)
            out.append(FVG(
                type="FVG_BULL", tf=tf, lo=gap_lo, hi=gap_hi,
                formation_ts=mid.ts, age_bars=age,
                mitigated=mit, stale=(age > stale_after and not mit),
            ))
        # Bearish FVG
        if nxt.high < prev.low:
            gap_lo, gap_hi = nxt.high, prev.low
            mit = _is_mitigated(bars, i + 1, gap_lo, gap_hi)
            age = n - 1 - (i + 1)
            out.append(FVG(
                type="FVG_BEAR", tf=tf, lo=gap_lo, hi=gap_hi,
                formation_ts=mid.ts, age_bars=age,
                mitigated=mit, stale=(age > stale_after and not mit),
            ))
    return out


def _is_mitigated(bars: list[OHLC], start_idx: int, lo: float, hi: float) -> bool:
    for b in bars[start_idx + 1:]:
        if b.low <= hi and b.high >= lo:
            return True
    return False
```

- [ ] **Step 5.4: Run tests**

Run: `uv run pytest tests/test_fvg.py -v`
Expected: PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/fvg.py tests/test_fvg.py
git commit -m "feat(fvg): add 3-bar Fair Value Gap detection with mitigation and stale flags"
```

---

## Task 6: Order Blocks (ICT, 1.5×ATR displacement filter)

**Files:**
- Create: `src/order_blocks.py`
- Create: `tests/test_order_blocks.py`

- [ ] **Step 6.1: Failing test — bullish OB is last bearish candle before ≥1.5×ATR displacement breaking prior swing high**

Create `tests/test_order_blocks.py`:
```python
from src.order_blocks import detect_order_blocks
from src.types import OHLC

def _b(ts, o, h, l, c, v=1.0):
    return OHLC(ts=ts, open=o, high=h, low=l, close=c, volume=v)

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
```

- [ ] **Step 6.2: Run test to fail**

Run: `uv run pytest tests/test_order_blocks.py -v`
Expected: FAIL — module missing.

- [ ] **Step 6.3: Implement src/order_blocks.py**

Create `src/order_blocks.py`:
```python
"""ICT Order Blocks with 1.5×ATR displacement filter.

Bullish OB = the last down candle (close < open) that precedes a
displacement UP candle whose range exceeds 1.5×ATR and whose close breaks
above the most recent swing high within the lookback window.

Bearish OB = mirror.

Lifecycle:
  - `mitigated`: subsequent bar trades into the OB's price range → spent.
  - `stale`: age > stale_after AND unmitigated → flagged but retained.

OB range = the precursor candle's [low, high].
"""
from __future__ import annotations
from dataclasses import dataclass

from src.types import OHLC, Timeframe

DISPLACEMENT_ATR_MULT = 1.5
PRIOR_SWING_LOOKBACK = 20


@dataclass(frozen=True)
class OrderBlock:
    type: str            # "OB_BULL" | "OB_BEAR"
    tf: Timeframe
    lo: float
    hi: float
    formation_ts: int
    age_bars: int
    mitigated: bool
    stale: bool


def detect_order_blocks(
    bars: list[OHLC], *, tf: Timeframe, atr_14: float, stale_after: int = 100,
) -> list[OrderBlock]:
    if len(bars) < 3 or atr_14 <= 0:
        return []
    threshold = DISPLACEMENT_ATR_MULT * atr_14
    out: list[OrderBlock] = []
    n = len(bars)

    for i in range(1, n):
        disp = bars[i]
        disp_range = disp.high - disp.low
        if disp_range < threshold:
            continue
        # Up displacement?
        if disp.close > disp.open:
            # Need to break the most recent swing high in lookback [i-PRIOR_SWING_LOOKBACK, i-1]
            look_lo = max(0, i - PRIOR_SWING_LOOKBACK)
            prior_high = max(b.high for b in bars[look_lo:i])
            if disp.close <= prior_high:
                continue
            # Find last down candle before i
            ob_idx = _last_down_before(bars, i)
            if ob_idx is None:
                continue
            ob = bars[ob_idx]
            mit = _mitigated(bars, ob_idx, ob.low, ob.high)
            age = n - 1 - ob_idx
            out.append(OrderBlock(
                type="OB_BULL", tf=tf, lo=ob.low, hi=ob.high,
                formation_ts=ob.ts, age_bars=age,
                mitigated=mit, stale=(age > stale_after and not mit),
            ))
        # Down displacement?
        elif disp.close < disp.open:
            look_lo = max(0, i - PRIOR_SWING_LOOKBACK)
            prior_low = min(b.low for b in bars[look_lo:i])
            if disp.close >= prior_low:
                continue
            ob_idx = _last_up_before(bars, i)
            if ob_idx is None:
                continue
            ob = bars[ob_idx]
            mit = _mitigated(bars, ob_idx, ob.low, ob.high)
            age = n - 1 - ob_idx
            out.append(OrderBlock(
                type="OB_BEAR", tf=tf, lo=ob.low, hi=ob.high,
                formation_ts=ob.ts, age_bars=age,
                mitigated=mit, stale=(age > stale_after and not mit),
            ))
    # De-duplicate: keep most recent OB per (type, formation_ts)
    seen = {}
    for o in out:
        seen[(o.type, o.formation_ts)] = o
    return list(seen.values())


def _last_down_before(bars: list[OHLC], end_idx: int) -> int | None:
    for k in range(end_idx - 1, -1, -1):
        if bars[k].close < bars[k].open:
            return k
    return None


def _last_up_before(bars: list[OHLC], end_idx: int) -> int | None:
    for k in range(end_idx - 1, -1, -1):
        if bars[k].close > bars[k].open:
            return k
    return None


def _mitigated(bars: list[OHLC], from_idx: int, lo: float, hi: float) -> bool:
    for b in bars[from_idx + 2:]:  # skip the displacement bar itself
        if b.low <= hi and b.high >= lo:
            return True
    return False
```

- [ ] **Step 6.4: Run tests**

Run: `uv run pytest tests/test_order_blocks.py -v`
Expected: PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/order_blocks.py tests/test_order_blocks.py
git commit -m "feat(order_blocks): add ICT order block detection with 1.5×ATR displacement filter"
```

---

## Task 7: Market Structure (BOS / CHoCH)

**Files:**
- Create: `src/market_structure.py`
- Create: `tests/test_market_structure.py`

- [ ] **Step 7.1: Failing test — structure bias + BOS + CHoCH from pivot sequence**

Create `tests/test_market_structure.py`:
```python
from src.market_structure import analyze_structure, StructureState
from src.swings import detect_pivots
from src.types import OHLC

def _b(ts, h, l, c=None):
    c = c if c is not None else (h + l) / 2
    return OHLC(ts=ts, open=c, high=h, low=l, close=c, volume=1.0)

def test_uptrend_classified_as_bullish():
    # HH then HL then HH: classic uptrend
    # Pivots: highs rising, lows rising.
    pivots_highs = [(1, 100.0), (3, 105.0), (5, 110.0)]
    pivots_lows  = [(2, 98.0),  (4, 102.0)]
    state = analyze_structure(pivots_highs, pivots_lows, current_price=109.0)
    assert state.bias == "bullish"
    assert state.last_bos is not None
    assert state.invalidation_level is not None  # the most recent HL

def test_choch_on_first_break_against_trend():
    # Uptrend then price breaks below the most recent higher low → CHoCH bearish
    pivots_highs = [(1, 100.0), (3, 105.0)]
    pivots_lows  = [(2, 98.0),  (4, 102.0)]
    state = analyze_structure(pivots_highs, pivots_lows, current_price=101.0)
    # price 101 < most recent HL 102 → CHoCH bearish
    assert state.last_choch is not None
    assert state.last_choch["direction"] == "bearish"
```

- [ ] **Step 7.2: Run test to fail**

Run: `uv run pytest tests/test_market_structure.py -v`
Expected: FAIL — module missing.

- [ ] **Step 7.3: Implement src/market_structure.py**

Create `src/market_structure.py`:
```python
"""Market structure analysis — BOS (Break of Structure) + CHoCH (Change of
Character) derived from the same swing pivots swings.py already produces.

Bias definition:
  bullish: sequence of higher-highs (HH) AND higher-lows (HL)
  bearish: sequence of lower-highs (LH) AND lower-lows (LL)
  range:   neither clean pattern

BOS: continuation. Price takes out the most recent pivot IN the trend
direction (new HH in bullish / new LL in bearish).

CHoCH: reversal. First break AGAINST the prevailing trend — bullish trend
breaks the most recent HL, or bearish trend breaks the most recent LH.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class StructureState:
    bias: str                          # "bullish" | "bearish" | "range"
    last_bos: dict | None              # {"direction", "level", "ts"} or None
    last_choch: dict | None            # idem
    invalidation_level: float | None   # most-recent opposite pivot


def analyze_structure(
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    current_price: float,
) -> StructureState:
    """`highs` / `lows` are (ts_or_idx, price) tuples, same shape as
    detect_pivots. Works on either index or timestamp keys — only ordering
    matters."""
    if len(highs) < 2 or len(lows) < 2:
        return StructureState(bias="range", last_bos=None, last_choch=None,
                              invalidation_level=None)

    hh_seq = all(highs[i][1] > highs[i - 1][1] for i in range(1, len(highs)))
    ll_seq = all(lows[i][1]  < lows[i - 1][1]  for i in range(1, len(lows)))
    hl_seq = all(lows[i][1]  > lows[i - 1][1]  for i in range(1, len(lows)))
    lh_seq = all(highs[i][1] < highs[i - 1][1] for i in range(1, len(highs)))

    if hh_seq and hl_seq:
        bias = "bullish"
    elif ll_seq and lh_seq:
        bias = "bearish"
    else:
        bias = "range"

    last_bos: dict | None = None
    last_choch: dict | None = None
    invalidation: float | None = None

    if bias == "bullish":
        most_recent_hh = highs[-1]
        most_recent_hl = lows[-1]
        invalidation = most_recent_hl[1]
        if current_price > most_recent_hh[1]:
            last_bos = {"direction": "bullish", "level": most_recent_hh[1], "ts": most_recent_hh[0]}
        if current_price < most_recent_hl[1]:
            last_choch = {"direction": "bearish", "level": most_recent_hl[1], "ts": most_recent_hl[0]}
    elif bias == "bearish":
        most_recent_ll = lows[-1]
        most_recent_lh = highs[-1]
        invalidation = most_recent_lh[1]
        if current_price < most_recent_ll[1]:
            last_bos = {"direction": "bearish", "level": most_recent_ll[1], "ts": most_recent_ll[0]}
        if current_price > most_recent_lh[1]:
            last_choch = {"direction": "bullish", "level": most_recent_lh[1], "ts": most_recent_lh[0]}
    return StructureState(
        bias=bias, last_bos=last_bos, last_choch=last_choch,
        invalidation_level=invalidation,
    )
```

- [ ] **Step 7.4: Run tests**

Run: `uv run pytest tests/test_market_structure.py -v`
Expected: PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/market_structure.py tests/test_market_structure.py
git commit -m "feat(market_structure): add BOS/CHoCH structure bias analyzer"
```

---

## Task 8: Source adapters — convert module outputs to unified Level

**Files:**
- Modify: `src/levels.py`
- Create: `tests/test_level_adapters.py`

- [ ] **Step 8.1: Failing test — adapters produce Level from each source module**

Create `tests/test_level_adapters.py`:
```python
from src.levels import (
    fibs_to_levels, pools_to_levels, profile_to_levels,
    avwap_to_levels, fvgs_to_levels, obs_to_levels, structure_to_levels,
)
from src.types import FibLevel, SwingPair

def test_fib_adapter_maps_ratios_to_source_codes():
    pair = SwingPair(tf="1d", high_price=110, high_ts=0, low_price=100, low_ts=0, direction="down")
    fl = FibLevel(price=106.18, tf="1d", ratio=0.618, kind="retracement", pair=pair)
    levels = fibs_to_levels([fl])
    assert len(levels) == 1
    assert levels[0].source == "FIB_618"
    assert levels[0].tf == "1d"
```
Rest of adapter tests below follow a consistent pattern — one assertion per source confirming the correct LevelSource string is emitted.

- [ ] **Step 8.2: Run test to fail**

Run: `uv run pytest tests/test_level_adapters.py -v`
Expected: FAIL — adapters missing.

- [ ] **Step 8.3: Append adapters to src/levels.py**

Append to `src/levels.py`:
```python
# ---- Source → Level adapters ----
from src.types import FibLevel, Timeframe


_RATIO_TO_SRC = {
    0.236: "FIB_236", 0.382: "FIB_382", 0.5: "FIB_500",
    0.618: "FIB_618", 0.786: "FIB_786",
    1.272: "FIB_1272", 1.618: "FIB_1618",
}


def fibs_to_levels(fibs: list[FibLevel]) -> list[Level]:
    out: list[Level] = []
    for f in fibs:
        src = _RATIO_TO_SRC.get(f.ratio)
        if src is None:
            continue
        out.append(Level(
            price=f.price, min_price=f.price, max_price=f.price,
            source=src, tf=f.tf, strength=0.6 if f.ratio in (0.5, 0.618, 0.382) else 0.4,
            age_bars=0, meta={"ratio": f.ratio, "kind": f.kind},
        ))
    return out


def pools_to_levels(pools: dict[str, list[dict]], tf: Timeframe = "1d") -> list[Level]:
    out: list[Level] = []
    for side in ("buy_side", "sell_side"):
        for p in pools.get(side, []):
            if p.get("swept"):
                continue
            src = "LIQ_BSL" if p["type"] == "BSL" else "LIQ_SSL"
            rng = p["price_range"]
            out.append(Level(
                price=p["price"], min_price=rng[0], max_price=rng[1],
                source=src, tf=p["tfs"][0] if p["tfs"] else tf,
                strength=min(1.0, p["strength_score"] / 30.0),
                age_bars=p["age_hours"],
                meta={"touches": p["touches"], "tfs": p["tfs"]},
            ))
    return out


def profile_to_levels(profile, *, tf: Timeframe) -> list[Level]:
    out: list[Level] = []
    out.append(Level(
        price=profile.poc, min_price=profile.poc, max_price=profile.poc,
        source="POC", tf=tf, strength=0.8, age_bars=0,
    ))
    out.append(Level(
        price=profile.vah, min_price=profile.vah, max_price=profile.vah,
        source="VAH", tf=tf, strength=0.5, age_bars=0,
    ))
    out.append(Level(
        price=profile.val, min_price=profile.val, max_price=profile.val,
        source="VAL", tf=tf, strength=0.5, age_bars=0,
    ))
    for h in profile.hvn:
        out.append(Level(
            price=h, min_price=h, max_price=h,
            source="HVN", tf=tf, strength=0.4, age_bars=0,
        ))
    for l in profile.lvn:
        out.append(Level(
            price=l, min_price=l, max_price=l,
            source="LVN", tf=tf, strength=0.3, age_bars=0,
        ))
    return out


def naked_pocs_to_levels(naked_list, *, period: str, tf: Timeframe) -> list[Level]:
    src = {"D": "NAKED_POC_D", "W": "NAKED_POC_W", "M": "NAKED_POC_M"}[period]
    out: list[Level] = []
    for np_ in naked_list:
        if not np_.is_naked:
            continue
        out.append(Level(
            price=np_.price, min_price=np_.price, max_price=np_.price,
            source=src, tf=tf, strength=0.7,
            age_bars=0, meta={"distance_atr": np_.distance_atr},
        ))
    return out


def avwap_to_levels(avwaps, *, tf: Timeframe) -> list[Level]:
    """Emit one Level per anchor at its latest VWAP value + 1σ/2σ bands."""
    out: list[Level] = []
    for a in avwaps:
        if not a.vwap or all(x != x for x in a.vwap):
            continue
        last = next((v for v in reversed(a.vwap) if v == v), None)
        if last is None:
            continue
        out.append(Level(
            price=last, min_price=last, max_price=last,
            source=a.anchor_type, tf=tf, strength=0.7, age_bars=0,
        ))
        u1 = next((v for v in reversed(a.upper_1sd) if v == v), None)
        l1 = next((v for v in reversed(a.lower_1sd) if v == v), None)
        u2 = next((v for v in reversed(a.upper_2sd) if v == v), None)
        l2 = next((v for v in reversed(a.lower_2sd) if v == v), None)
        if u1 is not None: out.append(Level(price=u1, min_price=u1, max_price=u1, source="AVWAP_BAND_1SD_UP",   tf=tf, strength=0.4, age_bars=0))
        if l1 is not None: out.append(Level(price=l1, min_price=l1, max_price=l1, source="AVWAP_BAND_1SD_DOWN", tf=tf, strength=0.4, age_bars=0))
        if u2 is not None: out.append(Level(price=u2, min_price=u2, max_price=u2, source="AVWAP_BAND_2SD_UP",   tf=tf, strength=0.5, age_bars=0))
        if l2 is not None: out.append(Level(price=l2, min_price=l2, max_price=l2, source="AVWAP_BAND_2SD_DOWN", tf=tf, strength=0.5, age_bars=0))
    return out


def fvgs_to_levels(fvgs) -> list[Level]:
    out: list[Level] = []
    for f in fvgs:
        if f.mitigated:
            continue
        mid = (f.lo + f.hi) / 2
        strength = 0.6 if not f.stale else 0.3
        out.append(Level(
            price=mid, min_price=f.lo, max_price=f.hi,
            source=f.type, tf=f.tf, strength=strength,
            age_bars=f.age_bars, meta={"stale": f.stale},
        ))
    return out


def obs_to_levels(obs) -> list[Level]:
    out: list[Level] = []
    for o in obs:
        if o.mitigated:
            continue
        mid = (o.lo + o.hi) / 2
        strength = 0.7 if not o.stale else 0.35
        out.append(Level(
            price=mid, min_price=o.lo, max_price=o.hi,
            source=o.type, tf=o.tf, strength=strength,
            age_bars=o.age_bars, meta={"stale": o.stale},
        ))
    return out


def structure_to_levels(state, *, tf: Timeframe) -> list[Level]:
    out: list[Level] = []
    if state.last_bos:
        lvl = state.last_bos["level"]
        out.append(Level(
            price=lvl, min_price=lvl, max_price=lvl,
            source="MS_BOS_LEVEL", tf=tf, strength=0.8, age_bars=0,
            meta={"direction": state.last_bos["direction"]},
        ))
    if state.last_choch:
        lvl = state.last_choch["level"]
        out.append(Level(
            price=lvl, min_price=lvl, max_price=lvl,
            source="MS_CHOCH_LEVEL", tf=tf, strength=0.9, age_bars=0,
            meta={"direction": state.last_choch["direction"]},
        ))
    if state.invalidation_level is not None:
        out.append(Level(
            price=state.invalidation_level,
            min_price=state.invalidation_level, max_price=state.invalidation_level,
            source="MS_INVALIDATION", tf=tf, strength=0.6, age_bars=0,
        ))
    return out
```

- [ ] **Step 8.4: Run tests**

Run: `uv run pytest tests/test_level_adapters.py tests/test_levels.py -v`
Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/levels.py tests/test_level_adapters.py
git commit -m "feat(levels): add per-source Level adapters for fibs/pools/VP/AVWAP/FVG/OB/MS"
```

---

## Task 9: Wire all layers into emit_payload

**Files:**
- Modify: `scripts/emit_payload.py`
- Modify: `src/fetch.py` (extend to return bars for aggregation)
- Modify: `src/main.py` (unify confluence)

- [ ] **Step 9.1: Extend emit_payload to build unified zones**

Replace the body of `scripts/emit_payload.py` with:
```python
"""Emit the full analyst payload: unified confluence zones (fib + liq + VP +
AVWAP + FVG + OB + MS), plus all raw signal sections (derivatives, liquidity,
taker delta, structure bias).
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

from src import derivatives as derivatives_mod
from src import liquidity as liquidity_mod
from src.config import CONFIG
from src.fetch import fetch_all, taker_delta_per_tf
from src.venue_aggregator import fetch_all_venues, aggregate_bars
from src.fibs import compute_all
from src.main import (
    ATR_CLUSTER_MULTIPLIER, MAX_EXTENSION_DISTANCE_PCT,
    MIN_PAIRS_PER_TF, _latest,
)
from src.swings import atr, detect_swings, detect_pivots
from src.avwap import compute_avwap, resolve_anchors
from src.volume_profile import compute_profile, compute_naked_pocs
from src.fvg import detect_fvgs
from src.order_blocks import detect_order_blocks
from src.market_structure import analyze_structure
from src.levels import (
    cluster_levels, split_by_price,
    fibs_to_levels, pools_to_levels, profile_to_levels, naked_pocs_to_levels,
    avwap_to_levels, fvgs_to_levels, obs_to_levels, structure_to_levels,
)


async def _aggregated_per_tf(symbol: str, binance_ohlc: dict) -> dict:
    """For each TF: fetch Bybit + Coinbase bars and aggregate with Binance."""
    agg: dict = {}
    for tf, binance_bars in binance_ohlc.items():
        others = await fetch_all_venues(symbol, tf, limit=len(binance_bars))
        agg[tf] = aggregate_bars({
            "binance": binance_bars,
            "bybit":    others["bybit"],
            "coinbase": others["coinbase"],
        })
    return agg


async def build() -> dict:
    ohlc, deriv = await asyncio.gather(fetch_all(), derivatives_mod.fetch_all())

    # --- Swings + fibs (Binance-only as before; swings need historic stability)
    all_pairs = []
    contributing, skipped = [], []
    for tf, bars in ohlc.items():
        pairs = detect_swings(bars, tf=tf, max_pairs=3)
        if len(pairs) < MIN_PAIRS_PER_TF:
            skipped.append(tf)
            continue
        all_pairs.extend(pairs)
        contributing.append(tf)

    daily_bars = ohlc["1d"]
    current_price = daily_bars[-1].close
    daily_atr = _latest(atr(daily_bars, 14))
    radius = daily_atr * ATR_CLUSTER_MULTIPLIER

    fibs = compute_all(all_pairs)
    fibs = [
        l for l in fibs
        if l.kind == "retracement"
        or abs(l.price - current_price) / current_price <= MAX_EXTENSION_DISTANCE_PCT
    ]

    # --- Liquidity pools (unchanged)
    liquidity_pools = liquidity_mod.compute_pools(
        swing_pairs=all_pairs, ohlc=ohlc,
        current_price=current_price, daily_atr=daily_atr,
    )

    # --- Aggregated OHLCV for VP / AVWAP only
    agg_ohlc = await _aggregated_per_tf(CONFIG.symbol, ohlc)

    # --- Volume Profile per TF (on aggregated bars)
    vp_by_tf: dict = {}
    naked_pocs: dict = {"D": [], "W": [], "M": []}
    for tf, bars in agg_ohlc.items():
        tf_atr = _latest(atr(bars, 14))
        vp_by_tf[tf] = compute_profile(bars, atr_14=tf_atr)
    # Naked POCs (daily / weekly / monthly) — derived from 1h aggregated bars
    # so we have enough granularity for mid-period visitation checks
    if "1h" in agg_ohlc:
        h1 = agg_ohlc["1h"]
        atr_1d = _latest(atr(daily_bars, 14))
        naked_pocs["D"] = compute_naked_pocs(h1, period_ms=86_400_000,      lookback=10, atr_14=atr_1d)
        naked_pocs["W"] = compute_naked_pocs(h1, period_ms=7*86_400_000,    lookback=6,  atr_14=atr_1d)
        naked_pocs["M"] = compute_naked_pocs(h1, period_ms=30*86_400_000,   lookback=3,  atr_14=atr_1d)

    # --- AVWAP per TF
    avwap_by_tf: dict = {}
    for tf, bars in agg_ohlc.items():
        anchors = resolve_anchors(bars, [p for p in all_pairs if p.tf == tf])
        avwap_by_tf[tf] = [
            compute_avwap(bars, anchor_idx=idx, anchor_type=typ, anchor_ts=ts)
            for typ, idx, ts in anchors
        ]

    # --- FVG / OB / MS per TF (on Binance-primary bars — cross-venue aggregation
    # doesn't help 3-bar pattern detection; use the stable reference source)
    fvg_by_tf: dict = {}
    ob_by_tf: dict = {}
    ms_by_tf: dict = {}
    for tf, bars in ohlc.items():
        tf_atr = _latest(atr(bars, 14))
        fvg_by_tf[tf] = detect_fvgs(bars, tf=tf, atr_14=tf_atr)
        ob_by_tf[tf]  = detect_order_blocks(bars, tf=tf, atr_14=tf_atr)
        highs, lows = detect_pivots(bars, n=None)
        ms_by_tf[tf] = analyze_structure(highs, lows, current_price=current_price)

    # --- Unified level list
    levels = fibs_to_levels(fibs)
    levels += pools_to_levels(liquidity_pools, tf="1d")
    for tf, vp in vp_by_tf.items():
        levels += profile_to_levels(vp, tf=tf)
    for period, pocs in naked_pocs.items():
        levels += naked_pocs_to_levels(pocs, period=period, tf="1d")
    for tf, avwaps in avwap_by_tf.items():
        levels += avwap_to_levels(avwaps, tf=tf)
    for fvgs in fvg_by_tf.values():
        levels += fvgs_to_levels(fvgs)
    for obs in ob_by_tf.values():
        levels += obs_to_levels(obs)
    for tf, ms in ms_by_tf.items():
        levels += structure_to_levels(ms, tf=tf)

    # Drop far-away levels (keep within 20% of current price)
    levels = [l for l in levels if abs(l.price - current_price) / current_price <= 0.20]

    zones = cluster_levels(levels, radius=radius)
    support, resistance = split_by_price(zones, current_price)

    # --- Payload
    prev_close = daily_bars[-2].close
    change_24h_pct = (current_price - prev_close) / prev_close * 100

    def z_to_dict(z):
        return {
            "min_price": round(z.min_price, 2),
            "max_price": round(z.max_price, 2),
            "mid": round(z.mid, 2),
            "score": z.score,
            "source_count": z.source_count,
            "classification": z.classification,
            "distance_pct": round((z.mid - current_price) / current_price * 100, 2),
            "sources": sorted({l.source for l in z.levels}),
            "contributing_levels": sorted(
                [{"source": l.source, "tf": l.tf, "price": round(l.price, 2), "meta": l.meta}
                 for l in z.levels],
                key=lambda d: d["price"],
            ),
        }

    if deriv.get("status") == "ok" and deriv.get("liquidation_clusters_72h"):
        deriv["liquidation_clusters_72h"] = derivatives_mod.enrich_clusters_with_price(
            deriv["liquidation_clusters_72h"], ohlc["4h"]
        )

    return {
        "asset": CONFIG.asset,
        "display_name": CONFIG.display_name,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "current_price": round(current_price, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "daily_atr": round(daily_atr, 2),
        "contributing_tfs": contributing,
        "skipped_tfs": skipped,
        "resistance": [z_to_dict(z) for z in resistance[:10]],
        "support":    [z_to_dict(z) for z in support[:10]],
        "derivatives": deriv,
        "spot_taker_delta_by_tf": taker_delta_per_tf(ohlc),
        "liquidity": liquidity_pools,
        "market_structure": {
            tf: {
                "bias": ms.bias,
                "last_bos": ms.last_bos,
                "last_choch": ms.last_choch,
                "invalidation_level": ms.invalidation_level,
            }
            for tf, ms in ms_by_tf.items()
        },
        "naked_pocs": {
            period: [
                {
                    "price": round(p.price, 2),
                    "period_start_ts": p.period_start_ts,
                    "period_end_ts": p.period_end_ts,
                    "distance_atr": round(p.distance_atr, 2) if p.distance_atr is not None else None,
                }
                for p in lst if p.is_naked
            ]
            for period, lst in naked_pocs.items()
        },
        "venue_sources": ["binance", "bybit", "coinbase"],
    }


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else f"/tmp/{CONFIG.asset}_swings_payload.json"
    payload = asyncio.run(build())
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"payload written: {out_path}")
    print(f"current: {payload['current_price']} "
          f"resistance: {len(payload['resistance'])} "
          f"support: {len(payload['support'])} "
          f"derivatives: {payload['derivatives']['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9.2: End-to-end payload smoke test**

Run:
```bash
ASSET=btc uv run python -m scripts.emit_payload /tmp/btc_pa_payload.json
cat /tmp/btc_pa_payload.json | python -m json.tool | head -80
```
Expected: payload JSON written; `resistance` and `support` arrays contain zones with `sources` list mentioning multiple of the new codes (FIB_618, POC, AVWAP_*, FVG_*, OB_*, MS_*), at least one `classification == "strong"` or `"confluence"`.

- [ ] **Step 9.3: Commit**

```bash
git add scripts/emit_payload.py
git commit -m "feat(emit_payload): unify fib+liq+VP+AVWAP+FVG+OB+MS into single confluence payload"
```

---

## Task 10: Update main.py to use unified confluence

**Files:**
- Modify: `src/main.py`

- [ ] **Step 10.1: Replace fib-only cluster path in main.py**

In `src/main.py`, replace the block from `levels = compute_all(all_pairs)` through `support, resistance = split_by_price(zones, current_price)` (roughly lines 48-69) with logic that delegates to emit_payload's build:

```python
from scripts.emit_payload import build as build_payload

async def run() -> int:
    try:
        payload = await build_payload()
    except Exception as e:
        await _notify_failure(f"{CONFIG.display_name} Swings: payload build failed — {e}")
        return 1

    current_price = payload["current_price"]
    change_24h_pct = payload["change_24h_pct"]
    daily_atr = payload["daily_atr"]
    contributing = payload["contributing_tfs"]
    skipped = payload["skipped_tfs"]
    # Re-hydrate minimal Zone objects for notion_writer compatibility
    # (notion_writer signature expects Zone objects; the new payload zones are
    # plain dicts — adapt here or update notion_writer in Step 10.2)
    ...
```

**NOTE:** This step will touch `src/notion_writer.py` and `src/telegram_notify.py` — they both currently accept `Zone` (the old fib-only dataclass). Decide one of:
(a) Keep `Zone` as a legacy shim that `build_payload` also emits alongside the new `MultiSourceZone`, so downstream callers don't break.
(b) Update both writers to consume the new zone dict shape (`min_price`, `max_price`, `score`, `sources`, `classification`, `distance_pct`).

Pick (b). The analyst agent writes the briefing — the Notion page carries the briefing body unchanged, so the writer just needs the header fields (price, ATR) and an optional JSON dump of the payload for auditability. Simplify accordingly.

- [ ] **Step 10.2: Update notion_writer + telegram_notify to consume dict zones**

Update `src/notion_writer.py::build_page_payload` signature to accept dict zones (from `payload["resistance"] / payload["support"]`) instead of `list[Zone]`. Change:
```python
# old
def build_page_payload(*, support: list[Zone], resistance: list[Zone], ...):
    ...
    # references to z.min_price / z.max_price / z.score ...
```
to:
```python
def build_page_payload(*, support: list[dict], resistance: list[dict], ...):
    ...
    # references to z["min_price"] / z["max_price"] / z["score"] / z["source_count"]
    # / z["classification"] ...
```
Mirror the same change in `src/telegram_notify.py::build_summary`.

- [ ] **Step 10.3: Run the full test suite**

Run: `uv run pytest -x`
Expected: all tests pass. Failures in `test_notion_writer.py` / `test_telegram_notify.py` point to remaining spots that still expect `Zone` dataclasses — update those tests with dict fixtures matching the new shape.

- [ ] **Step 10.4: Commit**

```bash
git add src/main.py src/notion_writer.py src/telegram_notify.py tests/
git commit -m "refactor(main): drive pipeline from unified payload builder; update writers for dict zones"
```

---

## Task 11: Rewrite analyst agent prompt

**Files:**
- Modify: `.claude/agents/crypto-swings-analyst.md`

- [ ] **Step 11.1: Replace the Input Schema and Analysis Framework sections**

The new schema description must document `source_count`, `classification`, `sources`, `market_structure`, `naked_pocs`, `venue_sources`. The Analysis Framework must be restructured as follows (FULL REPLACEMENT — do not leave any vestige of the fib-only framing):

Replace the current `## Input Schema` → `## Boundaries` with this block:

```markdown
## Input Schema

The payload at `data/payload.json` has this shape (highlights — see pipeline
for full field list):

```json
{
  "asset": "btc",
  "display_name": "BTC",
  "timestamp_utc": "...",
  "current_price": 74646.0,
  "change_24h_pct": -0.68,
  "daily_atr": 2369.0,
  "contributing_tfs": ["1M", "1w", "1d", "4h", "1h"],
  "skipped_tfs": [],
  "venue_sources": ["binance", "bybit", "coinbase"],
  "resistance": [
    {
      "min_price": 78962.0,
      "max_price": 79457.0,
      "mid": 79209.5,
      "score": 14.8,
      "source_count": 3,
      "classification": "strong",          // "strong" | "confluence" | "structural_pivot" | "level"
      "distance_pct": 6.11,
      "sources": ["FIB_618", "POC", "AVWAP_WEEK"],
      "contributing_levels": [
        {"source": "FIB_618", "tf": "1d", "price": 79100, "meta": {...}},
        ...
      ]
    }
  ],
  "support": [ /* same shape */ ],
  "derivatives": { /* unchanged from prior versions */ },
  "spot_taker_delta_by_tf": { /* unchanged */ },
  "liquidity": { "buy_side": [...], "sell_side": [...] },
  "market_structure": {
    "1d": {
      "bias": "bullish" | "bearish" | "range",
      "last_bos":   { "direction": "bullish", "level": 78000, "ts": ... } | null,
      "last_choch": { "direction": "bearish", "level": 72000, "ts": ... } | null,
      "invalidation_level": 72000 | null
    },
    ...
  },
  "naked_pocs": {
    "D": [ {"price": 76000, "distance_atr": 0.8, ...}, ... ],
    "W": [...],
    "M": [...]
  }
}
```

## Analysis Framework

Briefing sections (exact order): **Preț curent**, **Context structural**,
**Pe scurt**, **Rezistență**, **Suport**, **Zone de liquidity** (optional),
**De urmărit**.

### Context structural

One short line per TF where `market_structure[tf].bias` exists. Format:
`- **{tf}** — {bias} (ultima {BOS|CHoCH}: {direction} la ${level}). Invalidare: ${invalidation}.`

- Emit lines for 1M / 1w / 1d only when those TFs produced bias. Skip `range`
  TFs unless they contradict a higher TF (note the contradiction in Pe scurt).
- For `bias: range`, write: `- **{tf}** — range (fără BOS/CHoCH recent).`

### Pe scurt

Two to four hedged Romanian sentences. Blend:
- The 24h move vs `daily_atr`.
- Position relative to structural bias and nearest strong-confluence zone.
- ONE derivatives signal, when sharp and non-null (same rules as before:
  funding > +15% annualized, basis > ±0.10, OI Δ > ±5%, dominant-side 24h liq).
- Optional: mention a naked POC above/below if it's within 2 × ATR as a
  magnet phrase.

Hedged vocabulary: *poate, pare, ar putea, probabil, sugerează*.

### Confluence classification (this replaces the old `puternică/medie/slabă` mechanic)

The pipeline assigns `classification` per zone. Use it verbatim in bullets:

| Classification | Meaning | Romanian label |
|---|---|---|
| `structural_pivot` | MS BOS/CHoCH level + another source. Directional — when broken, the thesis flips. | `pivot structural` |
| `strong`           | 3+ distinct source families | `confluență puternică` |
| `confluence`       | 2 distinct source families | `confluență medie` |
| `level`            | 1 family only | omit the zone from S/R lists — not worth calling out |

Do NOT fabricate a different label. Do NOT downgrade/upgrade based on
derivatives (Pass-2 logic is removed — let the classification stand; mention
derivatives separately in Pe scurt).

### Zone bullets (Rezistență + Suport)

Format:
```
- **$MIN–$MAX** ({distance}%) — {label} · {sources, comma-separated, max 4}
```

Examples:
```
- **$78,962–$79,457** (+6.11%) — confluență puternică · FIB_618 (1d) · POC (1d) · AVWAP_WEEK
- **$72,000–$72,400** (−3.52%) — pivot structural · MS_CHOCH_LEVEL (4h) · LIQ_SSL · FVG_BULL (4h)
```

Rules:
- **Nearest first.** Up to 4 bullets per side.
- Drop zones with `classification == "level"` unless fewer than 2 zones
  remain per side — then include the top single-source zone to avoid an
  empty section.
- When a zone's `sources` list contains MS_BOS/CHoCH and LIQ together, tag
  the direction: `· MS_BOS_LEVEL bullish (4h)` → readers understand break
  direction without reading the raw payload.
- When a FVG or OB is in the sources, readers should know it's an ICT zone;
  state "FVG" or "OB" plainly — the acronyms stay English.

### Confluence combos the analyst must recognize

Treat these combos as high-conviction setups when they appear in a zone's
`sources` list — weave into Pe scurt or De urmărit as the structural thesis:

- **FIB + LIQ** → classic stop-hunt at retrace.
- **FIB + FVG** → imbalance fills inside retrace.
- **LIQ + FVG + OB** → institutional re-entry zone.
- **POC + AVWAP** → mean-reversion magnet (phrase: *"zonă de echilibru"*).
- **NAKED_POC + FIB** → unfinished auction at golden pocket (strong magnet).
- **MS_BOS + LIQ** → break triggers sweep (directional).
- **MS_CHOCH + FVG + OB** → reversal zone with entry trigger (highest conviction).

### Zone de liquidity (unchanged format; include naked POCs)

Extend the existing rules so unmitigated **naked POCs** also qualify when
they are NOT inside an already-listed zone. Format:
```
- **${price}** ({distance}%) — naked POC {D|W|M} · {age_days}d
```

Cap the section at 3 bullets total (pools + naked POCs combined).

### De urmărit

Three lines. Use real prices from top zones:
- **Sus:** o închidere 4h deasupra $X ar putea deschide $Y; dacă zona conține MS_BOS bullish, menționează.
- **Jos:** o închidere 4h sub $X ar putea aduce $Y în joc.
- **Invalidare:** prefera `market_structure.{1d|4h}.invalidation_level` când există; altfel cea mai puternică zonă de suport.

## Boundaries (unchanged from previous version)
```

Keep the rest of the file (Language, Workflow, Response Format, Boundaries body) intact — those rules still apply. Only the **Input Schema** and **Analysis Framework** sections change.

- [ ] **Step 11.2: Commit**

```bash
git add .claude/agents/crypto-swings-analyst.md
git commit -m "feat(analyst): rewrite crypto-swings-analyst prompt for unified multi-source confluence"
```

---

## Task 12: End-to-end validation

- [ ] **Step 12.1: Run full test suite**

Run: `uv run pytest -x`
Expected: all tests pass.

- [ ] **Step 12.2: Generate BTC + ETH payloads**

Run:
```bash
ASSET=btc uv run python -m scripts.emit_payload /tmp/btc_payload.json
ASSET=eth uv run python -m scripts.emit_payload /tmp/eth_payload.json
```
Expected: both payloads have `resistance` + `support` zones where at least the top zone on each side has `classification in ("strong", "confluence", "structural_pivot")` and `source_count >= 2`. Zones with only FIB sources are rare — most confluence comes from multi-family agreement.

- [ ] **Step 12.3: Dispatch analyst agent locally on the payload**

```bash
cp /tmp/btc_payload.json data/payload.json
# Invoke crypto-swings-analyst agent via the same mechanism the skill uses
# (whatever your local test harness is — typically Agent tool with the
# agent file above)
# Then inspect data/briefing.md:
cat data/briefing.md
```
Expected: briefing in Romanian; contains Context structural, classification labels, source tags on every bullet, and coherent De urmărit. If the analyst fails schema validation, capture the error and adjust the input schema docs accordingly.

- [ ] **Step 12.4: Diff briefing against a sample from previous version**

Compare the new briefing's info density and correctness against a recent real briefing from Notion. Look for:
- Sources cited are actually in the zone's `sources` list (no hallucination).
- Structural context line per TF is present.
- Naked POC mentions align with the `naked_pocs` section.
- No vestigial "confluență slabă" mechanic.

Flag any regressions and fix the prompt before merging.

- [ ] **Step 12.5: Commit + open PR**

```bash
git push -u origin feature/price-action-layer
gh pr create --title "Price-action & volume intelligence layer" --body "$(cat <<'EOF'
## Summary
- Add Binance+Bybit+Coinbase OHLCV aggregator for VP/AVWAP
- Add anchored VWAP, volume profile (composite + naked POC), FVG, order blocks, and market structure (BOS/CHoCH) modules
- Unify fib + liquidity pools + new sources into a single multi-source confluence scorer
- Rewrite crypto-swings-analyst prompt: classification-based zone labels (strong/confluence/structural_pivot), combo recognition, structural context section

## Test plan
- [x] Unit tests green for all new modules
- [x] BTC + ETH payload smoke tests show multi-source zones
- [ ] Analyst briefing reviewed for BTC + ETH against recent Notion briefings
EOF
)"
```

---

## Self-review notes (author-facing, pre-merge)

Before landing:
1. Verify that `MAX_EXTENSION_DISTANCE_PCT` filter still excludes fib extensions — we now clip levels to ±20% which is different from fib-extension-only filtering. If some extension levels remain valid beyond 15% they'll now be included; decide whether that's intended.
2. Confirm Bybit+Coinbase fetch adds <3 seconds to total pipeline runtime (we now make 2×5 = 10 extra HTTP requests). If it pushes over Routines' per-run budget, parallelize across TFs more aggressively or fall back to Binance-only for AVWAP/VP with an explicit payload flag.
3. Naked POC period definitions use fixed `period_ms` (24h, 7d, 30d). Month boundaries drift; if this matters we switch to calendar-aware slicing in a follow-up.
4. Event anchors list is static (halving, ETF). Add a quarterly review to confirm they're still relevant or add new ones (e.g. major regulatory events).
