---
name: crypto-setups
description: Full crypto trade-setup pipeline. Phase 1 fetches macro context (per-asset news + US economic calendar) once. Phase 2 per-asset: fetches OHLC from Binance across 5 timeframes + derivatives (OI, funding, liquidations) from Coinalyze/Bybit + options (Deribit), computes multi-source confluence zones, dispatches the crypto-setups-analyst agent to produce a Romanian trade-setup briefing with Catalyst-Gate awareness (2+ setups: long + short, optional 3rd), publishes to Notion under the asset's Swings parent, notifies Telegram. Takes an `asset` argument — `btc`, `eth`, or `all` (runs both sequentially in one session to conserve Routines quota). Use when the user wants actionable BTC or ETH trade setups with entries, stops, and R:R targets.
---

You are executing the crypto-setups analysis pipeline. The pipeline is **two-phase**: one macro fetch shared across the run, then a per-asset loop.

## Arguments

- `asset` — one of `btc`, `eth`, or `all`. Required.
  - `btc` / `eth` — run Phase 1 then Phase 2 for that asset only.
  - `all` — run Phase 1 once, then Phase 2 for BTC, then for ETH. Independent per-asset error handling: a failure in BTC must NOT skip ETH.

If the argument is missing or not one of `btc` / `eth` / `all`, stop and ask the user which asset to run.

## Step 1 — Refresh repo

```bash
git checkout main
git pull --ff-only
```

Ensures HEAD is on `main` and picks up code changes since session start.

## Step 2 — Phase 1: macro fetch (once per run)

```bash
python3 -m scripts.emit_macro
```

Writes `data/macro_context.json` with:
- Per-asset news (up to 5 items per asset, last 48h, via MARKETAUX for primary + CoinDesk RSS + Cointelegraph RSS, filtered per-asset by `relevance_terms` in the config; Google News RSS fallback when nothing else matched).
- Economic calendar (next 48h events, read from `data-mirror/ff_calendar_thisweek.json` refreshed every 4h by this repo's GHA workflow). The analyst's Catalyst Gate filters to USD high-impact.

Required env vars (all optional — each source degrades silently):
- `MARKETAUX_API_KEY` — primary news.
- `FIRECRAWL_API_KEY` — article-body extraction fallback.
- `FIRECRAWL_BUDGET_PER_RUN` — Firecrawl call cap per run (default `10`).

If this step fails entirely, **continue anyway** — the analyst handles an absent `macro_context.json` by skipping the Catalyst Gate's event logic and any news attribution. Do not abort the pipeline on a macro-fetch failure.

## Step 3 — Capture timestamp

```bash
echo $(date +%Y%m%d_%H%M%S)
```

Store as TIMESTAMP. In `all` mode, capture a fresh TIMESTAMP for each asset (wall-clock moves during the run).

## Step 4 — Phase 2: per-asset loop

For each asset in the dispatch plan (one for `btc` / `eth`, two for `all`), execute 4a → 4d sequentially. In `all` mode, a failure in one asset records an outcome and moves on — does NOT skip the next asset.

### 4a. Emit payload

Export `ASSET` so every downstream call sees it. In `all` mode, re-export before each asset's run.

```bash
export ASSET=<asset>         # btc or eth
python3 -m scripts.emit_payload data/payload.json
```

`emit_payload.py` reads `ASSET`, loads `config/$ASSET.json`, fetches Binance OHLC across 5 timeframes, pulls derivatives from Coinalyze + Bybit + Hyperliquid, pulls Deribit options, computes Fibonacci confluence zones + sessions + recent_action sidecars, writes `data/payload.json`.

Required env:
- `ASSET` — `btc` or `eth`.
- `COINALYZE_API_KEY` — OI + liquidations. If unset, derivatives degrade to `status=unavailable` and the pipeline continues.

If this exits non-zero, record the failure for this asset and continue.

### 4b. Dispatch crypto-setups-analyst agent

Use the Agent tool to spawn the `crypto-setups-analyst` agent with this minimal prompt:

```
Read and analyze: data/payload.json
Also read (if present): data/macro_context.json

Write your complete briefing as Markdown to data/briefing.md using the Write tool. Do not include a top-level page title — the publisher sets it. After the file is saved, respond with exactly: done data/briefing.md
```

That is the complete prompt. Do not add more context — the agent has its full instructions (role, Catalyst Gate, Regime Gate, Order Flow Vote, language rules, analysis framework, output format) embedded, and reads the asset identity from the payload's `asset` / `display_name` fields.

In `all` mode, the agent overwrites `data/briefing.md` each time — this is expected. Step 4c must run immediately after the agent returns and before the next asset overwrites the file.

If the agent returns `error: ...`, record the failure and move on.

### 4c. Publish to Notion

```bash
python3 publish_notion.py data/briefing.md TIMESTAMP
```

Substitute the actual TIMESTAMP. `publish_notion.py` reads `ASSET` and routes the page under the correct parent. The script prints the Notion URL on its last stdout line.

Required env: `NOTION_TOKEN`.

On non-zero exit, capture stderr and record the failure.

### 4d. Notify Telegram (non-fatal)

```bash
python3 notify_telegram.py "$(echo $ASSET | tr a-z A-Z) Swings briefing published $(date +%Y-%m-%d\ %H:%M)
[View on Notion](<notion_url>)"
```

Substitute `<notion_url>`. Required env: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID_BTC` / `TELEGRAM_CHAT_ID_ETH` (legacy fallback: `TELEGRAM_CHAT_ID`).

Idempotent: missing env → silent no-op; API failure → non-fatal.

## Step 5 — Confirm

**Single-asset mode** — report one outcome:
- **On success:** `Analysis uploaded to Notion: <notion_url>`
- **On macro failure (non-fatal, continued):** include a leading line `Macro fetch failed (continued without catalysts): <stderr>`, then the success / failure line.
- **On agent failure:** `$ASSET Swings failed at analysis step: <error from agent>`
- **On publish failure:** `$ASSET Swings failed at publish step: <stderr>`
- If Telegram failed non-fatally, append: `Telegram notification failed: <stderr>`.

**`all` mode** — after both assets run, report one consolidated message:

```
Crypto Swings (all):
- BTC: <notion_url>   ← or: BTC: failed at <step> — <error summary>
- ETH: <notion_url>   ← or: ETH: failed at <step> — <error summary>
```

If macro failed non-fatally, prepend: `Macro fetch failed (continued without catalysts).`

If any asset's Telegram failed non-fatally, append: `Telegram notification failed for: BTC` (or `ETH`, or `BTC, ETH`).

Do not return early just because one asset failed — always report the final state of both.
