"""CLI contract tests (v5) — in-process `engine.cli.main(argv)` against the
live mock server on :8900, with a throwaway engine DB per test module.

Contracts under test:
  * every read command exits 0 and prints something useful — no tracebacks
  * verdict-first: 🔴/🟡/🟢 is the FIRST character of outlook/vat/weekly/
    reconcile/forecast, and the verdict appears in status/daily
  * `daily` is read-only; only `daily --advance` moves the sim clock (+1d)
  * mutations (item add/undo) print a before→after impact block and are undoable
  * whatif --item patches change the close vs base
  * scenario save/list/run/compare round-trips
  * map preview never persists a rule; map add does
  * bad input exits 1 with one friendly ⚠ line, never a traceback
  * settings persist; `changes` works without --since; export/import round-trip
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
import requests

from engine.cli import main

BASE = "http://127.0.0.1:8900"
ORG = "jam-scn-1"
ICONS = ("🔴", "🟡", "🟢")


@pytest.fixture(scope="module", autouse=True)
def cli_db(tmp_path_factory):
    """One throwaway engine DB for the module — Store() resolves
    CASHEW_ENGINE_DB at call time, so the env var is enough."""
    db = tmp_path_factory.mktemp("cli-engine") / "engine.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("CASHEW_ENGINE_DB", str(db))
    yield db
    mp.undo()


def _sim_post(path: str, body: dict | None = None) -> dict:
    r = requests.post(f"{BASE}/sim/{path}", json=body or {}, timeout=10)
    r.raise_for_status()
    return r.json()


def _sim_now() -> str:
    r = requests.get(f"{BASE}/sim/now", timeout=10)
    r.raise_for_status()
    return r.json()["now"]


@pytest.fixture(autouse=True)
def sim_clock():
    """Deterministic: pin org + clock before AND after every test."""
    _sim_post("org", {"slug": ORG})
    _sim_post("reset")
    yield
    _sim_post("org", {"slug": ORG})
    _sim_post("reset")


def run(capsys, *argv) -> tuple[int, str]:
    rc = main([str(a) for a in argv])
    return rc, capsys.readouterr().out


# --- (a) read commands are clean; verdict comes first -----------------------------

READ_COMMANDS = [
    ("outlook",),
    ("forecast",),
    ("status",),
    ("reconcile",),
    ("vat",),
    ("tax",),
    ("weekly",),
    ("daily",),
    ("debtors",),
    ("creditors",),
    ("commitments",),
    ("items",),
    ("lineitems",),
    ("lessons",),
    ("changes",),
    ("settings",),
    ("assume", "list"),
    ("scenario", "list"),
    ("map", "list"),
    ("compare", "--a", "2026-05", "--b", "2026-06"),
]


@pytest.mark.parametrize("argv", READ_COMMANDS, ids=lambda a: " ".join(a))
def test_read_command_runs_clean(capsys, argv):
    rc, out = run(capsys, *argv)
    assert rc == 0
    lines = out.splitlines()
    assert lines and lines[0].strip(), f"{argv}: empty first line"
    assert "Traceback" not in out


@pytest.mark.parametrize("cmd", ["outlook", "vat", "weekly", "reconcile", "forecast"])
def test_verdict_icon_is_first_char(capsys, cmd):
    rc, out = run(capsys, cmd)
    assert rc == 0
    assert out[0] in ICONS, f"{cmd} must open with the verdict icon, got: {out[:40]!r}"


@pytest.mark.parametrize("cmd", ["status", "daily"])
def test_verdict_appears_in(capsys, cmd):
    rc, out = run(capsys, cmd)
    assert rc == 0
    assert any(icon in out for icon in ICONS)


# --- (b) daily is read-only; --advance moves the clock ------------------------------

def test_daily_does_not_move_the_clock(capsys):
    before = _sim_now()
    rc, _ = run(capsys, "daily")
    assert rc == 0
    assert _sim_now() == before


def test_daily_advance_moves_clock_one_day(capsys):
    before = _sim_now()
    rc, out = run(capsys, "daily", "--advance")
    assert rc == 0
    expected = (date.fromisoformat(before) + timedelta(days=1)).isoformat()
    assert _sim_now() == expected
    assert expected in out                        # the header shows the new day


# --- (c) item add → impact → listed → undo → reversed impact ------------------------

def test_item_add_impact_list_undo_roundtrip(capsys):
    run(capsys, "items")                          # ensure the config is synced

    rc, out = run(capsys, "item", "add", "--name", "Test kit purchase",
                  "--category", "office_supplies", "--method", "one_off",
                  "--amount", "-25000", "--date", "2026-07-15")
    assert rc == 0
    assert "saved" in out
    assert "Close (" in out or "No material change" in out
    assert "(-£25,000)" in out                    # the before→after delta
    assert "item undo" in out                     # undo hint

    rc, out = run(capsys, "items")
    assert rc == 0 and "Test kit purchase" in out

    rc, out = run(capsys, "item", "undo")
    assert rc == 0
    assert "Removed the item created by the last edit." in out
    assert "(+£25,000)" in out                    # reversed impact block

    rc, out = run(capsys, "items")
    assert rc == 0 and "Test kit purchase" not in out


# --- (d) whatif --item patch -----------------------------------------------------------

def test_whatif_item_patch_changes_close(capsys):
    rc, _ = run(capsys, "item", "add", "--name", "Test rent",
                "--category", "rent", "--method", "recurring_fixed",
                "--amount", "-1000", "--day", "10")
    assert rc == 0
    rc, out = run(capsys, "whatif", "--item", "Test rent:amount=-5000")
    assert rc == 0
    assert "What-if" in out
    m = re.search(r"Close: (-?£[\d,]+) → \*(-?£[\d,]+)\*", out)
    assert m, f"no close diff line in: {out}"
    assert m.group(1) != m.group(2)               # the patch moved the close


# --- (e) scenario save/list/run/compare ---------------------------------------------

def test_scenario_roundtrip(capsys):
    rc, out = run(capsys, "scenario", "save", "downturn", "--scale", "revenue=0.5")
    assert rc == 0 and "saved" in out
    rc, out = run(capsys, "scenario", "save", "hire",
                  "--add", "payroll=-4000@2026-08-28")
    assert rc == 0 and "saved" in out

    rc, out = run(capsys, "scenario", "list")
    assert rc == 0 and "downturn" in out and "hire" in out

    rc, out = run(capsys, "scenario", "run", "downturn")
    assert rc == 0 and "What-if" in out and "Close:" in out

    rc, out = run(capsys, "scenario", "compare", "downturn", "hire")
    assert rc == 0
    assert "Scenario comparison" in out
    assert "downturn: close" in out and "hire: close" in out


# --- (f) map preview vs add ----------------------------------------------------------

def _rule_count(capsys) -> int:
    rc, out = run(capsys, "map", "list")
    assert rc == 0
    return int(re.match(r"(\d+) owner rule", out).group(1))


def test_map_preview_does_not_persist_but_add_does(capsys):
    n0 = _rule_count(capsys)

    rc, out = run(capsys, "map", "preview", "--pattern", "cloudcanvas",
                  "--category", "office_supplies")
    assert rc == 0 and "preview only" in out
    assert _rule_count(capsys) == n0              # nothing persisted

    rc, out = run(capsys, "map", "add", "--pattern", "cloudcanvas",
                  "--category", "office_supplies")
    assert rc == 0 and "Rule saved" in out
    assert _rule_count(capsys) == n0 + 1

    rc, out = run(capsys, "map", "list")
    assert "'cloudcanvas' → office_supplies" in out


# --- (g) bad input: one friendly line, exit 1, no traceback ---------------------------

def test_item_add_bad_params_json_is_friendly(capsys):
    rc, out = run(capsys, "item", "add", "--name", "Bad idea",
                  "--category", "rent", "--method", "recurring_fixed",
                  "--params", "not json")
    assert rc == 1
    assert "⚠" in out
    assert "Traceback" not in out


def test_whatif_bad_add_spec_is_friendly(capsys):
    rc, out = run(capsys, "whatif", "--add", "garbage")
    assert rc == 1
    assert "⚠" in out
    assert "Traceback" not in out


# --- (h) settings persist -------------------------------------------------------------

def test_settings_floor_persists(capsys):
    rc, out = run(capsys, "settings", "--floor", "20000")
    assert rc == 0 and "(updated)" in out
    assert "cash floor: £20,000" in out

    rc, out = run(capsys, "settings")             # no flags: read back
    assert rc == 0
    assert "cash floor: £20,000" in out


# --- (i) changes without --since uses the last-viewed cursor --------------------------

def test_changes_uses_last_viewed_cursor(capsys):
    run(capsys, "outlook")                        # sets the last_viewed_at cursor
    rc, out = run(capsys, "changes")
    assert rc == 0
    assert "changed since" in out or "No forecast changes since" in out
    assert "Traceback" not in out


# --- (j) export → import preview is a no-op -------------------------------------------

def test_export_then_import_preview_noop(capsys, tmp_path):
    out_path = tmp_path / "forecast.xlsx"
    rc, out = run(capsys, "export", "--out", str(out_path))
    assert rc == 0
    assert f"MEDIA:{out_path}" in out
    assert out_path.exists()

    rc, out = run(capsys, "import", str(out_path))
    assert rc == 0
    assert "Import preview" in out
    assert "0 change(s), 0 addition(s), 0 ending(s), 0 removal(s)" in out
    assert "preview only" in out                  # nothing applied
