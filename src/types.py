from dataclasses import dataclass, field
from typing import Literal

Timeframe = Literal["1M", "1w", "1d", "4h", "1h"]
SwingDirection = Literal["up", "down"]
LevelKind = Literal["retracement", "extension"]

@dataclass(frozen=True)
class OHLC:
    ts: int         # open time, ms since epoch
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Binance spot klines row[9]. Populated from spot data only — None on
    # fixtures that pre-date this field. Used for derived taker delta.
    taker_buy_volume: float | None = None

@dataclass(frozen=True)
class SwingPair:
    tf: Timeframe
    high_price: float
    high_ts: int
    low_price: float
    low_ts: int
    direction: SwingDirection  # "up" = high more recent; "down" = low more recent

@dataclass(frozen=True)
class FibLevel:
    price: float
    tf: Timeframe
    ratio: float
    kind: LevelKind
    pair: SwingPair

@dataclass(frozen=True)
class Zone:
    min_price: float
    max_price: float
    score: int
    levels: tuple[FibLevel, ...]

    @property
    def mid(self) -> float:
        return (self.min_price + self.max_price) / 2

TF_WEIGHTS: dict[Timeframe, int] = {"1M": 5, "1w": 4, "1d": 3, "4h": 2, "1h": 1}
LEVEL_WEIGHTS: dict[float, int] = {
    0.236: 1, 0.382: 2, 0.5: 3, 0.618: 3, 0.786: 2, 1.272: 2, 1.618: 3,
}
RETRACEMENT_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
EXTENSION_RATIOS = (1.272, 1.618)

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
