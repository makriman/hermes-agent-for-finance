"""Registry of the JAM test orgs (JAM Ltd, 3 scenarios).

Scoped to JAM only. Folder + VAT-pot coverage come from the dataset README;
everything else (magnitudes, categories, events) is computed from the CSVs at
load time so the harness never drifts from the actual data.
"""
from __future__ import annotations

ORGS: dict[str, dict] = {
    "jam-scn-1": {"folder": "JAM 1", "name": "JAM Ltd", "business": "JAM",
                  "scenario": 1, "vat_coverage": 0.65},
    "jam-scn-2": {"folder": "JAM 2", "name": "JAM Ltd", "business": "JAM",
                  "scenario": 2, "vat_coverage": 0.80},
    "jam-scn-3": {"folder": "JAM 3", "name": "JAM Ltd", "business": "JAM",
                  "scenario": 3, "vat_coverage": 0.25},
}


def get_org(slug: str) -> dict:
    if slug not in ORGS:
        raise KeyError(f"unknown org slug '{slug}'. Known: {', '.join(ORGS)}")
    return {"slug": slug, **ORGS[slug]}


def all_orgs() -> list[dict]:
    return [{"slug": s, **v} for s, v in ORGS.items()]
