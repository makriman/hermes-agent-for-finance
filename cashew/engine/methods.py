"""The extrapolator — the four PRD forecast methods, applied deterministically.

  recurring_fixed     fixed amount each period on an anchor day
  recurring_variable  trend (least-squares over lookback observations)
  one_off             a single dated amount (recurring_fixed with start==end)
  linked              percentage of another category's computed series

Every line item is piecewise: it only emits inside [start_date, end_date].
"""
from __future__ import annotations

import calendar
from datetime import date


def month_iter(start: date, months: int) -> list[str]:
    """['2026-07', '2026-08', ...] for `months` periods starting at start's month."""
    y, m = start.year, start.month
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def clamp_day(month: str, day: int) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, min(max(int(day), 1), calendar.monthrange(y, m)[1]))


def in_window(d: date, start: str | None, end: str | None) -> bool:
    if start and d < date.fromisoformat(start):
        return False
    if end and d > date.fromisoformat(end):
        return False
    return True


def trend_fit(observations: list[float]) -> tuple[float, float]:
    """Least-squares (intercept, slope) over index 0..n-1. n<2 -> flat mean."""
    n = len(observations)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return observations[0], 0.0
    xs = range(n)
    mean_x = (n - 1) / 2
    mean_y = sum(observations) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, observations)) / denom \
        if denom else 0.0
    intercept = mean_y - slope * mean_x
    return intercept, slope


def project(item: dict, months: list[str],
            observations: list[float] | None = None) -> list[tuple[date, float]]:
    """Occurrences (date, amount) for one NON-linked line item over `months`.

    `observations` = historical per-month totals (oldest first) for
    recurring_variable; ignored otherwise.
    """
    method = item["method"]
    p = item.get("params", {})
    day = int(p.get("day_of_month", 15))
    out: list[tuple[date, float]] = []

    if method == "one_off":
        d = date.fromisoformat(p["date"])
        if d.strftime("%Y-%m") in months and in_window(d, item.get("start_date"),
                                                       item.get("end_date")):
            out.append((d, round(float(p["amount"]), 2)))
        return out

    if method == "recurring_fixed":
        amount = float(p["amount"])
        growth = float(p.get("growth_pct", 0.0)) / 100.0
        for i, ym in enumerate(months):
            d = clamp_day(ym, day)
            if in_window(d, item.get("start_date"), item.get("end_date")):
                out.append((d, round(amount * ((1 + growth) ** i), 2)))
        return out

    if method == "recurring_variable":
        obs_full = observations or p.get("observations") or []
        lookback = int(p.get("lookback_months", 3))
        obs = obs_full[-lookback:]
        # A trend fitted through volatile observations is noise dressed up as
        # insight — one spike month extrapolates into a hockey stick.
        # Volatility is judged on the FULL series (a 3-point window hides it);
        # above cv 0.6 the honest projection is the typical recent month, flat.
        if len(obs_full) >= 3:
            import statistics as _st
            m_ = _st.mean(obs_full)
            cv_ = _st.pstdev(obs_full) / abs(m_) if m_ else 0.0
            if cv_ > 0.6:
                med = _st.median(obs)
                out = []
                for ym in months:
                    d = clamp_day(ym, day)
                    if in_window(d, item.get("start_date"), item.get("end_date")):
                        out.append((d, round(med, 2)))
                return out
        intercept, slope = trend_fit(obs)
        n = len(obs)
        # sanity cap vs runaway trends: 3x the typical observed magnitude
        typical = sum(abs(o) for o in obs) / n if obs else 0.0
        cap = typical * 3 if typical else 1e12
        # a declining expense decays toward zero — it never flips into income
        # (and vice versa for a declining income stream). Direction comes from
        # the NONZERO observations: a thin trailing month must not flip it.
        nonzero = [o for o in obs if o != 0]
        signs = {o > 0 for o in nonzero}
        direction = (1 if nonzero and nonzero[0] > 0 else -1) if len(signs) == 1 else 0
        for i, ym in enumerate(months):
            val = intercept + slope * (n + i)
            val = max(min(val, cap), -cap)
            if direction > 0:
                val = max(val, 0.0)
            elif direction < 0:
                val = min(val, 0.0)
            d = clamp_day(ym, day)
            if in_window(d, item.get("start_date"), item.get("end_date")):
                out.append((d, round(val, 2)))
        return out

    raise ValueError(f"unknown method {method!r} (linked is resolved by the engine)")


def project_linked(item: dict, months: list[str],
                   target_series: dict[str, float]) -> list[tuple[date, float]]:
    """linked: amount per month = pct% of |target category's monthly total|,
    signed by the pct itself (e.g. COGS = -30% of revenue)."""
    p = item.get("params", {})
    pct = float(p["pct"]) / 100.0
    day = int(p.get("day_of_month", 15))
    out: list[tuple[date, float]] = []
    for ym in months:
        base = abs(target_series.get(ym, 0.0))
        d = clamp_day(ym, day)
        if in_window(d, item.get("start_date"), item.get("end_date")):
            out.append((d, round(base * pct, 2)))
    return out
