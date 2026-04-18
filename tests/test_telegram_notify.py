from src.telegram_notify import build_summary


def _zone(price, score, sources=("FIB_618",), classification="confluence"):
    return {
        "min_price": price,
        "max_price": price,
        "mid": float(price),
        "score": score,
        "source_count": len(sources),
        "classification": classification,
        "distance_pct": 0.0,
        "sources": list(sources),
        "contributing_levels": [],
    }


def test_summary_includes_current_price_and_top_zones():
    msg = build_summary(
        current_price=60000.0,
        top_support=[_zone(58000, 10), _zone(55000, 8)],
        top_resistance=[_zone(62000, 12), _zone(65000, 9)],
        notion_url="https://notion.so/page-abc",
    )
    assert "60" in msg
    assert "58" in msg and "62" in msg
    assert "notion.so/page-abc" in msg


def test_summary_handles_empty_zones():
    msg = build_summary(
        current_price=60000.0,
        top_support=[],
        top_resistance=[],
        notion_url="https://notion.so/page-abc",
    )
    assert "60" in msg
    assert "notion.so/page-abc" in msg
