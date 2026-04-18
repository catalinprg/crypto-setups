from src.levels import (
    fibs_to_levels, pools_to_levels, profile_to_levels,
    avwap_to_levels, fvgs_to_levels, obs_to_levels, structure_to_levels,
    naked_pocs_to_levels,
)
from src.types import FibLevel, SwingPair
from src.market_structure import StructureState
from src.fvg import FVG
from src.order_blocks import OrderBlock
from src.volume_profile import VolumeProfile, NakedPOC
from src.avwap import AnchoredVwap


def test_fib_adapter_maps_ratios_to_source_codes():
    pair = SwingPair(tf="1d", high_price=110, high_ts=0, low_price=100, low_ts=0, direction="down")
    fl = FibLevel(price=106.18, tf="1d", ratio=0.618, kind="retracement", pair=pair)
    levels = fibs_to_levels([fl])
    assert len(levels) == 1
    assert levels[0].source == "FIB_618"
    assert levels[0].tf == "1d"


def test_pools_adapter_skips_swept_and_maps_type():
    pools = {
        "buy_side": [
            {"type": "BSL", "price": 105.0, "price_range": [104.5, 105.5], "tfs": ["1d"],
             "age_hours": 10, "touches": 2, "strength_score": 15, "swept": False, "distance_pct": 1.0},
            {"type": "BSL", "price": 106.0, "price_range": [106, 106], "tfs": ["1d"],
             "age_hours": 10, "touches": 1, "strength_score": 5, "swept": True, "distance_pct": 2.0},
        ],
        "sell_side": [
            {"type": "SSL", "price": 95.0, "price_range": [94.8, 95.2], "tfs": ["1w"],
             "age_hours": 20, "touches": 3, "strength_score": 30, "swept": False, "distance_pct": -2.0},
        ],
    }
    levels = pools_to_levels(pools)
    # 3 pools, 1 swept → 2 levels
    assert len(levels) == 2
    assert {l.source for l in levels} == {"LIQ_BSL", "LIQ_SSL"}


def test_profile_adapter_emits_poc_vah_val():
    vp = VolumeProfile(poc=100.0, vah=105.0, val=95.0, hvn=[102.0], lvn=[97.0], bin_width=0.5)
    levels = profile_to_levels(vp, tf="1d")
    sources = {l.source for l in levels}
    assert "POC" in sources
    assert "VAH" in sources
    assert "VAL" in sources
    assert "HVN" in sources
    assert "LVN" in sources


def test_avwap_adapter_emits_main_and_bands():
    av = AnchoredVwap(
        anchor_type="AVWAP_WEEK", anchor_ts=0,
        vwap=[100.0, 101.0, 102.0],
        upper_1sd=[101.0, 102.0, 103.0], lower_1sd=[99.0, 100.0, 101.0],
        upper_2sd=[102.0, 103.0, 104.0], lower_2sd=[98.0, 99.0, 100.0],
    )
    levels = avwap_to_levels([av], tf="1d")
    sources = {l.source for l in levels}
    assert "AVWAP_WEEK" in sources
    assert "AVWAP_BAND_1SD_UP" in sources
    assert "AVWAP_BAND_1SD_DOWN" in sources
    assert "AVWAP_BAND_2SD_UP" in sources
    assert "AVWAP_BAND_2SD_DOWN" in sources


def test_fvg_adapter_drops_mitigated_and_scales_stale_strength():
    unm = FVG(type="FVG_BULL", tf="1h", lo=100, hi=102, formation_ts=0, age_bars=5,  mitigated=False, stale=False)
    stl = FVG(type="FVG_BEAR", tf="1h", lo=110, hi=112, formation_ts=0, age_bars=150, mitigated=False, stale=True)
    mit = FVG(type="FVG_BULL", tf="1h", lo=90, hi=92,   formation_ts=0, age_bars=5,  mitigated=True,  stale=False)
    levels = fvgs_to_levels([unm, stl, mit])
    assert len(levels) == 2   # mitigated dropped
    strengths = {l.source: l.strength for l in levels}
    assert strengths["FVG_BULL"] > strengths["FVG_BEAR"]  # non-stale stronger


def test_ob_adapter_drops_mitigated_and_scales_stale_strength():
    unm = OrderBlock(type="OB_BULL", tf="1h", lo=100, hi=101, formation_ts=0, age_bars=5,  mitigated=False, stale=False)
    stl = OrderBlock(type="OB_BEAR", tf="1h", lo=110, hi=111, formation_ts=0, age_bars=150, mitigated=False, stale=True)
    mit = OrderBlock(type="OB_BULL", tf="1h", lo=90,  hi=91,  formation_ts=0, age_bars=5,  mitigated=True,  stale=False)
    levels = obs_to_levels([unm, stl, mit])
    assert len(levels) == 2
    strengths = {l.source: l.strength for l in levels}
    assert strengths["OB_BULL"] > strengths["OB_BEAR"]


def test_structure_adapter_emits_bos_choch_invalidation():
    state = StructureState(
        bias="bullish",
        last_bos={"direction": "bullish", "level": 110.0, "ts": 0},
        last_choch={"direction": "bearish", "level": 102.0, "ts": 0},
        invalidation_level=102.0,
    )
    levels = structure_to_levels(state, tf="1d")
    sources = {l.source for l in levels}
    assert "MS_BOS_LEVEL" in sources
    assert "MS_CHOCH_LEVEL" in sources
    assert "MS_INVALIDATION" in sources
    # Direction carried through meta
    bos_level = next(l for l in levels if l.source == "MS_BOS_LEVEL")
    assert bos_level.meta.get("direction") == "bullish"


def test_naked_pocs_adapter_maps_period_to_source():
    from src.volume_profile import NakedPOC
    naked_d = [NakedPOC(price=100.0, period_start_ts=0, period_end_ts=1, is_naked=True, distance_atr=1.5)]
    not_naked = [NakedPOC(price=105.0, period_start_ts=0, period_end_ts=1, is_naked=False, distance_atr=2.0)]
    levels = naked_pocs_to_levels(naked_d, period="D", tf="1d")
    assert len(levels) == 1
    assert levels[0].source == "NAKED_POC_D"
    # Not-naked filtered out
    assert naked_pocs_to_levels(not_naked, period="D", tf="1d") == []
