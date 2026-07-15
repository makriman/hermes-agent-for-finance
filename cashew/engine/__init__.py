"""Cashew forecasting engine (Layer 1).

Deterministic core: pulls actuals from the provider APIs, detects recurring
line items, projects a cashflow forecast, freezes it as an immutable version,
and reconciles it against actuals with a variance decomposition.

LLMs never touch the numbers — every figure is computed here.
"""
__version__ = "4.0.0"
