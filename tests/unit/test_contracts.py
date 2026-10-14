"""Tests for kt.unit.contracts (DESIGN.md 7)."""
import dataclasses

import pytest

from kt.unit import contracts as c
from kt.unit.contracts import (
    AddonKind,
    AddonManifest,
    Bar,
    CandidateEvent,
    ContractError,
    GateResult,
    Quote,
    Signal,
    TradeIntent,
)


def make_intent(**kw):
    base = dict(intent_id="01J", origin="STRATEGY", source_id="s1",
                code="005930", action="ENTRY_LONG")
    base.update(kw)
    return TradeIntent(**base)


# ---------------------------------------------------------------- module

def test_all_exports_exist():
    missing = [name for name in c.__all__ if not hasattr(c, name)]
    assert missing == []
    assert len(c.__all__) == len(set(c.__all__))


def test_every_origin_has_action_rules():
    assert set(c._ORIGIN_ACTIONS) == set(c.ORIGINS)


# ---------------------------------------------------------------- AddonManifest

def test_manifest_valid():
    m = AddonManifest(id="filters.min_trading_value", kind=AddonKind.FILTER,
                      version="1.0.0", api=c.API_VERSION)
    assert m.compute == "inline"
    assert m.timeout_ms == 200


@pytest.mark.parametrize("kw", [
    {"id": "single"},
    {"id": "Filters.x"},
    {"kind": "filter"},
    {"version": "1.0"},
    {"api": 0},
    {"compute": "thread"},
    {"timeout_ms": 0},
])
def test_manifest_invalid(kw):
    base = dict(id="filters.x", kind=AddonKind.FILTER, version="1.0.0", api=1)
    base.update(kw)
    with pytest.raises(ContractError):
        AddonManifest(**base)


def test_dataclasses_are_frozen():
    intent = make_intent()
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.qty = 5


# ---------------------------------------------------------------- Bar

def test_bar_valid():
    b = Bar(ts=0, o=100, h=110, l=90, c=105, v=10, amount=1050, confirmed=True)
    assert b.seq == 0
    assert b.amount_estimated is False


@pytest.mark.parametrize("kw", [
    {"h": 99},
    {"l": 101},
    {"v": -1},
    {"amount": -1},
    {"ts": -1},
    {"seq": -1},
])
def test_bar_invalid(kw):
    base = dict(ts=0, o=100, h=110, l=90, c=105, v=10, amount=1050, confirmed=True)
    base.update(kw)
    with pytest.raises(ContractError):
        Bar(**base)


# ---------------------------------------------------------------- Quote

def test_quote_accepts_query_suffix():
    q = Quote(code="005930_NX", ts_ms=0, price=70000, change_pct=0.0, volume=0, amount=0)
    assert q.best_ask is None


@pytest.mark.parametrize("code,price", [
    ("5930", 70000),
    ("005930_XX", 70000),
    ("005930", 0),
])
def test_quote_invalid(code, price):
    with pytest.raises(ContractError):
        Quote(code=code, ts_ms=0, price=price, change_pct=0.0, volume=0, amount=0)


# ---------------------------------------------------------------- CandidateEvent

def test_candidate_valid():
    e = CandidateEvent(source_id="cond:1", code="005930", kind="IN", ts_ms=0)
    assert dict(e.meta) == {}


@pytest.mark.parametrize("code,kind", [
    ("005930_NX", "IN"),
    ("005930", "MOVE"),
])
def test_candidate_invalid(code, kind):
    with pytest.raises(ContractError):
        CandidateEvent(source_id="cond:1", code=code, kind=kind, ts_ms=0)


# ---------------------------------------------------------------- Signal

def test_signal_valid():
    s = Signal(instance_id="i1", code="005930", tf="1m", bar_ts=0, name="long_entry")
    assert tuple(s.orders) == ()


@pytest.mark.parametrize("kw", [
    {"tf": "2m"},
    {"instance_id": ""},
])
def test_signal_invalid(kw):
    base = dict(instance_id="i1", code="005930", tf="1m", bar_ts=0, name="long_entry")
    base.update(kw)
    with pytest.raises(ContractError):
        Signal(**base)


# ---------------------------------------------------------------- TradeIntent

@pytest.mark.parametrize("kw", [
    {},
    {"order_type": "LIMIT", "limit_price": 70000},
    {"order_type": "STOP", "stop_price": 69000},
    {"order_type": "STOP_LIMIT", "limit_price": 69100, "stop_price": 69000},
    {"origin": "PROTECTION", "source_id": "core", "action": "EXIT_LONG"},
    {"origin": "TTL", "action": "CANCEL", "target_order_key": "K1"},
    {"origin": "MANUAL", "action": "EXIT_LONG", "qty": 10},
])
def test_intent_valid(kw):
    make_intent(**kw)


@pytest.mark.parametrize("kw", [
    {"intent_id": ""},
    {"source_id": ""},
    {"origin": "ROBOT"},
    {"action": "ENTRY_SHORT"},
    {"order_type": "IOC"},
    {"code": "005930_NX"},
    {"origin": "PROTECTION", "action": "ENTRY_LONG"},
    {"origin": "FLATTEN", "action": "ENTRY_LONG"},
    {"origin": "TTL", "action": "EXIT_LONG"},
    {"qty": 0},
    {"qty": True},
    {"limit_price": 70000},
    {"order_type": "LIMIT"},
    {"order_type": "STOP", "limit_price": 1, "stop_price": 1},
    {"order_type": "STOP_LIMIT", "stop_price": 1},
    {"action": "CANCEL"},
    {"target_order_key": "K1"},
])
def test_intent_invalid(kw):
    with pytest.raises(ContractError):
        make_intent(**kw)


# ---------------------------------------------------------------- GateResult

@pytest.mark.parametrize("args", [
    (True, "K1", None),
    (False, None, "LOCKED"),
])
def test_gate_result_valid(args):
    GateResult(*args)


@pytest.mark.parametrize("args", [
    (True, "K1", "LOCKED"),
    (False, None, None),
])
def test_gate_result_invalid(args):
    with pytest.raises(ContractError):
        GateResult(*args)
