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
