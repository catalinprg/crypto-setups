"""Unified multi-source confluence clustering.

Groups Levels from heterogeneous sources (fib, liquidity, VP, AVWAP, FVG,
OB, market structure) into zones. Zone score rewards DISTINCT source count
far more than raw level count — two sources agreeing is a stronger signal
than five fib retracements from the same swing.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Literal

from src.types import Level, TF_WEIGHTS

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
FAMILY_BONUS = 3.0
HTF_WEEK_MULT = 1.25
HTF_MONTH_MULT = 1.5


@dataclass(frozen=True)
class MultiSourceZone:
    min_price: float
    max_price: float
    levels: tuple[Level, ...]
    source_count: int         # distinct families
    score: float
    classification: Literal["strong", "confluence", "structural_pivot", "level"]

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
    base = FAMILY_BONUS * len(families)
    base += sum(TF_WEIGHTS.get(l.tf, 1) * l.strength for l in group)
    tfs = {l.tf for l in group}
    if "1M" in tfs:
        base *= HTF_MONTH_MULT
    elif "1w" in tfs:
        base *= HTF_WEEK_MULT
    return base


def _classify(
    source_count: int, families: set[str]
) -> Literal["strong", "confluence", "structural_pivot", "level"]:
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
