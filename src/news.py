"""News fetch for crypto-setups.

Combines three sources:
  1. MARKETAUX — per-asset symbol (BTC / ETH). Primary when the API key
     is set. Returns structured items with summaries.
  2. Crypto-native RSS — CoinDesk + Cointelegraph. Always fetched (keyless).
     Crypto-wide feeds; per-asset filtering happens via `relevance_terms`.
  3. Google News RSS — fallback when MARKETAUX is disabled AND the
     crypto-native feeds produced nothing for this asset.

Env vars (optional; each source degrades silently):
  MARKETAUX_API_KEY — enables primary source

Returns a list of dicts: headline / source / published / url / summary /
content (content is added later by article_extract; set to None here).
"""
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY", "")

NEWS_MAX_ITEMS = 5
NEWS_MAX_AGE_HOURS = 48
HTTP_TIMEOUT = 8
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 2
RSS_TIMEOUT = 5
RSS_MAX_RETRIES = 3

COINDESK_FEED      = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH_FEED = "https://cointelegraph.com/rss"

# Shared across the run so we don't hammer the same feed twice when
# `all` mode processes both BTC and ETH. emit_macro calls
# `reset_shared_cache()` at the start of each run.
_SHARED_RSS_CACHE: dict[str, list[dict]] = {}


def reset_shared_cache() -> None:
    _SHARED_RSS_CACHE.clear()


def _http_get(url: str, headers: Optional[dict] = None, timeout: int = HTTP_TIMEOUT) -> Optional[requests.Response]:
    """GET with exponential backoff on 5xx AND transient network errors.
    Returns the final Response (possibly still 5xx) or None if every
    attempt raised."""
    resp: Optional[requests.Response] = None
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code < 500:
                return resp
        except requests.RequestException:
            pass
        if attempt < HTTP_MAX_RETRIES - 1:
            time.sleep(HTTP_BACKOFF_BASE ** attempt)
    return resp


def _is_fresh_iso(iso_str: str) -> bool:
    try:
        pub_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return False
    age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
    return 0 <= age_h <= NEWS_MAX_AGE_HOURS


def fetch_marketaux(symbol: str, max_items: int = NEWS_MAX_ITEMS) -> Optional[list[dict]]:
    if not MARKETAUX_API_KEY:
        return None
    today = datetime.now(timezone.utc).date()
    published_after = (today - timedelta(days=2)).isoformat() + "T00:00:00"
    url = (
        f"https://api.marketaux.com/v1/news/all"
        f"?symbols={symbol}"
        f"&filter_entities=true"
        f"&published_after={published_after}"
        f"&language=en"
        f"&api_token={MARKETAUX_API_KEY}"
    )
    resp = _http_get(url, timeout=HTTP_TIMEOUT)
    if resp is None or resp.status_code != 200:
        return None
    items: list[dict] = []
    for art in resp.json().get("data", []):
        pub_str = art.get("published_at", "")
        if not pub_str or not _is_fresh_iso(pub_str):
            continue
        article_url = art.get("url", "")
        if not article_url:
            continue
        headline = art.get("title", "").strip()
        summary = (art.get("description") or art.get("snippet") or "")[:300].strip() or headline or None
        items.append({
            "headline":  headline,
            "source":    art.get("source", "").strip(),
            "published": pub_str,
            "url":       article_url,
            "summary":   summary,
            "content":   None,
        })
        if len(items) >= max_items:
            break
    return items or None


