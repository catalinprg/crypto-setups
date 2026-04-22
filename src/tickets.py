"""Persistent ticket ledger — survives across stateless cloud runs.

Each run:
  1. Loads `state/tickets_{asset}.jsonl` (one ticket per line).
  2. Evaluates every `armed` ticket against fresh market data (1h/4h/1d bars
     since `created_at` + current price) and mutates its status if an exit
     condition has fired.
  3. Writes the ledger back and exposes the evaluated ticket set to the
     analyst agent via the payload.

The module is pure functions + simple JSONL I/O — no network, no globals.
Exit-condition evaluation is deterministic and has unit tests.

Ticket lifecycle:
    armed ── (limit touched) ─────────────→ triggered ─┬── stop hit ──→ stopped
      │                                                │── tp2 hit ───→ tp2_filled
      │                                                └── tp1 hit ───→ tp1_filled (tp1_reached flag persists)
      │── invalidation fires ─────────────→ invalidated
      │── kill-switch up fires ───────────→ killed_up
      │── kill-switch down fires ─────────→ killed_down
      └── expiry hours pass without fill ─→ expired

Terminal states: stopped, tp2_filled, invalidated, killed_up, killed_down, expired.
Non-terminal: armed, triggered, tp1_filled (tp1_filled means the runner leg
is live — ticket remains actionable until stop / tp2 / invalidation / kill).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

TicketStatus = Literal[
    "armed",
    "triggered",
    "tp1_filled",
    "tp2_filled",
    "stopped",
    "invalidated",
    "killed_up",
    "killed_down",
    "expired",
]

TERMINAL_STATUSES: frozenset[str] = frozenset({
    "tp2_filled", "stopped", "invalidated", "killed_up", "killed_down", "expired",
})

_CONDITION_TYPES: frozenset[str] = frozenset({
    "1h_close_above", "1h_close_below",
    "4h_close_above", "4h_close_below",
    "1d_close_above", "1d_close_below",
    "none",
})


def ledger_path(asset: str, root: Path | None = None) -> Path:
    root = root or Path(__file__).resolve().parent.parent
    return root / "state" / f"tickets_{asset}.jsonl"


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def save_ledger(path: Path, tickets: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for t in tickets:
            f.write(json.dumps(t, separators=(",", ":"), sort_keys=True) + "\n")


@dataclass
class MarketSnapshot:
    """Snapshot of price action used to evaluate ticket exit conditions.

    `bars_*_since_created` are the closed bars whose open time is >= the
    ticket's `created_at`. The evaluator only looks at CLOSED bars for any
    close-based condition — an in-progress bar can still reverse.
    """
    now_utc: datetime
    current_price: float
    bars_1h_since_created: list[dict]
    bars_4h_since_created: list[dict]
    bars_1d_since_created: list[dict]


def _condition_fires(
    condition: dict | None, snapshot: MarketSnapshot
) -> tuple[bool, float | None, str | None]:
    """Evaluate a {type, price} rule against the snapshot's closed bars.

    Returns (fired, trigger_price, trigger_ts_iso). `fired` is False when the
    condition is None, its type is "none", its type is unknown (defensive), or
    no qualifying bar has closed.
    """
    if not condition:
        return False, None, None
    ctype = condition.get("type")
    cprice = condition.get("price")
    if ctype not in _CONDITION_TYPES or ctype == "none" or cprice is None:
        return False, None, None

    bars = {
        "1h_close_above": snapshot.bars_1h_since_created,
        "1h_close_below": snapshot.bars_1h_since_created,
        "4h_close_above": snapshot.bars_4h_since_created,
        "4h_close_below": snapshot.bars_4h_since_created,
        "1d_close_above": snapshot.bars_1d_since_created,
        "1d_close_below": snapshot.bars_1d_since_created,
    }[ctype]

    above = ctype.endswith("_above")
    for bar in bars:
        close = bar.get("close")
        if close is None:
            continue
        if (above and close > cprice) or ((not above) and close < cprice):
            ts_ms = bar.get("ts")
            ts_iso = (
                datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
                if ts_ms is not None else None
            )
            return True, float(close), ts_iso
    return False, None, None


def _bars_extremes(bars: list[dict]) -> tuple[float | None, float | None]:
    if not bars:
        return None, None
    highs = [b["high"] for b in bars if b.get("high") is not None]
    lows = [b["low"] for b in bars if b.get("low") is not None]
    return (max(highs) if highs else None), (min(lows) if lows else None)


def _resolve(
    ticket: dict, new_status: TicketStatus, reason: str,
    snapshot: MarketSnapshot, trigger_price: float | None = None,
) -> dict:
    out = dict(ticket)
    out["status"] = new_status
    out["resolved_at"] = snapshot.now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["resolved_price"] = trigger_price if trigger_price is not None else snapshot.current_price
    out["resolution_reason"] = reason
    return out


def evaluate_ticket(ticket: dict, snapshot: MarketSnapshot) -> dict:
    """Return an updated copy of the ticket with status advanced per the
    snapshot. Never mutates the input. Terminal tickets pass through unchanged.
    """
    status = ticket.get("status", "armed")
    if status in TERMINAL_STATUSES:
        return dict(ticket)

    # --- Kill-switches trump everything, in either direction.
    for field, new_status in (
        ("kill_switch_up", "killed_up"),
        ("kill_switch_down", "killed_down"),
    ):
        fired, trig_price, _ = _condition_fires(ticket.get(field), snapshot)
        if fired:
            return _resolve(ticket, new_status, f"{field}_fired", snapshot, trig_price)

    # --- Invalidation (setup-specific exit).
    fired, trig_price, _ = _condition_fires(ticket.get("invalidation"), snapshot)
    if fired:
        return _resolve(ticket, "invalidated", "invalidation_fired", snapshot, trig_price)

    # --- Limit fill + post-fill TP/SL checks.
    direction = ticket.get("direction", "long")
    entry_1 = ticket.get("entry_1")
    entry_2 = ticket.get("entry_2")
    entry_shallow = entry_1 if entry_1 is not None else entry_2
    entry_deep = entry_2 if entry_2 is not None else entry_1

    highest, lowest = _bars_extremes(snapshot.bars_1h_since_created)
    triggered_before = status in ("triggered", "tp1_filled")
    triggered_now = triggered_before

    if not triggered_now and entry_shallow is not None:
        # Long: price sweeps down through the shallow entry → fill.
        # Short: price sweeps up through the shallow entry → fill.
        if direction == "long" and lowest is not None and lowest <= entry_shallow:
            triggered_now = True
        elif direction == "short" and highest is not None and highest >= entry_shallow:
            triggered_now = True

    if triggered_now and not triggered_before:
        out = dict(ticket)
        out["status"] = "triggered"
        out["triggered_at"] = snapshot.now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        # After marking triggered, still evaluate stop/TP in same pass —
        # caller gets the most advanced state reachable from this snapshot.
        ticket = out

    if triggered_now:
        stop = ticket.get("stop")
        tp1 = ticket.get("tp1")
        tp2 = ticket.get("tp2")

        if stop is not None:
            if direction == "long" and lowest is not None and lowest <= stop:
                return _resolve(ticket, "stopped", "stop_hit", snapshot, stop)
            if direction == "short" and highest is not None and highest >= stop:
                return _resolve(ticket, "stopped", "stop_hit", snapshot, stop)

        if tp2 is not None:
            if direction == "long" and highest is not None and highest >= tp2:
                return _resolve(ticket, "tp2_filled", "tp2_hit", snapshot, tp2)
            if direction == "short" and lowest is not None and lowest <= tp2:
                return _resolve(ticket, "tp2_filled", "tp2_hit", snapshot, tp2)

        if tp1 is not None and ticket.get("status") != "tp1_filled":
            if direction == "long" and highest is not None and highest >= tp1:
                out = dict(ticket)
                out["status"] = "tp1_filled"
                out["tp1_reached_at"] = snapshot.now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                return out
            if direction == "short" and lowest is not None and lowest <= tp1:
                out = dict(ticket)
                out["status"] = "tp1_filled"
                out["tp1_reached_at"] = snapshot.now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                return out

        return ticket

    # --- Not triggered: check expiry.
    expiry_hours = ticket.get("expiry_hours") or (24 if ticket.get("setup_type") == "day_trade" else 72)
    created_at = ticket.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = (snapshot.now_utc - created).total_seconds() / 3600
            if age_hours >= expiry_hours:
                return _resolve(ticket, "expired", "expiry_reached", snapshot)
        except ValueError:
            pass

    return dict(ticket)


def build_snapshot(
    now_utc: datetime,
    current_price: float,
    bars_by_tf: dict[str, list[dict]],
    created_at_iso: str,
) -> MarketSnapshot:
    """Filter bars to those closed at or after `created_at_iso`. Treats the
    bar's open time (`ts`, ms) as the inclusion anchor.
    """
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # Malformed timestamp — treat ticket as fresh (no bars qualify).
        created = now_utc
    cutoff_ms = int(created.timestamp() * 1000)

    def since(bars: list[dict]) -> list[dict]:
        return [b for b in (bars or []) if b.get("ts", 0) >= cutoff_ms]

    return MarketSnapshot(
        now_utc=now_utc,
        current_price=current_price,
        bars_1h_since_created=since(bars_by_tf.get("1h", [])),
        bars_4h_since_created=since(bars_by_tf.get("4h", [])),
        bars_1d_since_created=since(bars_by_tf.get("1d", [])),
    )


def run_ledger_cycle(
    asset: str,
    now_utc: datetime,
    current_price: float,
    bars_by_tf: dict[str, list[dict]],
    ledger_root: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Load ledger, evaluate every non-terminal ticket, persist, and return
    (active_tickets, resolved_this_run) for embedding in the payload.

    - `active_tickets`  → tickets still non-terminal after evaluation
      (armed, triggered, tp1_filled). These are the ones the analyst shows
      at the top of the next briefing as "still live."
    - `resolved_this_run` → tickets whose status transitioned to terminal
      (or to triggered/tp1_filled from armed) on this run. The analyst
      reports these as "changed since last briefing."
    """
    path = ledger_path(asset, ledger_root)
    ledger = load_ledger(path)
    active: list[dict] = []
    resolved: list[dict] = []
    updated: list[dict] = []
    for ticket in ledger:
        prior_status = ticket.get("status", "armed")
        if prior_status in TERMINAL_STATUSES:
            updated.append(ticket)
            continue
        snapshot = build_snapshot(
            now_utc, current_price, bars_by_tf,
            ticket.get("created_at", now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        new_ticket = evaluate_ticket(ticket, snapshot)
        updated.append(new_ticket)
        new_status = new_ticket.get("status")
        if new_status != prior_status:
            resolved.append(new_ticket)
        if new_status not in TERMINAL_STATUSES:
            active.append(new_ticket)
    save_ledger(path, updated)
    return active, resolved


def append_new_tickets(
    asset: str, new_tickets: list[dict], ledger_root: Path | None = None,
) -> int:
    """Append freshly-minted tickets (status=armed) to the ledger. Returns
    the number appended. IDs are assigned here if missing.
    """
    path = ledger_path(asset, ledger_root)
    ledger = load_ledger(path)
    existing_ids = {t.get("id") for t in ledger}
    appended = 0
    for raw in new_tickets:
        t = dict(raw)
        t.setdefault("status", "armed")
        t.setdefault("asset", asset)
        if not t.get("id") or t["id"] in existing_ids:
            t["id"] = _mint_id(asset, t, existing_ids)
        existing_ids.add(t["id"])
        ledger.append(t)
        appended += 1
    save_ledger(path, ledger)
    return appended


def _mint_id(asset: str, ticket: dict, taken: set[str | None]) -> str:
    created = ticket.get("created_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    compact = created.replace("-", "").replace(":", "").replace("Z", "Z").replace("T", "T")
    direction = ticket.get("direction", "long").upper()
    base = f"{asset.upper()}_{direction}_{compact}"
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i:02d}" in taken:
        i += 1
    return f"{base}_{i:02d}"
