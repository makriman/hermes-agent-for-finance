"""Mapping layer — Actuals + Context -> categories (deterministic).

Priority order per transaction:
  1. owner rules   (DB, set via `cashew map add` — the LLM/owner approval loop)
  2. GL booking    (bank<->Xero join; GL account name -> category)
  3. learned rules (counterparty -> category, learned from this org's own
                    booked history — covers the Xero reconciliation-lag window)
  4. heuristics    (small, generic keyword fallbacks, e.g. HMRC -> tax)
  5. unmapped      (queued for the owner/agent to map)

No LLM in this module: the agent may *propose* an owner rule in chat, but it
lands here as an explicit `map add`, and from then on it is deterministic.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# --- GL account name -> cashew category (generic keyword table) ---------------
_GL_KEYWORDS: list[tuple[str, str]] = [
    ("savings", "transfers_internal"),
    ("transfer", "transfers_internal"),
    ("other revenue", "refund"),
    ("sales", "revenue"),
    ("interest income", "financing_income"),
    ("purchases", "suppliers_cogs"),
    ("cost of goods", "suppliers_cogs"),
    ("advertising", "marketing_advertising"),
    ("marketing", "marketing_advertising"),
    ("audit", "professional_services"),
    ("accountancy", "professional_services"),
    ("legal", "professional_services"),
    ("consulting", "consulting_fees"),
    ("entertainment", "meals_entertainment"),
    ("insurance", "insurance"),
    ("interest paid", "loan_repayment"),
    ("loan", "loan_repayment"),
    ("printing", "office_supplies"),
    ("stationery", "office_supplies"),
    ("office equipment", "capital_expenditure"),
    ("equipment", "capital_expenditure"),
    ("software", "subscription_saas"),
    ("subscription", "subscription_saas"),
    ("rent", "rent"),
    ("repairs", "repairs_maintenance"),
    ("maintenance", "repairs_maintenance"),
    ("salaries", "payroll"),
    ("wages", "payroll"),
    ("remuneration", "directors_drawings"),
    ("dividend", "directors_drawings"),
    ("drawings", "directors_drawings"),
    ("pension", "pension"),
    ("travel", "travel"),
    ("corporation tax", "tax_corp"),
    ("vat", "tax_vat"),
    ("paye", "tax_paye"),
    ("nic", "tax_paye"),
    ("utilities", "utilities"),
]

# --- generic counterparty heuristics (last resort before unmapped) ------------
# Specific HMRC taxes FIRST — a corp-tax or PAYE payment misrouted into
# tax_vat would poison the VAT module's payment history.
_CP_HEURISTICS: list[tuple[str, str]] = [
    (r"corporation tax|corp(?:\.| )tax|\bhmrc ct\b", "tax_corp"),
    (r"\bpaye\b|\bnics?\b|national insurance", "tax_paye"),
    (r"\bhmrc\b.*vat|\bvat\b.*hmrc|^hmrc vat$", "tax_vat"),
    (r"\bhmrc\b", "tax_vat"),
    (r"dividend|drawings", "directors_drawings"),
    (r"payroll", "payroll"),
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def gl_to_category(gl_name: str) -> str | None:
    n = norm(gl_name)
    if not n:
        return None
    for kw, cat in _GL_KEYWORDS:
        if kw in n:
            return cat
    return None


def heuristic_category(counterparty: str, description: str) -> str | None:
    hay = f"{norm(counterparty)} {norm(description)}"
    for pat, cat in _CP_HEURISTICS:
        if re.search(pat, hay):
            return cat
    return None


def learn_rules(booked: list[tuple[str, str]]) -> dict[str, str]:
    """counterparty -> category, learned from this org's own booked history.

    A rule is kept only when one category clearly dominates for the
    counterparty (>=80% of >=2 observations) — ambiguous ones stay unlearned."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for counterparty, category in booked:
        cp = norm(counterparty)
        if cp:
            votes[cp][category] += 1
    rules: dict[str, str] = {}
    for cp, counter in votes.items():
        cat, hits = counter.most_common(1)[0]
        total = sum(counter.values())
        if total >= 2 and hits / total >= 0.8:
            rules[cp] = cat
    return rules


class Mapper:
    """Classifies transactions using the 5-layer priority order."""

    def __init__(self, owner_rules: list[dict] | None = None):
        # owner rules: [{pattern, category}] — regex or plain substring on
        # "counterparty description"
        self.owner_rules = owner_rules or []
        self.learned: dict[str, str] = {}

    def learn_from(self, booked: list[tuple[str, str]]) -> None:
        self.learned = learn_rules(booked)

    def _owner_match(self, counterparty: str, description: str) -> str | None:
        hay = f"{norm(counterparty)} {norm(description)}"
        for r in self.owner_rules:
            pat = norm(r["pattern"])
            try:
                if re.search(pat, hay):
                    return r["category"]
            except re.error:
                if pat in hay:
                    return r["category"]
        return None

    def classify(self, counterparty: str, description: str,
                 gl_name: str = "") -> tuple[str, str]:
        """Return (category, source)."""
        cat = self._owner_match(counterparty, description)
        if cat:
            return cat, "owner"
        cat = gl_to_category(gl_name)
        if cat:
            return cat, "gl"
        cat = self.learned.get(norm(counterparty))
        if cat:
            return cat, "learned"
        cat = heuristic_category(counterparty, description)
        if cat:
            return cat, "heuristic"
        return "unmapped", "unmapped"
