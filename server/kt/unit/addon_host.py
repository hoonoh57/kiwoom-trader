"""애드온 호스트 (DESIGN.md 3.2 Manager, 3.3 I1·I2·I6, 7).

- discover(root): root 아래 '<group>/<name>/manifest.py'를 찾아 등록한다.
  manifest.py는 MANIFEST(AddonManifest)와 create() -> Addon을 제공해야 한다.
  root가 없으면 애드온 0개로 끝난다(G1). 로드 실패는 problems에 기록하고 계속한다.
- register(): 코드에서 직접 등록. api 불일치는 INCOMPATIBLE, 중복 id는 거부.
- attach/detach/reattach/configure: 수명주기. create()와 attach()도 Isolator를 거친다.
- invoke/ainvoke: 애드온 메서드 호출의 유일한 경로(I2).
- 설정은 깊은 복사로 보관·전달한다. 알 수 없는 키도 그대로 보존한다(I6).

상태: INCOMPATIBLE > QUARANTINED > DETACHED > ACTIVE 순으로 판정한다.
"""
from __future__ import annotations

import copy
import importlib.util
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from kt.unit.contracts import API_VERSION, Addon, AddonKind, AddonManifest, AddonState
from kt.unit.isolate import CallOutcome, EventSink, IsolationPolicy, Isolator

__all__ = ["MANIFEST_FILE", "AddonFactory", "AddonRecord", "AddonHost"]

log = logging.getLogger(__name__)

MANIFEST_FILE = "manifest.py"
AddonFactory = Callable[[], Addon]

_UNSET: Any = object()                       # 아직 attach된 적 없음
_RESERVED_METHODS = frozenset({"attach", "detach", "manifest"})


@dataclass
class AddonRecord:
    addon_id: str
    manifest: AddonManifest
    factory: AddonFactory
    origin: str                              # "code" 또는 manifest.py 경로
    isolator: Isolator | None = None         # INCOMPATIBLE이면 None
    instance: Addon | None = None
    config: dict[str, Any] = field(default_factory=dict)
    ctx: Any = _UNSET
    error: str = ""
    incompatible: bool = False

    @property
    def state(self) -> AddonState:
        if self.incompatible or self.isolator is None:
            return AddonState.INCOMPATIBLE
        if self.isolator.state is AddonState.QUARANTINED:
            return AddonState.QUARANTINED
        if self.instance is None:
            return AddonState.DETACHED
        return AddonState.ACTIVE