def _fetch_rss_url(feed_url: str, cache_key: str) -> list[dict]:
    """Fetch + parse an RSS feed once per run. Cached via module-level dict
    so multiple assets in the same run don't re-fetch."""
    if cache_key in _SHARED_RSS_CACHE:
        return _SHARED_RSS_CACHE[cache_key]

    try:
        import feedparser
    except ImportError:
        _SHARED_RSS_CACHE[cache_key] = []
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    items: list[dict] = []
    for attempt in range(RSS_MAX_RETRIES):
        try:
            resp = requests.get(feed_url, headers=headers, timeout=RSS_TIMEOUT)
            if resp.status_code != 200:
                if attempt < RSS_MAX_RETRIES - 1:
                    time.sleep(HTTP_BACKOFF_BASE ** attempt)
                continue
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:40]:
                pub_date = entry.get("published", "") or entry.get("updated", "")
                if not pub_date:
                    continue
                try:
                    pub_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
                    if not pub_tuple:
                        continue
                    pub_dt = datetime(*pub_tuple[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                if age_h > NEWS_MAX_AGE_HOURS:
                    continue
                source = (
                    entry.source.title if hasattr(entry, "source")
                    else cache_key
                )
                title = entry.get("title", "").strip()
                summary_raw = entry.get("summary", "") or entry.get("description", "")
                # Strip HTML from RSS summaries — common in CoinDesk/CT feeds.
                summary = _strip_html(summary_raw)[:300]
                items.append({
                    "headline":  title,
                    "source":    source.strip() if isinstance(source, str) else cache_key,
                    "published": pub_dt.isoformat(),
                    "url":       entry.get("link", ""),
                    "summary":   summary or title,
                    "content":   None,
                })
            break
        except requests.RequestException:
            if attempt < RSS_MAX_RETRIES - 1:
                time.sleep(HTTP_BACKOFF_BASE ** attempt)

    _SHARED_RSS_CACHE[cache_key] = items
    return items


def _strip_html(s: str) -> str:
    """Light HTML stripper for RSS summaries. Not a full parser — the
    crypto feeds emit simple `<p>...</p>` and `<a>...</a>` wrapping."""
    import re
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _filter_by_relevance(items: list[dict], terms: list[str]) -> list[dict]:
    """Keep items that mention ANY term in headline + summary. Case-insensitive
    substring match. Falls back to unfiltered when `terms` is empty."""
    if not terms:
        return items
    terms_lower = [t.lower() for t in terms if t]
    kept = []
    for it in items:
        text = " ".join([
            (it.get("headline") or ""),
            (it.get("summary") or ""),
        ]).lower()
        if any(t in text for t in terms_lower):
            kept.append(it)
    return kept


def fetch_google_news_rss(query: str, max_items: int = NEWS_MAX_ITEMS) -> list[dict]:
    """Google News RSS fallback, per-asset query. Not cached — query is
    asset-specific."""
    try:
        import feedparser
    except ImportError:
        return []

    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for attempt in range(RSS_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=RSS_TIMEOUT)
            if resp.status_code != 200:
                if attempt < RSS_MAX_RETRIES - 1:
                    time.sleep(HTTP_BACKOFF_BASE ** attempt)
                continue
            feed = feedparser.parse(resp.content)
            items: list[dict] = []
            for entry in feed.entries:
                try:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                if age_h > NEWS_MAX_AGE_HOURS:
                    continue
                source = (
                    entry.source.title if hasattr(entry, "source")
                    else entry.title.rsplit(" - ", 1)[-1]
                )
                title = (
                    entry.title.rsplit(" - ", 1)[0]
                    if " - " in entry.title else entry.title
                )
                items.append({
                    "headline":  title.strip(),
                    "source":    source.strip(),
                    "published": pub_dt.isoformat(),
                    "url":       entry.link,
                    "summary":   title.strip(),
                    "content":   None,
                })
                if len(items) >= max_items:
                    break
            return items
        except requests.RequestException:
            if attempt < RSS_MAX_RETRIES - 1:
                time.sleep(HTTP_BACKOFF_BASE ** attempt)
    return []


def fetch_for_asset(asset_cfg: dict) -> tuple[list[dict], list[str]]:
    """Fetch + filter + merge news for one asset config. Returns
    (items, sources_used) where `sources_used` is the list of feeds that
    contributed items (any of: 'marketaux', 'coindesk', 'cointelegraph',
    'google_rss').

    Dedup by URL (first seen wins). Cap at NEWS_MAX_ITEMS."""
    mx_symbol = asset_cfg.get("marketaux_symbol")
    rss_query = asset_cfg.get("rss_query") or f"{asset_cfg.get('display_name', '')} crypto news"
    terms     = asset_cfg.get("relevance_terms", [])

    collected: list[dict] = []
    sources_used: list[str] = []
    seen_urls: set[str] = set()

    def _extend(items: list[dict], tag: str) -> None:
        before = len(collected)
        for it in items:
            url = it.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                collected.append(it)
        if len(collected) > before:
            sources_used.append(tag)

    if mx_symbol:
        mx_items = fetch_marketaux(mx_symbol)
        if mx_items:
            _extend(mx_items, "marketaux")

    cd_items = _fetch_rss_url(COINDESK_FEED, "coindesk")
    ct_items = _fetch_rss_url(COINTELEGRAPH_FEED, "cointelegraph")

    # Filter crypto-wide feeds by this asset's relevance_terms.
    _extend(_filter_by_relevance(cd_items, terms), "coindesk")
    _extend(_filter_by_relevance(ct_items, terms), "cointelegraph")

    if not collected:
        g_items = fetch_google_news_rss(rss_query)
        if g_items:
            _extend(g_items, "google_rss")

    collected.sort(key=lambda it: it.get("published", ""), reverse=True)
    return collected[:NEWS_MAX_ITEMS], sources_used or ["none"]
