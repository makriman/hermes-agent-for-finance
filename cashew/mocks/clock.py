"""Persisted virtual clock + active org.

State lives in a small JSON file so it survives uvicorn reloads and is shared
between the API process and the sim runner (which drives it over HTTP). A file
lock keeps concurrent writes safe.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import config

try:  # POSIX file locking; degrade gracefully if unavailable
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None


def _default_state() -> dict:
    return {"org": config.DEFAULT_ORG, "now": config.ANCHOR.isoformat()}


def _read() -> dict:
    p: Path = config.STATE_FILE
    if not p.exists():
        st = _default_state()
        _write(st)
        return st
    try:
        return json.loads(p.read_text())
    except Exception:
        st = _default_state()
        _write(st)
        return st


def _write(st: dict) -> None:
    p: Path = config.STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        if fcntl:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(st, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(p)


def get_org() -> str:
    return _read()["org"]


def get_now() -> date:
    return date.fromisoformat(_read()["now"])


def set_now(d: date) -> date:
    st = _read()
    st["now"] = d.isoformat()
    _write(st)
    return d


def advance(days: int = 1) -> date:
    return set_now(get_now() + timedelta(days=days))


def reset() -> date:
    """Reset the clock to the configured forecast anchor."""
    return set_now(config.ANCHOR)


def set_org(slug: str) -> dict:
    """Switch active org and reset the clock to the anchor."""
    st = {"org": slug, "now": config.ANCHOR.isoformat()}
    _write(st)
    return st
