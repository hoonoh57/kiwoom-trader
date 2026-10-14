"""5축 공통 계약 (DESIGN.md 7장).

서버의 모든 계층(core, bridge, engine, addons, manager)이 이 모듈을 import한다.
따라서 이 모듈은 표준 라이브러리만 사용하고, kt 패키지의 다른 모듈을 import하지 않는다.

여기서 하는 검증은 "구조적으로 말이 되는가"까지만이다.
한도, 시간, ARM, 킬 스위치 같은 정책 검사는 central/core/order_gate.py가 맡는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence

__all__ = [
    "API_VERSION", "TIMEFRAMES", "CODE_RE", "QUERY_CODE_RE", "ContractError",
    "AddonKind", "AddonState", "AddonManifest",
    "Bar", "Quote", "CandidateEvent", "FilterResult", "RankScore", "Signal",
    "Position", "AccountSummary", "TradeIntent", "GateResult",
    "ORIGINS", "ACTIONS", "ORDER_TYPES",
    "Clock", "AddonContext", "Addon", "Unit",
    "SourceAddon", "FeatureAddon", "FilterAddon", "RankerAddon",
    "StrategyAddon", "ExitAddon", "SizerAddon", "DatasourceAddon",
]

# 애드온 계약 버전. manifest.api가 이 값과 다르면 addon_host가 attach를 거부한다.
API_VERSION = 1

# 주문용 종목코드: KRX 6자리만 (v1)
CODE_RE = re.compile(r"^[0-9]{6}$")
# 조회용 종목코드: 6자리 + 선택 접미사 _AL(SOR) / _NX(NXT)
QUERY_CODE_RE = re.compile(r"^[0-9]{6}(?:_(?:AL|NX))?$")

TIMEFRAMES: tuple[str, ...] = (
    "tick", "1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d", "1w", "1M",
)

_ADDON_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


class ContractError(ValueError):
    """계약 위반. 생성 시점에 즉시 발생한다(fail-closed)."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ContractError(message)


def _positive_or_none(value: int | None, name: str) -> None:
    if value is not None:
        _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
                 f"{name}은 양의 정수여야 합니다: {value!r}")


# ---------------------------------------------------------------- 애드온 메타

class AddonKind(str, Enum):
    SOURCE = "source"
    FEATURE = "feature"
    FILTER = "filter"
    RANKER = "ranker"
    STRATEGY = "strategy"
    EXIT = "exit"
    SIZER = "sizer"
    DATASOURCE = "datasource"


class AddonState(str, Enum):
    DETACHED = "DETACHED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class AddonManifest:
    id: str                                   # 예: "filters.min_trading_value"
    kind: AddonKind
    version: str                              # semver
    api: int                                  # 계약 버전
    config_schema: Mapping[str, Any] = field(default_factory=dict)
    compute: Literal["inline", "pool"] = "inline"
    timeout_ms: int = 200

    def __post_init__(self) -> None:
        _require(bool(_ADDON_ID_RE.match(self.id)),
                 f"addon id는 'group.name' 소문자 형식이어야 합니다: {self.id!r}")
        _require(isinstance(self.kind, AddonKind), f"알 수 없는 addon kind: {self.kind!r}")
        _require(bool(_SEMVER_RE.match(self.version)), f"version은 semver여야 합니다: {self.version!r}")
        _require(isinstance(self.api, int) and self.api >= 1, f"api는 1 이상 정수여야 합니다: {self.api!r}")
        _require(self.compute in ("inline", "pool"), f"compute는 inline|pool: {self.compute!r}")
        _require(isinstance(self.timeout_ms, int) and self.timeout_ms > 0,
                 f"timeout_ms는 양의 정수여야 합니다: {self.timeout_ms!r}")


# ---------------------------------------------------------------- 시장 데이터

@dataclass(frozen=True, slots=True)
class Bar:
    ts: int            # 봉 시작 시각, epoch 초 (KST 기준으로 정규화된 값)
    o: int
    h: int
    l: int
    c: int
    v: int
    amount: int        # 거래대금(원)
    confirmed: bool
    seq: int = 0       # 틱 봉: 같은 초 안의 순번 (DESIGN 9.11)
    amount_estimated: bool = False   # amount를 c*v로 추정했으면 True

    def __post_init__(self) -> None:
        _require(self.ts >= 0 and self.seq >= 0, f"잘못된 ts/seq: {self.ts}/{self.seq}")
        _require(self.l <= min(self.o, self.c) and self.h >= max(self.o, self.c) and self.l <= self.h,
                 f"OHLC 불일치: o={self.o} h={self.h} l={self.l} c={self.c}")
        _require(self.v >= 0 and self.amount >= 0, "거래량/거래대금은 음수일 수 없습니다")


