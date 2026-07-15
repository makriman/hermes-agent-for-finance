import pytest
import requests
from fastapi.testclient import TestClient

from mocks import clock, config, store

MOCK_BASE = "http://127.0.0.1:8900"


@pytest.fixture(scope="session", autouse=True)
def live_mock_server():
    """The engine + CLI tests drive the LIVE mock server on :8900.

    If it is down, fail the run LOUDLY up front instead of silently skipping
    half the suite (the in-process TestClient tests would still pass and give
    a false green). Start it with ./run.sh or docker compose up.
    """
    try:
        r = requests.get(f"{MOCK_BASE}/sim/now", timeout=3)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — any failure means "not reachable"
        pytest.exit(
            f"mock server on :8900 is unreachable ({e.__class__.__name__}: {e}) "
            f"— start it first (./run.sh or docker compose up). Refusing to "
            f"silently skip the engine/CLI tests.",
            returncode=4,
        )
    yield MOCK_BASE
    # leave the shared dev server in its canonical state
    try:
        requests.post(f"{MOCK_BASE}/sim/reset", json={}, timeout=5)
        requests.post(f"{MOCK_BASE}/sim/org", json={"slug": "jam-scn-1"}, timeout=5)
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        pass


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Each test gets its own clock state file and a clean store cache."""
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    store.clear_cache()
    clock.set_org("jam-scn-1")
    clock.reset()
    yield


@pytest.fixture
def client():
    from mocks.app import app
    return TestClient(app)
