"""애드온 격리 래퍼 (DESIGN.md 3.3 I2, 7.2).

모든 애드온 호출은 Isolator를 거친다. Isolator가 보장하는 것:
- 애드온 예외가 코어로 전파되지 않는다. Exception만 포착하고,
  KeyboardInterrupt / SystemExit / asyncio.CancelledError는 그대로 전파한다.
- 호출마다 timeout_ms를 적용한다. 비동기 호출은 asyncio.wait_for로 중단한다.
  동기(inline) 호출은 강제 중단이 불가능하므로 소요 시간을 재서 초과하면
  결과를 버리고 실패로 센다(fail-closed).
- window_sec 안에 max_failures회 실패하면 QUARANTINED로 전환하고,
  이후 호출은 실행하지 않는다.
- 격리 해제(reattach)는 수동 command로만 한다.

pool(별도 프로세스) 실행과 워커 종료는 engine/pool.py가 맡는다(M5).
여기서는 그 결과를 record_failure()로 받기만 한다.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping

from kt.unit.contracts import AddonState

__all__ = ["FailureKind", "EventSink", "IsolationPolicy", "CallOutcome", "Isolator"]

log = logging.getLogger(__name__)

FailureKind = Literal["exception", "timeout", "quarantined"]
# 이벤트 수신자: (kind, payload). kind는 ADDON_FAILURE / ADDON_QUARANTINE / ADDON_REATTACH
EventSink = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class IsolationPolicy:
    max_failures: int = 3
    window_sec: float = 300.0

    def __post_init__(self) -> None:
        if not (isinstance(self.max_failures, int) and not isinstance(self.max_failures, bool)
                and self.max_failures >= 1):
            raise ValueError(f"max_failures는 1 이상 정수여야 합니다: {self.max_failures!r}")
        if not (isinstance(self.window_sec, (int, float)) and self.window_sec > 0):
            raise ValueError(f"window_sec는 양수여야 합니다: {self.window_sec!r}")


@dataclass(frozen=True, slots=True)
class CallOutcome:
    ok: bool
    value: Any = None
    failure: FailureKind | None = None
    error: str = ""
    elapsed_ms: float = 0.0


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _trace(exc: BaseException, limit: int = 5) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=limit))


class Isolator:
    """애드온 하나에 대한 격리 상태와 호출 래퍼."""

    def __init__(
        self,
        addon_id: str,
        timeout_ms: int,
        *,
        policy: IsolationPolicy | None = None,
        on_event: EventSink | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not addon_id:
            raise ValueError("addon_id가 비었습니다")
        if not (isinstance(timeout_ms, int) and not isinstance(timeout_ms, bool) and timeout_ms > 0):
            raise ValueError(f"timeout_ms는 양의 정수여야 합니다: {timeout_ms!r}")
        self.addon_id = addon_id
        self.timeout_ms = timeout_ms
        self.policy = policy or IsolationPolicy()
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: deque[float] = deque()
        self._state = AddonState.ACTIVE
        self._last_error = ""
        self._total_failures = 0

    # ------------------------------------------------------------ 상태 조회

    @property
    def state(self) -> AddonState:
        return self._state

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def total_failures(self) -> int:
        return self._total_failures

    def recent_failures(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._failures)

    def status(self) -> dict[str, Any]:
        return {
            "addon_id": self.addon_id,
            "state": self._state.value,
            "recent_failures": self.recent_failures(),
            "total_failures": self._total_failures,
            "last_error": self._last_error,
            "timeout_ms": self.timeout_ms,
        }

    # ------------------------------------------------------------ 호출

    def call(self, fn: Callable[..., Any], /, *args: Any, default: Any = None, **kwargs: Any) -> CallOutcome:
        """동기(inline) 애드온 호출."""
        if self._state is AddonState.QUARANTINED:
            return self._blocked(default)
        start = self._clock()
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 애드온 예외는 모두 격리 대상
            return self._fail("exception", _describe(exc), self._elapsed(start), default, _trace(exc))
        elapsed = self._elapsed(start)
        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()  # 실행되지 않은 코루틴 경고 방지
            return self._fail("exception", "inline call returned an awaitable; use acall()", elapsed, default)
        if elapsed > self.timeout_ms:
            return self._fail("timeout", f"inline call took {elapsed:.1f}ms > {self.timeout_ms}ms",
                              elapsed, default)
        return CallOutcome(True, value, None, "", elapsed)

    async def acall(self, fn: Callable[..., Awaitable[Any]], /, *args: Any,
                    default: Any = None, **kwargs: Any) -> CallOutcome:
        """비동기 애드온 호출. timeout_ms를 넘기면 중단한다."""
        if self._state is AddonState.QUARANTINED:
            return self._blocked(default)
        start = self._clock()
        try:
            value = await asyncio.wait_for(fn(*args, **kwargs), timeout=self.timeout_ms / 1000)
        except TimeoutError:  # Python 3.11+: asyncio.TimeoutError는 TimeoutError와 같다
            elapsed = self._elapsed(start)
            return self._fail("timeout", f"async call exceeded {self.timeout_ms}ms", elapsed, default)
        except Exception as exc:  # noqa: BLE001
            return self._fail("exception", _describe(exc), self._elapsed(start), default, _trace(exc))
        return CallOutcome(True, value, None, "", self._elapsed(start))

    # ------------------------------------------------------------ 외부 보고 / 수동 제어

    def record_failure(self, kind: FailureKind, error: str, elapsed_ms: float = 0.0) -> CallOutcome:
        """pool 등 외부 실행기가 실패를 보고할 때 사용한다."""
        if kind == "quarantined":
            raise ValueError("record_failure에 'quarantined'는 쓸 수 없습니다")
        return self._fail(kind, error, elapsed_ms, None)

    def quarantine(self, reason: str) -> bool:
        """수동 또는 호스트 판단으로 즉시 격리한다. 상태가 바뀌었으면 True."""
        with self._lock:
            if self._state is AddonState.QUARANTINED:
                return False
            self._state = AddonState.QUARANTINED
            self._last_error = reason
        self._emit("ADDON_QUARANTINE", {"addon_id": self.addon_id, "reason": reason,
                                        "failures": self.recent_failures(),
                                        "window_sec": self.policy.window_sec})
        return True

    def reattach(self) -> bool:
        """격리 해제. 실패 기록도 비운다. 상태가 바뀌었으면 True."""
        with self._lock:
            was = self._state
            self._state = AddonState.ACTIVE
            self._failures.clear()
        if was is AddonState.QUARANTINED:
            self._emit("ADDON_REATTACH", {"addon_id": self.addon_id})
            return True
        return False

    # ------------------------------------------------------------ 내부

    def _elapsed(self, start: float) -> float:
        return (self._clock() - start) * 1000.0

    def _prune(self, now: float) -> None:
        window = self.policy.window_sec
        while self._failures and now - self._failures[0] > window:
            self._failures.popleft()

    def _blocked(self, default: Any) -> CallOutcome:
        return CallOutcome(False, default, "quarantined", "addon is quarantined", 0.0)

    def _fail(self, kind: FailureKind, error: str, elapsed_ms: float, default: Any,
              trace: str = "") -> CallOutcome:
        with self._lock:
            now = self._clock()
            self._failures.append(now)
            self._prune(now)
            self._total_failures += 1
            self._last_error = error
            recent = len(self._failures)
            newly_quarantined = (self._state is AddonState.ACTIVE
                                 and recent >= self.policy.max_failures)
            if newly_quarantined:
                self._state = AddonState.QUARANTINED
        payload: dict[str, Any] = {"addon_id": self.addon_id, "failure": kind, "error": error,
                                   "elapsed_ms": round(elapsed_ms, 3), "recent_failures": recent}
        if trace:
            payload["trace"] = trace
        self._emit("ADDON_FAILURE", payload)
        if newly_quarantined:
            self._emit("ADDON_QUARANTINE", {"addon_id": self.addon_id, "reason": error,
                                            "failures": recent,
                                            "window_sec": self.policy.window_sec})
        return CallOutcome(False, default, kind, error, elapsed_ms)

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # noqa: BLE001 - 이벤트 수신자 오류가 격리 동작을 깨면 안 된다
            log.exception("isolate event sink failed: %s %s", kind, self.addon_id)
