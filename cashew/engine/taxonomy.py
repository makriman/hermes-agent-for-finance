"""Category taxonomy: how each Cashew category is treated in the forecast.

`include_in_forecast` is the key switch — the forecast projects the reliable
*operating* cash engine (revenue, costs, payroll, tax, regular loan repayments)
and deliberately excludes lumpy/discretionary items (owner drawings, capex),
volatile financing, and internal transfers. Those excluded items are where
"surprises" show up in reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass

# kinds
OPERATING_IN = "operating_in"
OPERATING_OUT = "operating_out"
TAX = "tax"
OWNER = "owner"
CAPEX = "capex"
FINANCING = "financing"
TRANSFER = "transfer"

# projection methods
RUN_RATE = "run_rate"                    # mean of recent months
FIXED = "fixed_recurring"               # stable recurring amount
SCHEDULED_QUARTERLY = "scheduled_quarterly"   # lumpy periodic (VAT)
LUMPY = "lumpy"                          # unpredictable one-offs -> not forecast
EXCLUDE = "exclude"                      # internal, netted out


@dataclass(frozen=True)
class CatSpec:
    kind: str
    method: str
    include_in_forecast: bool
    label: str


_C: dict[str, CatSpec] = {
    "revenue": CatSpec(OPERATING_IN, RUN_RATE, True, "Revenue"),
    "refund": CatSpec(OPERATING_IN, RUN_RATE, True, "Refunds"),
    "suppliers_cogs": CatSpec(OPERATING_OUT, RUN_RATE, True, "Suppliers / COGS"),
    "payroll": CatSpec(OPERATING_OUT, RUN_RATE, True, "Payroll"),
    "rent": CatSpec(OPERATING_OUT, FIXED, True, "Rent"),
    "subscription_saas": CatSpec(OPERATING_OUT, FIXED, True, "Subscriptions / SaaS"),
    "pension": CatSpec(OPERATING_OUT, FIXED, True, "Pension"),
    "marketing_advertising": CatSpec(OPERATING_OUT, RUN_RATE, True, "Marketing"),
    "professional_services": CatSpec(OPERATING_OUT, RUN_RATE, True, "Professional services"),
    "consulting_fees": CatSpec(OPERATING_OUT, RUN_RATE, True, "Consulting fees"),
    "insurance": CatSpec(OPERATING_OUT, RUN_RATE, True, "Insurance"),
    "office_supplies": CatSpec(OPERATING_OUT, RUN_RATE, True, "Office supplies"),
    "repairs_maintenance": CatSpec(OPERATING_OUT, RUN_RATE, True, "Repairs & maintenance"),
    "travel": CatSpec(OPERATING_OUT, RUN_RATE, True, "Travel"),
    "meals_entertainment": CatSpec(OPERATING_OUT, RUN_RATE, True, "Meals & entertainment"),
    "utilities": CatSpec(OPERATING_OUT, RUN_RATE, True, "Utilities"),
    "tax_vat": CatSpec(TAX, SCHEDULED_QUARTERLY, True, "VAT"),
    "tax_paye": CatSpec(TAX, FIXED, True, "PAYE / NI"),
    "tax_corp": CatSpec(TAX, SCHEDULED_QUARTERLY, True, "Corporation tax"),
    "loan_repayment": CatSpec(FINANCING, FIXED, True, "Loan repayment"),
    "directors_drawings": CatSpec(OWNER, LUMPY, False, "Director drawings / dividends"),
    "directors_contributions": CatSpec(OWNER, LUMPY, False, "Director contributions"),
    "capital_expenditure": CatSpec(CAPEX, LUMPY, False, "Capital expenditure"),
    # Symmetry matters: if loan REPAYMENTS are forecast, financing INFLOWS must
    # be too (invoice-finance businesses live on them). Reconciliation flags a
    # missing drawdown loudly — which is exactly the alarm the owner needs.
    "financing_income": CatSpec(FINANCING, RUN_RATE, True, "Financing / other income"),
    "transfers_internal": CatSpec(TRANSFER, EXCLUDE, False, "Internal transfer"),
}
_DEFAULT = CatSpec(OPERATING_OUT, RUN_RATE, True, "Other")


def spec(category: str) -> CatSpec:
    return _C.get(category, _DEFAULT)


def label(category: str) -> str:
    return spec(category).label


def is_transfer(category: str) -> bool:
    return spec(category).kind == TRANSFER


def in_operating_forecast(category: str) -> bool:
    return spec(category).include_in_forecast
