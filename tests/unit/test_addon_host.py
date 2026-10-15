"""Tests for kt.unit.addon_host (DESIGN.md 3.2, I1, I2, I6, 7)."""
import pytest

from kt.unit.addon_host import AddonHost
from kt.unit.contracts import AddonKind, AddonManifest, AddonState

AID = "filters.demo"


class FakeAddon:
    def __init__(self, manifest, fail_detach=False):
        self.manifest = manifest
        self.fail_detach = fail_detach
        self.attached = None
        self.detached = 0

    def attach(self, ctx, cfg):
        self.attached = (ctx, cfg)

    def detach(self):
        self.detached += 1
        if self.fail_detach:
            raise RuntimeError("detach boom")

    def check(self, code):
        return code == "005930"

    def boom(self):
        raise RuntimeError("boom")


def manifest(aid=AID, api=1, kind=AddonKind.FILTER):
    return AddonManifest(id=aid, kind=kind, version="1.0.0", api=api)


def setup(aid=AID, api=1, fail_detach=False, events=None):
    sink = (lambda *a: events.append(a)) if events is not None else None
    h = AddonHost(on_event=sink)
    created = []
    m = manifest(aid, api)

    def factory():
        inst = FakeAddon(m, fail_detach)
        created.append(inst)
        return inst

    rec = h.register(m, factory)
    return h, rec, created


def saw(events, name):
    return any(name in repr(e) for e in events)


# register
def test_register_returns_record():
    h, rec, _ = setup()
    assert rec is not None
    assert rec.addon_id == AID
    assert rec.origin == "code"
    assert rec.incompatible is False
    assert rec.isolator is not None
    assert h.ids() == [AID]
    assert h.state(AID) is AddonState.DETACHED

def test_register_rejects_non_manifest():
    with pytest.raises(TypeError):
        AddonHost().register({"id": AID}, lambda: None)

def test_register_rejects_non_callable_factory():
    with pytest.raises(TypeError):
        AddonHost().register(manifest(), None)

def test_duplicate_id_rejected_and_recorded():
    h, _, _ = setup()
    assert h.register(manifest(), lambda: None) is None
    assert h.ids() == [AID]
    assert len(h.status()["problems"]) == 1

def test_api_mismatch_is_incompatible():
    events = []
    h, rec, _ = setup(api=2, events=events)
    assert rec.incompatible is True
    assert rec.isolator is None
    assert rec.error
    assert h.state(AID) is AddonState.INCOMPATIBLE
    assert saw(events, "ADDON_INCOMPATIBLE")

def test_incompatible_attach_refused():
    h, _, created = setup(api=2)
    assert h.attach(AID, None, {}) is False
    assert created == []
    assert h.state(AID) is AddonState.INCOMPATIBLE


# attach / invoke / config
def test_attach_and_invoke():
    h, _, created = setup()
    assert h.attach(AID, "ctx", {}) is True
    assert h.state(AID) is AddonState.ACTIVE
    assert created[0].attached[0] == "ctx"
    out = h.invoke(AID, "check", "005930")
    assert out.ok and out.value is True
    assert h.active() == [AID]
    assert h.active(AddonKind.FILTER) == [AID]
    assert h.active(AddonKind.SOURCE) == []

def test_config_preserved_including_unknown_keys():
    h, _, _ = setup()
    h.attach(AID, None, {"x": 1, "unknown": 2})
    assert h.config(AID) == {"x": 1, "unknown": 2}

def test_config_is_copied():
    h, _, created = setup()
    cfg = {"nested": {"a": 1}}
    h.attach(AID, None, cfg)
    cfg["nested"]["a"] = 99
    assert h.config(AID)["nested"]["a"] == 1
    assert created[0].attached[1] is not cfg


# isolation
def test_repeated_failure_quarantines():
    h, _, _ = setup()
    h.attach(AID, None, {})
    kinds = [h.invoke(AID, "boom").failure for _ in range(4)]
    assert kinds == ["exception", "exception", "exception", "quarantined"]
    assert h.state(AID) is AddonState.QUARANTINED

def test_reattach_restores_active_and_config():
    h, _, _ = setup()
    h.attach(AID, None, {"x": 1, "unknown": 2})
    for _ in range(3):
        h.invoke(AID, "boom")
    assert h.reattach(AID) is True
    assert h.state(AID) is AddonState.ACTIVE
    assert h.config(AID) == {"x": 1, "unknown": 2}
    assert h.invoke(AID, "check", "005930").value is True


# detach
def test_detach_not_attached_returns_false():
    h, _, _ = setup()
    assert h.detach(AID) is False

def test_detach_calls_addon_and_emits():
    events = []
    h, _, created = setup(events=events)
    h.attach(AID, None, {})
    assert h.detach(AID) is True
    assert created[0].detached == 1
    assert h.state(AID) is AddonState.DETACHED
    assert saw(events, "ADDON_DETACH")

def test_detach_failure_still_detaches():
    h, rec, _ = setup(fail_detach=True)
    h.attach(AID, None, {})
    assert h.detach(AID) is True
    assert h.state(AID) is AddonState.DETACHED
    assert rec.error

def test_invoke_after_detach_is_blocked():
    h, _, _ = setup()
    h.attach(AID, None, {})
    h.detach(AID)
    out = h.invoke(AID, "check", "005930")
    assert out.ok is False
    assert out.failure == "quarantined"

def test_detach_all():
    h, _, _ = setup()
    m2 = manifest("filters.other")
    h.register(m2, lambda: FakeAddon(m2))
    h.attach(AID, None, {})
    h.attach("filters.other", None, {})
    h.detach_all()
    assert h.active() == []


# G1: zero addons
def test_discover_missing_root_is_zero_addons(tmp_path):
    h = AddonHost()
    assert h.discover(tmp_path / "nope") == []
    assert h.status() == {"addons": [], "problems": []}
