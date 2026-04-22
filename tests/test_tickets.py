"""Tests for the persistent ticket ledger + exit-condition evaluator.

The ledger tracks PENDING LIMIT ORDERS only. Once any entry is touched, the
ticket transitions to `triggered` and is terminal — the live trade (post-
fill management, SL hits, exits) is handled by the human, not this module.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.tickets import (
    MarketSnapshot,
    TERMINAL_STATUSES,
    append_new_tickets,
    build_snapshot,
    evaluate_ticket,
    ledger_path,
    load_ledger,
    run_ledger_cycle,
    save_ledger,
)


NOW = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)


def _bar(ts_iso: str, high: float, low: float, close: float) -> dict:
    ts_ms = int(datetime.fromisoformat(ts_iso).timestamp() * 1000)
    return {"ts": ts_ms, "open": close, "high": high, "low": low, "close": close, "volume": 0.0}


def _ticket(**overrides) -> dict:
    """Default long swing ticket used as the base for per-test mutations."""
    base = {
        "id": "BTC_LONG_20260422T0630Z",
        "asset": "btc",
        "direction": "long",
        "setup_type": "swing",
        "created_at": "2026-04-22T06:30:00Z",
        "confidence": 85,
        "entry_1": 76894.0,
        "entry_2": 76414.0,
        "stop": 75800.0,
        "invalidation": {"type": "4h_close_below", "price": 76132.0},
        "kill_switch_up": {"type": "1d_close_above", "price": 78728.0},
        "kill_switch_down": {"type": "1d_close_below", "price": 73724.0},
        "status": "armed",
    }
    base.update(overrides)
    return base


def _snap(bars_1h=None, bars_4h=None, bars_1d=None, price=77500.0, now=NOW):
    return MarketSnapshot(
        now_utc=now,
        current_price=price,
        bars_1h_since_created=bars_1h or [],
        bars_4h_since_created=bars_4h or [],
        bars_1d_since_created=bars_1d or [],
    )


# ---------------------------------------------------------------- pending
def test_armed_stays_armed_when_price_never_touches_entry():
    snap = _snap(bars_1h=[_bar("2026-04-22T07:00:00+00:00", 77800, 77100, 77400)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "armed"


# ---------------------------------------------------------------- trigger = terminal
def test_long_triggers_on_shallow_entry_touch():
    # Price sweeps down through entry_1 but not entry_2. Still a fill (limit
    # at entry_1 would execute). Triggered is terminal.
    snap = _snap(bars_1h=[_bar("2026-04-22T09:00:00+00:00", 77500, 76800, 77200)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "triggered"
    assert out["status"] in TERMINAL_STATUSES
    assert out["resolution_reason"] == "entry_touched"
    assert out["resolved_price"] == 76894.0  # shallow entry price, not the bar low


def test_long_triggers_on_deep_entry_when_price_wicks_through_both():
    # Price wicks through entry_2 → both legs would have filled. Still just
    # one triggered event (ledger doesn't care about ladder mechanics).
    snap = _snap(bars_1h=[_bar("2026-04-22T09:00:00+00:00", 77000, 76300, 76700)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "triggered"


def test_short_triggers_on_shallow_entry_touch():
    ticket = _ticket(
        direction="short",
        entry_1=78500.0, entry_2=78900.0,
        stop=79500.0,
        invalidation={"type": "4h_close_above", "price": 79100.0},
    )
    snap = _snap(bars_1h=[_bar("2026-04-22T09:00:00+00:00", 78600, 77900, 78200)])
    out = evaluate_ticket(ticket, snap)
    assert out["status"] == "triggered"
    assert out["resolved_price"] == 78500.0  # shallow short entry


# ---------------------------------------------------------------- invalidation
def test_invalidation_fires_on_4h_close_below():
    snap = _snap(bars_4h=[_bar("2026-04-22T08:00:00+00:00", 77000, 75800, 76000)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "invalidated"
    assert out["status"] in TERMINAL_STATUSES


def test_invalidation_ignores_wick_below_if_close_holds():
    snap = _snap(bars_4h=[_bar("2026-04-22T08:00:00+00:00", 77000, 75800, 76500)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "armed"


def test_invalidation_beats_trigger_in_same_pass():
    # Same 4h window: price wicks into entry AND closes below invalidation.
    # Structural death wins — the fill was into a dying setup.
    snap = _snap(
        bars_1h=[_bar("2026-04-22T08:00:00+00:00", 77000, 76300, 76400)],
        bars_4h=[_bar("2026-04-22T08:00:00+00:00", 77000, 75800, 76000)],
    )
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "invalidated"


# ---------------------------------------------------------------- kill-switch
def test_kill_switch_up_fires_on_1d_close_above():
    snap = _snap(bars_1d=[_bar("2026-04-22T00:00:00+00:00", 79000, 77000, 78800)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "killed_up"


def test_kill_switch_down_fires_on_1d_close_below():
    snap = _snap(bars_1d=[_bar("2026-04-22T00:00:00+00:00", 77000, 73500, 73700)])
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "killed_down"


def test_kill_switch_trumps_invalidation_and_trigger():
    snap = _snap(
        bars_1h=[_bar("2026-04-22T08:00:00+00:00", 77000, 76300, 76400)],   # would trigger
        bars_4h=[_bar("2026-04-22T08:00:00+00:00", 77000, 75800, 76000)],   # would invalidate
        bars_1d=[_bar("2026-04-22T00:00:00+00:00", 77000, 73500, 73700)],   # kills first
    )
    out = evaluate_ticket(_ticket(), snap)
    assert out["status"] == "killed_down"


# ---------------------------------------------------------------- expiry
def test_expiry_fires_for_untriggered_swing_after_72h():
    old_ticket = _ticket(created_at="2026-04-18T06:30:00Z")  # ~96h before NOW
    out = evaluate_ticket(old_ticket, _snap())
    assert out["status"] == "expired"


def test_day_trade_expires_at_24h_not_72h():
    old_ticket = _ticket(
        created_at="2026-04-21T08:00:00Z", setup_type="day_trade",
    )  # 26h ago
    out = evaluate_ticket(old_ticket, _snap())
    assert out["status"] == "expired"


def test_expiry_does_not_fire_for_already_triggered_ticket():
    # Triggered tickets are terminal — even past the expiry horizon they
    # pass through unchanged.
    old_ticket = _ticket(created_at="2026-04-18T06:30:00Z", status="triggered")
    out = evaluate_ticket(old_ticket, _snap())
    assert out["status"] == "triggered"


# ---------------------------------------------------------------- terminal passthrough
def test_terminal_ticket_is_not_re_evaluated():
    ticket = _ticket(status="triggered", resolved_at="2026-04-20T10:00:00Z")
    # Put in bars that would fire kill-switch if evaluated.
    snap = _snap(bars_1d=[_bar("2026-04-22T00:00:00+00:00", 80000, 77000, 79000)])
    out = evaluate_ticket(ticket, snap)
    assert out["status"] == "triggered"


# ---------------------------------------------------------------- ledger IO
def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "tickets_btc.jsonl"
    tickets = [_ticket(), _ticket(id="BTC_LONG_XYZ", entry_1=100.0)]
    save_ledger(path, tickets)
    loaded = load_ledger(path)
    assert len(loaded) == 2
    assert loaded[0]["id"] == tickets[0]["id"]
    assert loaded[1]["entry_1"] == 100.0


def test_load_ledger_missing_file_returns_empty(tmp_path: Path):
    assert load_ledger(tmp_path / "nope.jsonl") == []


def test_run_ledger_cycle_splits_active_and_resolved(tmp_path: Path):
    asset_root = tmp_path
    path = ledger_path("btc", asset_root)
    save_ledger(path, [
        _ticket(),                                              # stays armed
        _ticket(id="OLD", created_at="2026-04-17T00:00:00Z"),   # will expire
    ])
    active, resolved = run_ledger_cycle(
        "btc", NOW, 77500.0,
        bars_by_tf={"1h": [], "4h": [], "1d": []},
        ledger_root=asset_root,
    )
    assert len(active) == 1
    assert active[0]["status"] == "armed"
    assert len(resolved) == 1
    assert resolved[0]["status"] == "expired"
    assert len(load_ledger(path)) == 2


def test_run_ledger_cycle_triggered_drops_from_active(tmp_path: Path):
    # Key test for the new semantics: a ticket that triggers this run must
    # NOT show up in the next run's active list — it's terminal.
    asset_root = tmp_path
    path = ledger_path("btc", asset_root)
    save_ledger(path, [_ticket()])
    bars = {
        "1h": [_bar("2026-04-22T09:00:00+00:00", 77500, 76800, 77200)],  # triggers
        "4h": [], "1d": [],
    }
    active, resolved = run_ledger_cycle(
        "btc", NOW, 77500.0, bars_by_tf=bars, ledger_root=asset_root,
    )
    assert active == []
    assert len(resolved) == 1
    assert resolved[0]["status"] == "triggered"
    # Next run: ledger still on disk, but the triggered ticket is terminal so
    # it won't appear in active again.
    active2, resolved2 = run_ledger_cycle(
        "btc", NOW, 77500.0, bars_by_tf=bars, ledger_root=asset_root,
    )
    assert active2 == []
    assert resolved2 == []


def test_build_snapshot_filters_bars_before_created_at():
    bars = {
        "1h": [
            _bar("2026-04-22T05:00:00+00:00", 1, 1, 1),  # before created_at
            _bar("2026-04-22T07:00:00+00:00", 2, 2, 2),  # after
        ]
    }
    snap = build_snapshot(NOW, 77500.0, bars, "2026-04-22T06:30:00Z")
    assert len(snap.bars_1h_since_created) == 1
    assert snap.bars_1h_since_created[0]["close"] == 2


def test_append_new_tickets_mints_ids_and_preserves_ledger(tmp_path: Path):
    asset_root = tmp_path
    n = append_new_tickets(
        "btc",
        [
            {"direction": "long", "created_at": "2026-04-22T10:00:00Z"},
            {"direction": "long", "created_at": "2026-04-22T10:00:00Z"},
        ],
        ledger_root=asset_root,
    )
    assert n == 2
    ledger = load_ledger(ledger_path("btc", asset_root))
    ids = [t["id"] for t in ledger]
    assert len(set(ids)) == 2
    assert all(t["status"] == "armed" for t in ledger)
