"""Shared request context: the active org's store + the current virtual date."""
from __future__ import annotations

from datetime import date

from . import clock, store
from .models import OrgStore


def current() -> tuple[OrgStore, date]:
    return store.get_store(clock.get_org()), clock.get_now()


def iso_dt(d: date) -> str:
    """ISO timestamp at midnight UTC (provider APIs return timestamps)."""
    return f"{d.isoformat()}T00:00:00Z"
