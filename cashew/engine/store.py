"""SQLite store — immutable forecast versions, reconciliations, mapping rules,
owner assumptions, counterparty payment stats, and lessons (the learning loop's
durable memory)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ForecastVersion

DB_PATH = Path(os.getenv("CASHEW_ENGINE_DB",
                         str(Path(__file__).resolve().parent.parent / "engine.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    version_id TEXT PRIMARY KEY,
    org TEXT NOT NULL, month TEXT NOT NULL, anchor TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_forecasts_org_month ON forecasts(org, month);
CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL, month TEXT NOT NULL, as_of TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_recon_org_month ON reconciliations(org, month);
CREATE TABLE IF NOT EXISTS mapping_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL, pattern TEXT NOT NULL, category TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL,
    UNIQUE(org, pattern)
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL, category TEXT NOT NULL, counterparty TEXT DEFAULT '',
    amount REAL NOT NULL, date TEXT NOT NULL, note TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counterparty_stats (
    org TEXT NOT NULL, counterparty TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0, mean_lateness_days REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org, counterparty)
);
CREATE TABLE IF NOT EXISTS lateness_obs (
    org TEXT NOT NULL, counterparty TEXT NOT NULL, month TEXT NOT NULL,
    delta_days REAL NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (org, counterparty, month)
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL, month TEXT NOT NULL, text TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lessons_key ON lessons(org, month, key);
CREATE TABLE IF NOT EXISTS settings (
    org TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (org, key)
);
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    counterparty TEXT DEFAULT '',
    method TEXT NOT NULL,            -- recurring_fixed|recurring_variable|one_off|linked
    params TEXT NOT NULL,            -- JSON
    start_date TEXT, end_date TEXT,  -- piecewise window (NULL = open)
    source TEXT NOT NULL DEFAULT 'detected',   -- detected|owner|agent
    locked INTEGER NOT NULL DEFAULT 0,         -- owner-locked: sync won't touch
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(org, name)
);
CREATE TABLE IF NOT EXISTS line_item_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL, item_id INTEGER NOT NULL,
    action TEXT NOT NULL,            -- create|update|end|deactivate|undo|sync
    before TEXT, after TEXT,         -- JSON snapshots
    note TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenarios (
    org TEXT NOT NULL, name TEXT NOT NULL,
    overlay TEXT NOT NULL,           -- JSON {scale:{cat:f}, extra:[...], drop:[item names]}
    created_at TEXT NOT NULL,
    PRIMARY KEY (org, name)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        # resolved at call time (not def time) so tests can point
        # CASHEW_ENGINE_DB / engine.store.DB_PATH at a temp file
        path = path or Path(os.getenv("CASHEW_ENGINE_DB", str(DB_PATH)))
        self.conn = sqlite3.connect(str(path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        # gateway agent + CLI can hit this DB concurrently
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- forecasts -----------------------------------------------------------
    def save_forecast(self, fv: ForecastVersion) -> str:
        import uuid
        fv.version_id = f"{fv.org}:{fv.month}:{_now()}:{uuid.uuid4().hex[:6]}"
        self.conn.execute(
            "INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?,?,?)",
            (fv.version_id, fv.org, fv.month, fv.anchor, _now(),
             json.dumps(fv.to_dict())))
        self.conn.commit()
        return fv.version_id

    def latest_forecast(self, org: str, month: str) -> ForecastVersion | None:
        row = self.conn.execute(
            "SELECT payload FROM forecasts WHERE org=? AND month=? "
            "ORDER BY created_at DESC LIMIT 1", (org, month)).fetchone()
        return ForecastVersion.from_dict(json.loads(row["payload"])) if row else None

    # --- reconciliations ------------------------------------------------------
    def save_reconciliation(self, org: str, month: str, as_of: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO reconciliations(org, month, as_of, created_at, payload) "
            "VALUES (?,?,?,?,?)",
            (org, month, as_of, _now(), json.dumps(payload)))
        self.conn.commit()

    # --- mapping rules ----------------------------------------------------------
    def add_rule(self, org: str, pattern: str, category: str, origin: str = "owner") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO mapping_rules(org, pattern, category, origin, created_at) "
            "VALUES (?,?,?,?,?)", (org, pattern, category, origin, _now()))
        self.conn.commit()

    def rules(self, org: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT pattern, category, origin FROM mapping_rules WHERE org=?", (org,))]

    # --- assumptions -------------------------------------------------------------
    def add_assumption(self, org: str, category: str, amount: float, date_: str,
                       note: str, counterparty: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO assumptions(org, category, counterparty, amount, date, note, "
            "active, created_at) VALUES (?,?,?,?,?,?,1,?)",
            (org, category, counterparty, amount, date_, note, _now()))
        self.conn.commit()
        return cur.lastrowid

    def deactivate_assumption(self, org: str, aid: int) -> bool:
        cur = self.conn.execute(
            "UPDATE assumptions SET active=0 WHERE org=? AND id=?", (org, aid))
        self.conn.commit()
        return cur.rowcount > 0

    def assumptions(self, org: str, month: str | None = None) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, category, counterparty, amount, date, note FROM assumptions "
            "WHERE org=? AND active=1 ORDER BY date", (org,))]
        return [r for r in rows if month is None or r["date"][:7] == month]

    # --- counterparty payment stats (DSO learning) --------------------------------
    def update_lateness(self, org: str, observations: list[dict],
                        month: str = "") -> None:
        """IDEMPOTENT: one observation per (counterparty, month) — re-running
        a reconcile refreshes the same row instead of double-counting."""
        for ob in observations:
            self.conn.execute(
                "INSERT INTO lateness_obs(org, counterparty, month, delta_days, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(org, counterparty, month) "
                "DO UPDATE SET delta_days=excluded.delta_days, updated_at=excluded.updated_at",
                (org, ob["counterparty"], ob.get("month", month) or month,
                 ob["delta_days"], _now()))
        self.conn.commit()

    def lateness(self, org: str) -> dict[str, float]:
        return {r["counterparty"]: r["avg_d"] for r in self.conn.execute(
            "SELECT counterparty, AVG(delta_days) AS avg_d FROM lateness_obs "
            "WHERE org=? GROUP BY counterparty", (org,))}

    # --- lessons ---------------------------------------------------------------------
    def add_lessons(self, org: str, month: str, lessons: list) -> None:
        """Upsert by (org, month, key): a re-run refreshes the same lesson with
        the latest figures instead of stacking near-duplicates."""
        for l in lessons:
            if isinstance(l, dict):
                key, text = l.get("key") or l["text"][:60], l["text"]
            else:
                key, text = str(l)[:60], str(l)
            self.conn.execute(
                "INSERT INTO lessons(org, month, text, key, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(org, month, key) DO UPDATE SET text=excluded.text, "
                "created_at=excluded.created_at",
                (org, month, text, key, _now()))
        self.conn.commit()

    # --- org settings (e.g. the owner's cash floor) -------------------------------
    def set_setting(self, org: str, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)",
                          (org, key, str(value)))
        self.conn.commit()

    def get_setting(self, org: str, key: str, default=None):
        r = self.conn.execute("SELECT value FROM settings WHERE org=? AND key=?",
                              (org, key)).fetchone()
        return r["value"] if r else default

    def lessons(self, org: str, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT month, text, created_at FROM lessons WHERE org=? "
            "ORDER BY id DESC LIMIT ?", (org, limit))]

    # --- line-item config (the editable forecast) ---------------------------------
    def _item_row(self, r) -> dict:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        return d

    def items(self, org: str, include_inactive: bool = False) -> list[dict]:
        q = "SELECT * FROM line_items WHERE org=?"
        if not include_inactive:
            q += " AND active=1"
        return [self._item_row(r) for r in self.conn.execute(q + " ORDER BY category, name", (org,))]

    def get_item(self, org: str, ident: str | int) -> dict | None:
        if isinstance(ident, int) or str(ident).isdigit():
            r = self.conn.execute("SELECT * FROM line_items WHERE org=? AND id=?",
                                  (org, int(ident))).fetchone()
        else:
            r = self.conn.execute("SELECT * FROM line_items WHERE org=? AND name=?",
                                  (org, ident)).fetchone()
        return self._item_row(r) if r else None

    def upsert_item(self, org: str, name: str, category: str, method: str,
                    params: dict, counterparty: str = "", start_date: str | None = None,
                    end_date: str | None = None, source: str = "detected",
                    locked: int = 0, note: str = "") -> int:
        existing = self.get_item(org, name)
        pj = json.dumps(params)
        if existing:
            if existing["locked"] and source == "detected":
                return existing["id"]           # sync never touches owner-locked items
            before = json.dumps(existing)
            self.conn.execute(
                "UPDATE line_items SET category=?, counterparty=?, method=?, params=?, "
                "start_date=?, end_date=?, source=?, locked=?, active=1, updated_at=? "
                "WHERE org=? AND name=?",
                (category, counterparty, method, pj, start_date, end_date,
                 source, locked, _now(), org, name))
            self._log(org, existing["id"], "update" if source != "detected" else "sync",
                      before, json.dumps(self.get_item(org, name)), note)
            self.conn.commit()
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO line_items(org, name, category, counterparty, method, params, "
            "start_date, end_date, source, locked, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (org, name, category, counterparty, method, pj, start_date, end_date,
             source, locked, _now(), _now()))
        self._log(org, cur.lastrowid, "create", None,
                  json.dumps(self.get_item(org, name)), note)
        self.conn.commit()
        return cur.lastrowid

    def end_item(self, org: str, ident, end_date: str, note: str = "") -> bool:
        it = self.get_item(org, ident)
        if not it:
            return False
        before = json.dumps(it)
        self.conn.execute("UPDATE line_items SET end_date=?, locked=1, updated_at=? "
                          "WHERE org=? AND id=?", (end_date, _now(), org, it["id"]))
        self._log(org, it["id"], "end", before, json.dumps(self.get_item(org, it["id"])), note)
        self.conn.commit()
        return True

    def deactivate_item(self, org: str, ident, note: str = "") -> bool:
        it = self.get_item(org, ident)
        if not it:
            return False
        self.conn.execute("UPDATE line_items SET active=0, locked=1, updated_at=? "
                          "WHERE org=? AND id=?", (_now(), org, it["id"]))
        self._log(org, it["id"], "deactivate", json.dumps(it), None, note)
        self.conn.commit()
        return True

    def _log(self, org: str, item_id: int, action: str, before, after, note: str = ""):
        self.conn.execute(
            "INSERT INTO line_item_history(org, item_id, action, before, after, note, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (org, item_id, action, before, after, note, _now()))

    def undo_last(self, org: str) -> str:
        """Revert the most recent owner/agent edit (create/update/end/deactivate)."""
        r = self.conn.execute(
            "SELECT * FROM line_item_history WHERE org=? AND action IN "
            "('create','update','end','deactivate','recategorize') "
            "AND (note IS NULL OR note NOT LIKE 'sync as of%') "
            "ORDER BY id DESC LIMIT 1",
            (org,)).fetchone()
        if not r:
            return "Nothing to undo."
        if r["action"] == "create":
            self.conn.execute("DELETE FROM line_items WHERE org=? AND id=?",
                              (org, r["item_id"]))
            msg = "Removed the item created by the last edit."
        else:
            b = json.loads(r["before"])
            self.conn.execute(
                "UPDATE line_items SET category=?, counterparty=?, method=?, params=?, "
                "start_date=?, end_date=?, source=?, locked=?, active=?, updated_at=? "
                "WHERE org=? AND id=?",
                (b["category"], b["counterparty"], b["method"], json.dumps(b["params"]),
                 b["start_date"], b["end_date"], b["source"], b["locked"], b["active"],
                 _now(), org, r["item_id"]))
            msg = f"Reverted last {r['action']} on '{b['name']}'."
        self._log(org, r["item_id"], "undo", None, None, f"undid history #{r['id']}")
        self.conn.commit()
        return msg

    def recategorize_item(self, org: str, ident, new_category: str,
                          new_name: str, note: str = "") -> str:
        """Atomically move a line item to a new category (owner recategorized
        the counterparty). Lock and history are preserved; if an item already
        exists under the target name the source is merged into it (deactivated
        with a note) rather than double-counting the same cash."""
        it = self.get_item(org, ident)
        if not it:
            return "not found"
        target = self.get_item(org, new_name)
        if target and target["id"] != it["id"]:
            self.conn.execute("UPDATE line_items SET active=0, updated_at=? "
                              "WHERE org=? AND id=?", (_now(), org, it["id"]))
            self._log(org, it["id"], "recategorize", json.dumps(it), None,
                      f"merged into #{target['id']} '{new_name}' — {note}")
            self.conn.commit()
            return f"merged into existing '{new_name}'"
        before = json.dumps(it)
        self.conn.execute(
            "UPDATE line_items SET name=?, category=?, updated_at=? "
            "WHERE org=? AND id=?", (new_name, new_category, _now(), org, it["id"]))
        self._log(org, it["id"], "recategorize", before,
                  json.dumps(self.get_item(org, it["id"])), note)
        self.conn.commit()
        return f"moved to {new_category}"

    def changes_since(self, org: str, since_iso: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT h.created_at, h.action, h.note, li.name FROM line_item_history h "
            "LEFT JOIN line_items li ON li.id = h.item_id AND li.org = h.org "
            "WHERE h.org=? AND h.created_at >= ? ORDER BY h.id", (org, since_iso))]

    # --- scenarios --------------------------------------------------------------------
    def save_scenario(self, org: str, name: str, overlay: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO scenarios VALUES (?,?,?,?)",
                          (org, name, json.dumps(overlay), _now()))
        self.conn.commit()

    def scenario(self, org: str, name: str) -> dict | None:
        r = self.conn.execute("SELECT overlay FROM scenarios WHERE org=? AND name=?",
                              (org, name)).fetchone()
        return json.loads(r["overlay"]) if r else None

    def scenarios_list(self, org: str) -> list[str]:
        return [r["name"] for r in self.conn.execute(
            "SELECT name FROM scenarios WHERE org=? ORDER BY name", (org,))]

    def close(self) -> None:
        self.conn.close()
