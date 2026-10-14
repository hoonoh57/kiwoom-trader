"""Tests for kt.unit.isolate (DESIGN.md 3.3 I2, 7.2)."""
import asyncio

import pytest

from kt.unit.contracts import AddonState
from kt.unit.isolate import IsolationPolicy, Isolator


class FakeClock:
    """Controllable clock (seconds), injected into Isolator."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def boom():
    raise RuntimeError("boom")


# ---------------------------------------------------------------- construction

@pytest.mark.parametrize("max_failures,window_sec", [
    (0, 300),
    (True, 300),
    (3, 0),
    (3, -1),
])
def test_policy_invalid(max_failures, window_sec):
    with pytest.raises(ValueError):
        IsolationPolicy(max_failures=max_failures, window_sec=window_sec)


@pytest.mark.parametrize("addon_id,timeout_ms", [
    ("", 200),
    ("filters.x", 0),
    ("filters.x", True),
])
def test_isolator_invalid_args(addon_id, timeout_ms):
    with pytest.raises(ValueError):
        Isolator(addon_id, timeout_ms)


# ---------------------------------------------------------------- inline call

def test_call_success():
    iso = Isolator("filters.x", 200)
    out = iso.call(lambda a, b=0: a + b, 2, b=3)
    assert out.ok and out.value == 5 and out.failure is None
    assert iso.state is AddonState.ACTIVE


def test_exception_returns_default_and_event():
    events = []
    iso = Isolator("filters.x", 200, on_event=lambda k, p: events.append((k, p)))
    out = iso.call(boom, default="fallback")
    assert not out.ok
    assert out.value == "fallback"
    assert out.failure == "exception"
    assert "RuntimeError" in out.error
    kind, payload = events[0]
    assert kind == "ADDON_FAILURE"
    assert payload["addon_id"] == "filters.x"
    assert "trace" in payload


def test_quarantine_after_max_failures():
    events = []
    iso = Isolator("filters.x", 200, on_event=lambda k, p: events.append(k))
    for _ in range(3):
        iso.call(boom)
    assert iso.state is AddonState.QUARANTINED
    assert events == ["ADDON_FAILURE"] * 3 + ["ADDON_QUARANTINE"]
    called = []
    out = iso.call(lambda: called.append(1))
    assert out.failure == "quarantined"
    assert called == []


def test_failures_outside_window_do_not_count():
    clock = FakeClock()
    iso = Isolator("filters.x", 200, policy=IsolationPolicy(3, 300.0), clock=clock)
    clock.t = 0.0
    iso.call(boom)
    clock.t = 100.0
    iso.call(boom)
    clock.t = 401.0
    iso.call(boom)
    assert iso.state is AddonState.ACTIVE
    assert iso.recent_failures() == 1
    assert iso.total_failures == 3


def test_inline_timeout_discards_value():
    clock = FakeClock()
    iso = Isolator("filters.x", 200, clock=clock)

    def slow():
        clock.t += 0.5  # 500ms > 200ms
        return "late"

    out = iso.call(slow, default=None)
    assert not out.ok
    assert out.failure == "timeout"
    assert out.value is None


def test_inline_awaitable_rejected():
    async def coro():
        return 1

    iso = Isolator("filters.x", 200)
    out = iso.call(coro)
    assert out.failure == "exception"
    assert "acall" in out.error


def test_base_exception_propagates():
    def interrupt():
        raise KeyboardInterrupt

    iso = Isolator("filters.x", 200)
    with pytest.raises(KeyboardInterrupt):
        iso.call(interrupt)
    assert iso.total_failures == 0


# ---------------------------------------------------------------- async call

async def test_acall_success():
    async def double(x):
        return x * 2

    iso = Isolator("filters.x", 200)
    out = await iso.acall(double, 21)
    assert out.ok and out.value == 42


async def test_acall_timeout():
    iso = Isolator("filters.x", 20)
    out = await iso.acall(asyncio.sleep, 1, default="d")
    assert out.failure == "timeout"
    assert out.value == "d"


async def test_acall_exception():
    async def bad():
        raise ValueError("bad")

    iso = Isolator("filters.x", 200)
    out = await iso.acall(bad)
    assert out.failure == "exception"
    assert "ValueError" in out.error


async def test_acall_quarantined_skips():
    called = []

    async def fn():
        called.append(1)

    iso = Isolator("filters.x", 200)
    iso.quarantine("manual")
    out = await iso.acall(fn)
    assert out.failure == "quarantined"
    assert called == []


# ---------------------------------------------------------------- external / manual

def test_record_failure_counts_and_rejects_quarantined_kind():
    iso = Isolator("strategies.x", 200)
    for _ in range(3):
        iso.record_failure("timeout", "pool worker timeout", 1500.0)
    assert iso.state is AddonState.QUARANTINED
    with pytest.raises(ValueError):
        iso.record_failure("quarantined", "x")


def test_manual_quarantine_and_reattach_events():
    events = []
    iso = Isolator("filters.x", 200, on_event=lambda k, p: events.append(k))
    assert iso.quarantine("manual") is True
    assert iso.quarantine("again") is False
    assert iso.reattach() is True
    assert iso.state is AddonState.ACTIVE
    assert iso.recent_failures() == 0
    assert events == ["ADDON_QUARANTINE", "ADDON_REATTACH"]


def test_reattach_when_active_returns_false():
    iso = Isolator("filters.x", 200)
    assert iso.reattach() is False


def test_event_sink_error_does_not_break_isolation():
    def bad_sink(kind, payload):
        raise RuntimeError("sink down")

    iso = Isolator("filters.x", 200, on_event=bad_sink)
    out = iso.call(boom)
    assert out.failure == "exception"
    assert iso.total_failures == 1


def test_status_keys():
    iso = Isolator("filters.x", 200)
    iso.call(boom)
    st = iso.status()
    assert st["addon_id"] == "filters.x"
    assert st["state"] == "ACTIVE"
    assert st["recent_failures"] == 1
    assert st["total_failures"] == 1
    assert st["timeout_ms"] == 200
    assert "RuntimeError" in st["last_error"]