class AddonHost:
    def __init__(
        self,
        *,
        api_version: int = API_VERSION,
        policy: IsolationPolicy | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        self.api_version = api_version
        self.policy = policy or IsolationPolicy()
        self._on_event = on_event
        self._records: dict[str, AddonRecord] = {}
        self._problems: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 발견·등록

    def discover(self, root: str | Path) -> list[str]:
        """root 아래 manifest.py를 찾아 등록한다. 등록된 id 목록을 반환한다."""
        root = Path(root)
        if not root.is_dir():
            return []
        found: list[str] = []
        for path in sorted(root.rglob(MANIFEST_FILE)):
            rel_dirs = path.relative_to(root).parts[:-1]
            if any(p.startswith(("_", ".")) for p in rel_dirs):
                continue                     # __pycache__, _disabled, .hidden 등 제외
            origin = path.as_posix()
            try:
                module = self._load_module(path, root)
                manifest = getattr(module, "MANIFEST", None)
                factory = getattr(module, "create", None)
                if not isinstance(manifest, AddonManifest):
                    raise TypeError("MANIFEST가 AddonManifest가 아닙니다")
                if not callable(factory):
                    raise TypeError("create()가 없습니다")
            except Exception as exc:  # noqa: BLE001 - 애드온 로드 실패가 호스트를 죽이면 안 된다
                self._problem(origin, f"{type(exc).__name__}: {exc}")
                continue
            rec = self.register(manifest, factory, origin=origin)
            if rec is not None:
                found.append(rec.addon_id)
        return found

    def register(self, manifest: AddonManifest, factory: AddonFactory, *,
                 origin: str = "code") -> AddonRecord | None:
        if not isinstance(manifest, AddonManifest):
            raise TypeError("manifest는 AddonManifest여야 합니다")
        if not callable(factory):
            raise TypeError("factory는 호출 가능해야 합니다")
        if manifest.id in self._records:
            self._problem(origin, f"duplicate addon id: {manifest.id}", addon_id=manifest.id)
            return None
        rec = AddonRecord(manifest.id, manifest, factory, origin)
        if manifest.api != self.api_version:
            rec.incompatible = True
            rec.error = f"addon api {manifest.api} != host api {self.api_version}"
            self._emit("ADDON_INCOMPATIBLE", {"addon_id": manifest.id, "error": rec.error})
        else:
            rec.isolator = Isolator(manifest.id, manifest.timeout_ms,
                                    policy=self.policy, on_event=self._on_event)
        self._records[manifest.id] = rec
        return rec

    # ------------------------------------------------------------ 수명주기

    def attach(self, addon_id: str, ctx: Any, config: Mapping[str, Any] | None = None) -> bool:
        rec = self._get(addon_id)
        if rec.state in (AddonState.INCOMPATIBLE, AddonState.QUARANTINED):
            return False
        if rec.instance is not None:
            return True
        iso = rec.isolator
        assert iso is not None
        rec.ctx = ctx
        if config is not None:
            rec.config = copy.deepcopy(dict(config))

        made = iso.call(rec.factory)
        if not made.ok:
            rec.error = made.error
            return False
        inst = made.value
        man = getattr(inst, "manifest", None)
        if not isinstance(man, AddonManifest) or man.id != rec.addon_id:
            rec.error = iso.record_failure(
                "exception", "create()가 반환한 객체의 manifest가 등록 정보와 다릅니다").error
            return False
        attach_fn = getattr(inst, "attach", None)
        if not callable(attach_fn):
            rec.error = iso.record_failure("exception", "attach()가 없습니다").error
            return False
        out = iso.call(attach_fn, ctx, copy.deepcopy(rec.config))
        if not out.ok:
            rec.error = out.error
            return False

        rec.instance = inst
        rec.error = ""
        self._emit("ADDON_ATTACH", {"addon_id": addon_id, "version": rec.manifest.version})
        return True

    def detach(self, addon_id: str) -> bool:
        """붙어 있으면 떼고 True. detach() 실패는 기록만 하고 항상 떼어낸다."""
        rec = self._get(addon_id)
        inst = rec.instance
        if inst is None:
            return False
        rec.instance = None
        detach_fn = getattr(inst, "detach", None)
        if callable(detach_fn):
            iso = rec.isolator
            if iso is not None and iso.state is AddonState.ACTIVE:
                out = iso.call(detach_fn)
                if not out.ok:
                    rec.error = out.error
            else:
                # 격리 상태에서는 Isolator가 호출을 막으므로, 정리 목적의 호출만 직접 보호한다.
                try:
                    detach_fn()
                except Exception as exc:  # noqa: BLE001
                    rec.error = f"{type(exc).__name__}: {exc}"
                    log.warning("addon detach failed: %s %s", addon_id, rec.error)
        self._emit("ADDON_DETACH", {"addon_id": addon_id})
        return True

    def reattach(self, addon_id: str) -> bool:
        """격리 해제 후 마지막 ctx/config로 다시 붙인다. 붙었으면 True."""
        rec = self._get(addon_id)
        if rec.state is AddonState.INCOMPATIBLE:
            return False
        self.detach(addon_id)
        assert rec.isolator is not None
        rec.isolator.reattach()
        if rec.ctx is _UNSET:
            return False
        return self.attach(addon_id, rec.ctx)

    def configure(self, addon_id: str, config: Mapping[str, Any]) -> bool:
        """설정 교체. 붙어 있으면 새 설정으로 다시 붙인다."""
        rec = self._get(addon_id)
        rec.config = copy.deepcopy(dict(config))
        if rec.instance is None:
            return True
        self.detach(addon_id)
        return self.attach(addon_id, rec.ctx)

    def detach_all(self) -> None:
        for addon_id in list(self._records):
            self.detach(addon_id)

    # ------------------------------------------------------------ 호출 (I2)

    def invoke(self, addon_id: str, method: str, /, *args: Any,
               default: Any = None, **kwargs: Any) -> CallOutcome:
        rec, fn, blocked = self._resolve(addon_id, method, default)
        if blocked is not None:
            return blocked
        assert rec.isolator is not None
        return rec.isolator.call(fn, *args, default=default, **kwargs)

    async def ainvoke(self, addon_id: str, method: str, /, *args: Any,
                      default: Any = None, **kwargs: Any) -> CallOutcome:
        rec, fn, blocked = self._resolve(addon_id, method, default)
        if blocked is not None:
            return blocked
        assert rec.isolator is not None
        return await rec.isolator.acall(fn, *args, default=default, **kwargs)

    # ------------------------------------------------------------ 조회

    def state(self, addon_id: str) -> AddonState:
        return self._get(addon_id).state

    def ids(self) -> list[str]:
        return sorted(self._records)

    def active(self, kind: AddonKind | None = None) -> list[str]:
        return sorted(
            rid for rid, rec in self._records.items()
            if rec.state is AddonState.ACTIVE and (kind is None or rec.manifest.kind is kind)
        )

    def config(self, addon_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._get(addon_id).config)

    def status(self) -> dict[str, Any]:
        addons = []
        for rid in sorted(self._records):
            rec = self._records[rid]
            item: dict[str, Any] = {
                "addon_id": rid,
                "kind": rec.manifest.kind.value,
                "version": rec.manifest.version,
                "state": rec.state.value,
                "origin": rec.origin,
                "error": rec.error,
                "config": copy.deepcopy(rec.config),
            }
            if rec.isolator is not None:
                item["recent_failures"] = rec.isolator.recent_failures()
                item["total_failures"] = rec.isolator.total_failures
            addons.append(item)
        return {"addons": addons, "problems": [dict(p) for p in self._problems]}

    # ------------------------------------------------------------ 내부

    def _get(self, addon_id: str) -> AddonRecord:
        try:
            return self._records[addon_id]
        except KeyError:
            raise KeyError(f"unknown addon: {addon_id}") from None

    def _resolve(self, addon_id: str, method: str,
                 default: Any) -> tuple[AddonRecord, Any, CallOutcome | None]:
        if not method or method.startswith("_") or method in _RESERVED_METHODS:
            raise ValueError(f"invoke로 호출할 수 없는 메서드입니다: {method!r}")
        rec = self._get(addon_id)
        st = rec.state
        if st is not AddonState.ACTIVE:
            return rec, None, CallOutcome(False, default, "quarantined",
                                          f"addon not active: {st.value}")
        fn = getattr(rec.instance, method, None)
        if not callable(fn):
            assert rec.isolator is not None
            out = rec.isolator.record_failure("exception", f"method not found: {method}")
            return rec, None, CallOutcome(False, default, out.failure, out.error)
        return rec, fn, None

    @staticmethod
    def _load_module(path: Path, root: Path) -> Any:
        rel = path.relative_to(root).with_suffix("").as_posix()
        name = "kt_addon__" + re.sub(r"\W", "_", rel)
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _problem(self, origin: str, error: str, *, addon_id: str | None = None) -> None:
        item: dict[str, Any] = {"origin": origin, "error": error}
        if addon_id:
            item["addon_id"] = addon_id
        self._problems.append(item)
        log.warning("addon load problem: %s %s", origin, error)
        self._emit("ADDON_LOAD_ERROR", item)

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # noqa: BLE001
            log.exception("addon host event sink failed: %s", kind)
