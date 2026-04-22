"""Extract newly-emitted tickets from `data/new_tickets.json` and append them
to the persistent ledger `state/tickets_{asset}.jsonl`.

Runs immediately after the analyst agent returns, before the Notion publish
step. The analyst writes a structured JSON sidecar alongside the Markdown
briefing so this step does not need to parse prose.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import tickets as tickets_mod
from src.config import CONFIG


def _validate(ticket: dict) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errors: list[str] = []
    if ticket.get("direction") not in ("long", "short"):
        errors.append(f"direction must be 'long' or 'short', got {ticket.get('direction')!r}")
    if ticket.get("setup_type") not in ("swing", None):
        errors.append(
            f"setup_type must be 'swing' (or omitted), got {ticket.get('setup_type')!r}. "
            f"day_trade horizon is retired; all setups are swing."
        )
    for field in ("entry_1", "entry_2", "stop"):
        v = ticket.get(field)
        if not isinstance(v, (int, float)):
            errors.append(f"{field} must be numeric, got {v!r}")
    if "tp1" in ticket or "tp2" in ticket:
        errors.append(
            "tp1/tp2 fields are no longer supported — the analyst must not emit targets"
        )
    return errors


def main() -> int:
    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/new_tickets.json")
    if not in_path.exists():
        print(f"no new_tickets file at {in_path}; skipping (agent emitted 0 tickets)", file=sys.stderr)
        return 0

    try:
        payload = json.loads(in_path.read_text())
    except json.JSONDecodeError as e:
        print(f"new_tickets.json malformed: {e}", file=sys.stderr)
        return 1

    asset = payload.get("asset") or CONFIG.asset
    if asset != CONFIG.asset:
        print(
            f"asset mismatch: file says {asset!r}, CONFIG says {CONFIG.asset!r}; "
            f"refusing to cross-write ledger",
            file=sys.stderr,
        )
        return 1

    created_at = payload.get("timestamp_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_tickets = payload.get("tickets") or []

    if not raw_tickets:
        print("no new tickets emitted this run; ledger unchanged")
        return 0

    prepared: list[dict] = []
    for i, t in enumerate(raw_tickets):
        errors = _validate(t)
        if errors:
            print(f"ticket[{i}] invalid, skipping: {'; '.join(errors)}", file=sys.stderr)
            continue
        out = dict(t)
        out.setdefault("created_at", created_at)
        out.setdefault("status", "armed")
        out.setdefault("asset", asset)
        prepared.append(out)

    if not prepared:
        print("all emitted tickets failed validation; ledger unchanged", file=sys.stderr)
        return 1

    n = tickets_mod.append_new_tickets(asset, prepared)
    print(f"appended {n} ticket(s) to {tickets_mod.ledger_path(asset)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
