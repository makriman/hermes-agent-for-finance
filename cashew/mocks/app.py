"""Cashew mock harness — FastAPI app.

Three routers under one service (cleanly separable later):
  /openbanking/data/v1   — TrueLayer-shaped Open Banking API
  /xero/api.xro/2.0      — Xero-shaped Accounting API
  /sim                   — virtual-clock control + ground-truth oracle

Run:  uvicorn mocks.app:app --host 0.0.0.0 --port 8900
Docs: http://localhost:8900/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import clock, config, store
from .routers import openbanking, sim, xero


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure state file exists and the active org is warmed on startup.
    store.get_store(clock.get_org())
    yield


app = FastAPI(
    title="Cashew Mock Harness",
    version="1.0.0",
    description="TrueLayer + Xero mock APIs with a shared time-travel clock for "
                "testing Cashew cashflow forecasting against synthetic org data.",
    lifespan=lifespan,
)

app.include_router(openbanking.router)
app.include_router(xero.router)
app.include_router(sim.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "org": clock.get_org(), "now": clock.get_now().isoformat()}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "cashew-mocks",
        "version": "1.0.0",
        "org": clock.get_org(),
        "now": clock.get_now().isoformat(),
        "anchor": config.ANCHOR.isoformat(),
        "endpoints": {
            "openbanking": "/openbanking/data/v1/accounts",
            "xero": "/xero/api.xro/2.0/BankTransactions",
            "sim": "/sim/now",
            "truth": "/sim/truth/expected",
            "docs": "/docs",
        },
    }
