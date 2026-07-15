"""Central configuration for the Cashew mock/simulation harness.

Everything is overridable via environment variables so the same image can be
pointed at a different org, anchor, or seed without code changes.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent            # /home/hermes/cashew
DATA_ROOT = Path(os.getenv("CASHEW_DATA_ROOT", str(ROOT / "data" / "ailabgemini")))
STATE_FILE = Path(os.getenv("CASHEW_STATE_FILE", str(ROOT / ".sim_state.json")))

# --- Simulation defaults -----------------------------------------------------
DEFAULT_ORG = os.getenv("CASHEW_ORG", "jam-scn-1")

# Forecast anchor = end of the month *before* the scenario month. The engine
# forecasts the scenario month standing at the anchor, then the clock advances
# day-by-day through the scenario month revealing actuals.
ANCHOR = date.fromisoformat(os.getenv("CASHEW_ANCHOR", "2026-06-30"))
SCENARIO_MONTH = os.getenv("CASHEW_SCENARIO_MONTH", "2026-07")   # YYYY-MM

# Deterministic synthesis (AR/AP dates, payment lateness). Same seed => same data.
SEED = int(os.getenv("CASHEW_SEED", "42"))

# Realism knobs -----------------------------------------------------------
# Leak ground-truth categories into the open-banking feed? A real bank feed
# has no such labels — keep OFF so the engine must do its own mapping.
# Truth stays available for scoring via /sim/truth/labels.
LEAK_LABELS = os.getenv("CASHEW_LEAK_LABELS", "0") == "1"

# Xero reconciliation lag: bookkeeping runs behind the bank. Xero rows only
# become visible `XERO_LAG_DAYS` after their bank date, so the newest
# transactions are always unmapped-by-GL — keeping the mapping problem real.
XERO_LAG_DAYS = int(os.getenv("CASHEW_XERO_LAG_DAYS", "3"))

# Synthetic continuation: the source CSVs end at the scenario shocks (Jul 1).
# The loader extends each org's recurring activity (per-counterparty cadence,
# seeded/deterministic) from the day after the last real transaction through
# EXTEND_UNTIL, so daily reveal and mid-month reconciliation stay meaningful.
EXTEND = os.getenv("CASHEW_EXTEND", "1") == "1"
EXTEND_UNTIL = date.fromisoformat(os.getenv("CASHEW_EXTEND_UNTIL", "2026-08-31"))

# Pending settlement lag: transactions dated within the last N days of the
# clock also appear on /transactions/pending (auth'd, not yet settled).
PENDING_DAYS = int(os.getenv("CASHEW_PENDING_DAYS", "1"))

CURRENCY = "GBP"

# Provider identity surfaced through the mocks (data is Monzo via TrueLayer).
OPENBANKING_PROVIDER = "monzo"
OPENBANKING_PROVIDER_NAME = "Monzo"