@dataclass(frozen=True, slots=True)
class Quote:
    code: str
    ts_ms: int
    price: int
    change_pct: float
    volume: int
    amount: int
    best_ask: int | None = None
    best_bid: int | None = None
    upper_limit: int | None = None
    lower_limit: int | None = None

    def __post_init__(self) -> None:
        _require(bool(QUERY_CODE_RE.match(self.code)), f"잘못된 종목코드: {self.code!r}")
        _require(self.price > 0, f"현재가는 양수여야 합니다: {self.price}")


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    source_id: str
    code: str
    kind: Literal["IN", "OUT"]
    ts_ms: int
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(bool(CODE_RE.match(self.code)), f"잘못된 종목코드: {self.code!r}")
        _require(self.kind in ("IN", "OUT"), f"kind는 IN|OUT: {self.kind!r}")


@dataclass(frozen=True, slots=True)
class FilterResult:
    passed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RankScore:
    code: str
    score: float
    parts: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Signal:
    instance_id: str
    code: str
    tf: str
    bar_ts: int
    name: str                                    # "long_entry", "long_exit" 등
    orders: Sequence[Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        _require(bool(self.instance_id), "instance_id가 비었습니다")
        _require(bool(CODE_RE.match(self.code)), f"잘못된 종목코드: {self.code!r}")
        _require(self.tf in TIMEFRAMES, f"지원하지 않는 tf: {self.tf!r}")


# ---------------------------------------------------------------- 계좌 투영

@dataclass(frozen=True, slots=True)
class Position:
    code: str
    qty: int
    avg_price: int
    opened_ts_ms: int
    origin: Literal["SYSTEM", "EXTERNAL"]
    instance_id: str | None = None               # SYSTEM 포지션의 소속 전략 인스턴스
    pending_sell_qty: int = 0


@dataclass(frozen=True, slots=True)
class AccountSummary:
    ts_ms: int
    cash: int                                    # 예수금
    orderable: int                               # 주문가능금액
    total_eval: int                              # 평가금액 합계
    realized_pnl_today: int = 0


# ---------------------------------------------------------------- 주문 의도

ORIGINS = frozenset({"STRATEGY", "EXIT", "PROTECTION", "MANUAL", "FLATTEN", "TTL"})
ACTIONS = frozenset({"ENTRY_LONG", "EXIT_LONG", "CANCEL"})
ORDER_TYPES = frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"})

Origin = Literal["STRATEGY", "EXIT", "PROTECTION", "MANUAL", "FLATTEN", "TTL"]
Action = Literal["ENTRY_LONG", "EXIT_LONG", "CANCEL"]
OrderType = Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]

# origin별로 허용되는 action (보호/정리/TTL 경로가 진입을 만들 수 없게 함)
_ORIGIN_ACTIONS: Mapping[str, frozenset[str]] = {
    "STRATEGY": frozenset({"ENTRY_LONG", "EXIT_LONG", "CANCEL"}),
    "EXIT": frozenset({"EXIT_LONG", "CANCEL"}),
    "PROTECTION": frozenset({"EXIT_LONG", "CANCEL"}),
    "FLATTEN": frozenset({"EXIT_LONG", "CANCEL"}),
    "TTL": frozenset({"CANCEL"}),
    "MANUAL": frozenset({"ENTRY_LONG", "EXIT_LONG", "CANCEL"}),
}


