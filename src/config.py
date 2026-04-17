"""Per-asset configuration loader.

Reads `config/{asset}.json` based on the ASSET env var (default: "btc").
Exposes a module-level `CONFIG` singleton that other modules import. Keeping
configuration in JSON files (rather than Python) lets a new asset be added
just by dropping a new JSON file in `config/` — no code changes.

The default of "btc" when ASSET is unset preserves local-test and
legacy-invocation behavior from the btc-swings repo this module was merged
from. Production runs (Routines triggers) must set ASSET explicitly.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def supported_assets() -> tuple[str, ...]:
    """Assets currently wired up — one per `config/*.json` file."""
    return tuple(sorted(p.stem for p in CONFIG_DIR.glob("*.json")))


@dataclass(frozen=True)
class AssetConfig:
    asset: str
    display_name: str
    symbol: str
    coinalyze_symbols: tuple[str, ...]
    notion_parent_id: str


def load_config(asset: str) -> AssetConfig:
    asset = asset.lower().strip()
    available = supported_assets()
    path = CONFIG_DIR / f"{asset}.json"
    if not path.exists():
        raise ValueError(
            f"unsupported ASSET={asset!r}; expected one of {available}"
        )
    raw = json.loads(path.read_text())
    return AssetConfig(
        asset=raw["asset"],
        display_name=raw["display_name"],
        symbol=raw["symbol"],
        coinalyze_symbols=tuple(raw["coinalyze_symbols"]),
        notion_parent_id=raw["notion_parent_id"],
    )


CONFIG: AssetConfig = load_config(os.environ.get("ASSET", "btc"))
