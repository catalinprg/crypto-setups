from datetime import datetime, timezone
from src.config import CONFIG

NOTION_PARENT_ID = CONFIG.notion_parent_id


def _format_zone(z: dict, current_price: float) -> str:
    min_p, max_p, score = z["min_price"], z["max_price"], z["score"]
    dist_pct = z["distance_pct"]
    sources = z["sources"]
    price_str = (
        f"${min_p:,.0f}" if min_p == max_p
        else f"${min_p:,.0f}–${max_p:,.0f}"
    )
    return (
        f"- **{price_str}** (score {score}, {dist_pct:+.2f}%) — "
        f"{z['classification']} · {', '.join(sources[:4])}"
    )


def build_page_payload(
    *,
    current_price: float,
    change_24h_pct: float,
    atr_daily: float,
    support: list[dict],
    resistance: list[dict],
    contributing_tfs: list[str],
    skipped_tfs: list[str],
    parent_page_id: str,
    top_n: int = 5,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"{CONFIG.display_name} Swings — {now}"

    lines: list[str] = []
    lines.append(
        f"**Current:** ${current_price:,.0f} "
        f"({change_24h_pct:+.2f}% 24h) · ATR(1d): ${atr_daily:,.0f}"
    )
    lines.append("")

    lines.append("## Resistance")
    if resistance:
        for z in resistance[:top_n]:
            lines.append(_format_zone(z, current_price))
    else:
        lines.append("_(no resistance zones detected above current price)_")
    lines.append("")

    lines.append("## Support")
    if support:
        for z in support[:top_n]:
            lines.append(_format_zone(z, current_price))
    else:
        lines.append("_(no support zones detected below current price)_")
    lines.append("")

    lines.append("---")
    lines.append(
        f"**TFs contributing:** {', '.join(contributing_tfs) or 'none'}"
    )
    if skipped_tfs:
        lines.append(f"**TFs skipped** (insufficient swings): {', '.join(skipped_tfs)}")

    return {
        "title": title,
        "body": "\n".join(lines),
        "parent_page_id": parent_page_id,
    }