@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str                               # ULID
    origin: Origin
    source_id: str                               # 전략 인스턴스 id / 애드온 id / "core"
    code: str                                    # 6자리, v1은 KRX만
    action: Action
    order_type: OrderType = "MARKET"
    qty: int | None = None                       # None: 진입은 사이저 결정, EXIT는 전량
    limit_price: int | None = None
    stop_price: int | None = None
    target_order_key: str | None = None          # CANCEL 대상
    bar_ts: int | None = None
    reason: str = ""
    dedupe_key: str = ""                         # 비어 있으면 gate가 생성

    def __post_init__(self) -> None:
        _require(bool(self.intent_id), "intent_id가 비었습니다")
        _require(bool(self.source_id), "source_id가 비었습니다")
        _require(self.origin in ORIGINS, f"알 수 없는 origin: {self.origin!r}")
        _require(self.action in ACTIONS, f"알 수 없는 action: {self.action!r}")
        _require(self.order_type in ORDER_TYPES, f"알 수 없는 order_type: {self.order_type!r}")
        _require(bool(CODE_RE.match(self.code)), f"주문 종목코드는 KRX 6자리만 허용: {self.code!r}")
        _require(self.action in _ORIGIN_ACTIONS[self.origin],
                 f"origin {self.origin}은 {self.action}을 만들 수 없습니다")
        _positive_or_none(self.qty, "qty")
        _positive_or_none(self.limit_price, "limit_price")
        _positive_or_none(self.stop_price, "stop_price")

        if self.action == "CANCEL":
            _require(bool(self.target_order_key), "CANCEL에는 target_order_key가 필요합니다")
            return
        _require(self.target_order_key is None, "target_order_key는 CANCEL에서만 사용합니다")

        # 가격 인자 규칙 (dsl 파서 _validate_order_arguments와 동일)
        has_limit = self.limit_price is not None
        has_stop = self.stop_price is not None
        expected = {
            "MARKET": (False, False),
            "LIMIT": (True, False),
            "STOP": (False, True),
            "STOP_LIMIT": (True, True),
        }[self.order_type]
        _require((has_limit, has_stop) == expected,
                 f"{self.order_type} 주문 가격 인자 불일치: limit_price={self.limit_price}, "
                 f"stop_price={self.stop_price}")


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    order_key: str | None
    reject_code: str | None                      # DESIGN 9.5 거부 코드
    detail: str = ""

    def __post_init__(self) -> None:
        if self.accepted:
            _require(self.reject_code is None, "accepted 결과에는 reject_code가 없어야 합니다")
        else:
            _require(bool(self.reject_code), "거부 결과에는 reject_code가 필요합니다")


# ---------------------------------------------------------------- 컨텍스트와 유닛

class Clock(Protocol):
    def now_ms(self) -> int: ...
    def trade_date(self) -> str: ...             # 'YYYYMMDD' KST


class AddonContext(Protocol):
    """애드온에게 주는 읽기 전용 컨텍스트 (I3: 주문·저장·키움 호출 API 없음)."""
    addon_id: str
    clock: Clock

    def bars(self, code: str, tf: str, limit: int | None = None) -> Sequence[Bar]: ...
    def quote(self, code: str) -> Quote | None: ...
    def position(self, code: str) -> Position | None: ...
    def account_summary(self) -> AccountSummary | None: ...
    def log(self, level: str, message: str, **fields: Any) -> None: ...
    def schedule(self, job: Callable[[], Awaitable[None]], interval_sec: float) -> Callable[[], None]: ...
    """주기 작업 등록. 반환값을 호출하면 해제된다. 작업도 isolate 정책을 따른다."""


class Addon(Protocol):
    manifest: AddonManifest

    def attach(self, ctx: AddonContext, config: Mapping[str, Any]) -> None: ...
    def detach(self) -> None: ...


class Unit(Protocol):
    unit_id: str

    def boot(self) -> None: ...                       # 애드온 0개로 성공해야 함
    def snapshot_state(self) -> dict: ...
    def restore_state(self, raw: dict) -> None: ...   # 알 수 없는 키 보존


# ---------------------------------------------------------------- 종류별 애드온 (DESIGN 7.1)

class SourceAddon(Addon, Protocol):
    def start(self, emit: Callable[[CandidateEvent], None]) -> None: ...
    def stop(self) -> None: ...


class FeatureAddon(Addon, Protocol):
    def compute(self, code: str, quote: Quote | None,
                bars: Mapping[str, Sequence[Bar]]) -> Mapping[str, float]: ...


class FilterAddon(Addon, Protocol):
    def check(self, code: str, features: Mapping[str, float]) -> FilterResult: ...


class RankerAddon(Addon, Protocol):
    def score(self, codes: Sequence[str],
              features: Mapping[str, Mapping[str, float]]) -> list[RankScore]: ...


class StrategyAddon(Addon, Protocol):
    # profile/frame 타입은 engine 계층 소유이므로 여기서는 Any (계약이 engine을 import하지 않게)
    def profile(self) -> Any: ...
    def evaluate(self, frame: Any) -> Sequence[Signal]: ...


class ExitAddon(Addon, Protocol):
    def on_bar(self, position: Position, bars: Sequence[Bar]) -> Sequence[TradeIntent]: ...
    def on_quote(self, position: Position, quote: Quote) -> Sequence[TradeIntent]: ...


class SizerAddon(Addon, Protocol):
    def size(self, intent: TradeIntent, quote: Quote, account: AccountSummary) -> int: ...


class DatasourceAddon(Addon, Protocol):
    def load(self, code: str, tf: str, start_ts: int, end_ts: int) -> list[Bar]: ...
