"""Phase-1 macro fetch for crypto-setups.

Gathers shared-across-run context that the per-asset analyst can reference:
  1. Per-asset news (MARKETAUX BTC/ETH + CoinDesk + Cointelegraph + Google
     News fallback), filtered by `relevance_terms` in the asset config.
  2. Economic calendar (Forex Factory mirror, next 48h, high/medium impact).
     Crypto reads the USD-currency high-impact events only; the agent's
     Catalyst Gate filters down to what's actually relevant.

No earnings calendar — there's no equivalent for crypto. Crypto-native
scheduled events (Deribit Friday expiry, ETF flow reports) are NOT added
here yet — options proximity is already surfaced in the per-asset payload
via `options.max_pain_strike` and expiry distance.

Usage:
    python3 -m scripts.emit_macro [output_path]
    # default: data/macro_context.json

Env vars (optional):
    MARKETAUX_API_KEY   — enables primary news source
    FIRECRAWL_API_KEY   — article-body extraction fallback
    FIRECRAWL_BUDGET_PER_RUN — cap on Firecrawl calls per run (default 10)

Non-fatal: if every news source fails and the calendar is unreachable,
the script still emits an empty-but-structured payload so Phase 2 can run
with a skipped-catalyst section rather than breaking.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from src import article_extract as article_extract_mod
from src import econ_calendar as econ_calendar_mod
from src import news as news_mod


EXTRACT_CONCURRENCY = 10
ASSET_CONFIG_DIR = "config"


def _load_asset_configs() -> dict[str, dict]:
    """Load every `config/*.json` and return {asset: config}. Matches the
    glob-loading convention used by emit_payload.py so adding a new asset
    only requires dropping a new JSON."""
    root = Path(__file__).resolve().parent.parent
    cfg_dir = root / ASSET_CONFIG_DIR
    out: dict[str, dict] = {}
    for path in sorted(cfg_dir.glob("*.json")):
        with open(path) as f:
            cfg = json.load(f)
        asset = cfg.get("asset")
        if asset:
            out[asset] = cfg
    return out


def _enrich_with_content(items: list[dict]) -> list[dict]:
    """Fan out article fetches across a thread pool; each item gets a
    `content` field added IN PLACE. Failures pass through silently with
    `content: None` — the agent falls back to `summary` when content is
    absent."""
    if not items:
        return items

    def _work(item):
        return item, article_extract_mod.extract(item.get("url") or "")

    with ThreadPoolExecutor(max_workers=EXTRACT_CONCURRENCY) as pool:
        futures = [pool.submit(_work, it) for it in items]
        for fut in as_completed(futures):
            item, content = fut.result()
            item["content"] = content   # mutate in place
    return items


def build() -> dict:
    asset_configs = _load_asset_configs()
    article_extract_mod.reset_firecrawl_budget()
    news_mod.reset_shared_cache()

    per_asset_news: dict[str, dict] = {}
    all_items: list[dict] = []
    for asset, cfg in asset_configs.items():
        items, sources_used = news_mod.fetch_for_asset(cfg)
        per_asset_news[asset] = {
            "display":         cfg.get("display_name", asset.upper()),
            "relevance_terms": cfg.get("relevance_terms", []),
            "sources_used":    sources_used,
            "items":           items,
        }
        all_items.extend(items)

    # Dedup by URL across assets — the same CoinDesk story can match both
    # BTC and ETH when it mentions both. Keep the first occurrence; downstream
    # rendering takes items from per_asset_news[{asset}].items so we remove
    # duplicates from the later-asset entries. Asset order is insertion order
    # of the config glob (btc, eth alphabetically).
    seen_urls: set[str] = set()
    for asset, row in per_asset_news.items():
        kept: list[dict] = []
        for it in row["items"]:
            url = it.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            kept.append(it)
        row["items"] = kept

    # Rebuild the flat list for extraction, now without duplicates.
    unique_items = [
        it
        for row in per_asset_news.values()
        for it in row["items"]
    ]
    _enrich_with_content(unique_items)

    events = econ_calendar_mod.fetch()

    return {
        "timestamp_utc":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_asset_news":     per_asset_news,
        "economic_calendar":  events,
        "coverage_note":      (
            "News items are up to 5 per asset, last 48h. "
            "MARKETAUX is primary when the API key is set; CoinDesk + "
            "Cointelegraph RSS are crypto-wide feeds filtered per-asset "
            "by relevance_terms. Calendar holds next-48h events from "
            "Forex Factory; the analyst filters to USD high-impact in "
            "its Catalyst Gate."
        ),
    }


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "data/macro_context.json"
    payload = build()

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(payload, f, indent=2)

    total_news = sum(len(v["items"]) for v in payload["per_asset_news"].values())
    with_content = sum(
        1
        for v in payload["per_asset_news"].values()
        for it in v["items"]
        if it.get("content")
    )
    print(f"macro context written: {out_file}")
    print(
        f"  news items: {total_news} across {len(payload['per_asset_news'])} assets "
        f"({with_content}/{total_news} with extracted article content)"
    )
    print(f"  calendar events (48h window): {len(payload['economic_calendar'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
