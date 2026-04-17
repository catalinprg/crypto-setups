# crypto-swings

Cloud-native crypto swings briefing pipeline for a single asset (BTC or ETH) per run. Designed to run inside a Claude Code cloud session (iPhone / web / Desktop Remote mode). Successor to the twin `btc-swings` + `eth-swings` repos — the full pipeline is shared; the active asset is selected by the `ASSET` env var.

## Flow

1. `scripts/emit_payload.py` reads `ASSET` (`btc` or `eth`), loads `config/$ASSET.json`, pulls OHLC across 5 timeframes (Binance) + derivatives context (OI from Coinalyze, funding from Bybit, 72h liquidation history from Coinalyze with price-at-time enrichment from the 4h bars). Computes ATR-adaptive swing pivots → Fibonacci confluence zones → writes `data/payload.json`.
2. The `crypto-swings` skill dispatches the `crypto-swings-analyst` agent with that JSON.
3. The agent produces a hedged briefing in Romanian (trading terms in English) and writes it to `data/briefing.md`.
4. `publish_notion.py` converts the markdown to Notion blocks and creates a child page under the asset's Swings parent (`BTC Swings` or `ETH Swings`).
5. `notify_telegram.py` sends a link to Telegram.

## Configuration

Per-asset config lives in `config/{asset}.json`:

```json
{
  "asset": "btc",
  "display_name": "BTC",
  "symbol": "BTCUSDT",
  "coinalyze_symbols": ["BTCUSDT_PERP.A", "BTCUSDT.6", "BTCUSDT_PERP.3"],
  "notion_parent_id": "<notion page id>"
}
```

Add a new asset by dropping a new JSON file in `config/` — the loader discovers it via glob, no code change required.

## Required environment variables

Set on the cloud environment (not committed to the repo). Each Routines trigger runs the pipeline for one asset, so the per-asset-specific values live in the trigger's env:

- `ASSET` — `btc` or `eth`. Selects the config file. Required.
- `COINALYZE_API_KEY` — for open interest and liquidation data ([sign up](https://coinalyze.net/)). If unset, the derivatives section degrades to `status=unavailable` and the pipeline continues with pure fib analysis.
- `NOTION_TOKEN` — Notion Internal Integration Token ([create one](https://www.notion.so/profile/integrations)). The parent page for the active asset must be shared with the integration.

## Optional — Telegram notifications

Set both per trigger (each asset should have its own chat):

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` — asset-specific chat ID

If either is unset, the notification step is a silent no-op. Also add `api.telegram.org` to the environment's outbound network allowlist.

## Cloud setup (one-time)

In the Claude Code cloud environment configuration, set the bootstrap / setup-commands script to:

```bash
#!/bin/bash
uv pip install --system httpx requests
```

This runs automatically every time a session spins up. Both packages are pure-Python — no compilation needed.

Outbound network allowlist on the cloud environment must include:
- `data-api.binance.vision` (OHLC + spot book ticker for basis)
- `api.bybit.com` (perp ticker — funding rate + mark price)
- `api.hyperliquid.xyz` (cross-venue funding)
- `api.coinalyze.net` (OI, liquidations)
- `api.notion.com` (publishing)
- `api.telegram.org` (notifications, optional)

## Usage

In a Claude Code cloud session pointed at this repo, run:

```
/crypto-swings btc
/crypto-swings eth
```

The skill runs the full pipeline for the given asset and replies with the Notion URL.

## Liquidity pools (separate layer from fib confluence)

Alongside the fib zones, the payload now carries a `liquidity` section with buy-side (BSL, above swing highs) and sell-side (SSL, below swing lows) pool proxies derived from the same swing pivots. Pools are clustered with the `0.25 × daily_ATR` radius, ranked by unswept-first then by `TF_WEIGHTS × touches`, and filtered to within ±20% of price. The agent uses pool overlap with a fib zone to append tags like `· BSL-pool ~18h (3× 1w+1d)` and emits standalone unswept pools in a separate `### Zone de liquidity` section with magnet language — never as support/rezistență.

## What the output looks like

The briefing is written in Romanian with English trading terms. Example (BTC):

> **Pe scurt:** Prețul pare să se consolideze într-un confluence cluster dens ($73.8k–$75.0k), deci această bandă poate funcționa mai mult ca chop zone decât ca support curat. Finanțarea anualizată de ~2% și OI-ul aproape flat sugerează un positioning neutru.

With structured `### Rezistență`, `### Suport`, and `### De urmărit` sections below. Support zones that contain the current price are flagged as `[zona curentă]` rather than mislabeled as support. Liquidation clusters are only referenced when their price-at-time range actually overlaps a named zone.

## Local development

```bash
uv sync --extra dev
uv run pytest                                                # default ASSET=btc
ASSET=eth uv run pytest tests/test_config.py                 # ETH config sanity

COINALYZE_API_KEY=... ASSET=btc uv run python -m scripts.emit_payload /tmp/btc.json
COINALYZE_API_KEY=... ASSET=eth uv run python -m scripts.emit_payload /tmp/eth.json
```
