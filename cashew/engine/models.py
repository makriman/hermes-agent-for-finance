"""Engine data model (v2 — line-item based)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass(frozen=True)
class EnrichedTxn:
    """A bank transaction after Actuals Analysis: joined to the Xero ledger
    (when booked) and classified by the mapping layer."""
    txn_id: str
    date: date
    account: str
    amount: float              # signed: + in, - out
    description: str
    counterparty: str
    gl_code: str = ""          # "" when not yet booked in Xero (lag window)
    gl_name: str = ""
    tax_type: str = ""
    category: str = "unmapped"
    map_source: str = "unmapped"   # gl | rule | learned | heuristic | owner | unmapped


@dataclass
class LineItem:
    """A recurring cash line detected from actuals — the canonical forecast unit."""
    key: str                   # "category|counterparty_norm"
    category: str
    label: str
    counterparty: str
    kind: str                  # regular_monthly | variable_monthly | residual
    day_of_month: int          # anchor day for projection
    monthly_amount: float      # signed projected amount per month
    n_observed: int
    cv: float                  # amount coefficient of variation
    confidence: float          # 0..1
    basis: str


@dataclass
class Occurrence:
    """One dated, expected cash event inside the forecast month."""
    occ_id: str
    source: str                # lineitem | invoice_ar | invoice_ap | vat | assumption
    category: str
    label: str
    counterparty: str
    expected_date: str         # ISO
    amount: float              # signed
    confidence: float


@dataclass
class ForecastVersion:
    org: str
    month: str
    anchor: str
    created_at: str
    params: dict
    opening_balance: float
    operating_balance: float
    vat_pot: float
    occurrences: list[Occurrence]
    projected_net: float
    forecast_close: float
    low_point: dict            # {date, balance}
    weekly: list[dict]         # [{week, from, to, net, close}]
    vat: dict
    excluded: list[dict]       # watch-list: lumpy categories not forecast
    version_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ForecastVersion":
        occs = [Occurrence(**o) for o in d.get("occurrences", [])]
        return ForecastVersion(**{**d, "occurrences": occs})
