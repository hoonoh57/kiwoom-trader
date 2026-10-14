# kiwoom-trader 통합 설계도 v2.0 (단일 기준 문서)

- 문서 상태: v1, v1.1 변경분, 5축 철학 합의, 참조 저장소(kiwoom-auto, kiwoom-desk, dsl) 분석 결과를 통합한 단일 기준 문서. 이 문서가 이전 설계보다 우선한다.
- 작성일: 2026-10-14
- 위치: 저장소 루트 `DESIGN.md`. 모든 코딩 세션은 이 문서 하나로 시작한다.

---

## 0. 다음 세션 시작 지시문

> 첨부한 `DESIGN.md`(kiwoom-trader 통합 설계도 v2.0)를 기준으로 코딩한다. 문서와 다르게 판단해야 하면 먼저 이유를 말하고 승인을 받은 뒤 문서를 수정한다. 순서는 22장 M0부터 진행한다. 21장 "확인 필요" 항목은 추측으로 구현하지 않고 `bridge/kiwoom/spec.py`에 상수로 모아 `UNVERIFIED` 표시를 붙인다. 이번 세션 목표는 22.1 첫 묶음과 G1, G8 테스트 통과다.

코딩 원칙:

1. 불변식(3.3)을 깨는 코드는 쓰지 않는다. CI가 검사한다.
2. 키움 API 필드명과 코드값은 `spec.py` 한 곳에만 둔다.
3. 숫자 한도는 코드에 쓰지 않고 State(JSON)에서 읽는다.
4. 새 기능은 먼저 3.1 기준으로 "코어인가, 애드온인가"를 판정한다.

---

## 1. 목표와 범위

키움 REST/WebSocket API만으로 자동매매를 수행하는 종합 도구.

```
조건검색 포착 → 필터(2차 선별) → 랭킹(상위 N) → 차트 전략 신호
→ 단일 주문 게이트 → 체결·포지션 관리 → 전략 청산 / 하드 보호 청산
```

역할 분담:

| 구성 | 역할 |
|---|---|
| 키움 REST/WS | 실시간 시세, 조건검색, 주문, 계좌. 매매 전용 |
| Cybos Plus | 야간 배치 과거 봉 다운로드. 32비트 별도 워커. 백테스트·워밍업 데이터 |
| dsl 엔진(`hoonoh57/dsl`) | 지표·전략 계산의 유일한 주체. 차트·실시간·백테스트가 같은 코드 사용 |
| 브라우저 | 가상화면(VD)과 창 배치, 그리기 전용. 계산하지 않음 |

v1 포함: KRX 정규장 현금 매수 롱 전략, 모의/실전 전환, 1종목 1포지션, 시장가·지정가 주문, 서버 측 합성 STOP, 조건검색 실시간 편입·이탈, 전략 백테스트, 페이퍼 운용, 승격 절차.

v1 제외: 공매도, 신용, 해외, 다계좌, 피라미딩, NXT/SOR 주문(조회만 허용), ML, 시간외 거래.

---

## 2. 기술 스택

| 영역 | 선택 | 비고 |
|---|---|---|
| 서버 | Python 3.12 64비트, FastAPI, uvicorn, asyncio | 단일 프로세스 + 계산 풀 |
| HTTP/WS | httpx.AsyncClient, websockets | |
| 계산 | numpy, `vendor/dsl`(git submodule, 커밋 고정) | 진입점은 `engine/dsl_adapter.py` 하나 |
| 계산 격리 | `ProcessPoolExecutor(mp_context=spawn)` | 시간 초과 시 워커 종료 후 재생성 |
| 원장 | sqlite3, WAL, `synchronous=FULL` | append-only 트리거 |
| 과거 봉 | sqlite3 `data/history.sqlite` | Cybos 워커와 키움 백필 공용 |
| 선언 상태 | JSON 원자 저장(tempfile + fsync + os.replace) | kiwoom-auto `store.py` 수정판 |
| 프로토콜 타입 | pydantic v2 → JSON Schema → `web/src/gen/protocol.ts` | CI가 생성 결과 비교 |
| 웹 | Vite + TypeScript, lightweight-charts v5 | kiwoom-auto VD 로직 TS 이식 |
| Cybos | Python 3.x 32비트, pywin32 | 별도 venv, 별도 프로세스 |
| 테스트 | pytest, pytest-asyncio, hypothesis, Playwright, import-linter | |

---

## 3. 5축 헌법

### 3.1 코어와 애드온의 판정 기준

- 코어: 이것이 없으면 시스템이 안전하게 존재할 수 없는 것.
- 애드온: 없어도 안전하고, 있으면 기능이 늘어나는 것.

애드온 0개일 때 코어의 임무는 "새 위험을 만들지 않고, 기존 위험을 키우지 않는 것"이다. 따라서 하드 손절, 킬 스위치, 일일 손실 한도, 마감 정리, 재기동 대조는 반드시 코어에 둔다.

비대화 방지: 코어에는 장치(메커니즘)와 불변식만 두고, 수치는 State에 둔다. 전략 판단은 애드온이 `TradeIntent`로만 넘긴다. 전략이 늘어나도 코어 코드는 바뀌지 않는다.

### 3.2 5축과 재귀 적용

| 축 | 의미 |
|---|---|
| Core | 유닛의 생존 기능. 애드온 0개로 부팅·동작·복원 가능 |
| Bridge-runtime | 유닛 외부와의 유일한 통로(외부 API, 상위 Manager, 하위 유닛) |
| Addon | 격리 래퍼를 거쳐 붙는 기능. 실패하면 격리되고 코어는 계속 동작 |
| State | 선언 상태 / 원장 / 외부 진실 / 휘발 상태 4종 분리(6장) |
| Manager | 유닛 내부 조정자. 애드온 호스트, 구독, 명령 라우팅 |

모든 유닛(중앙관리자, 차트 창, 조건검색 창, 매매 창 등)은 같은 5축 계약을 따른다. 상위 유닛은 하위 유닛을 애드온처럼 다룬다. 말단 애드온(필터 하나, 지표 하나)은 재귀 대상에서 제외한다. 이 구조로 VD 이동과 앱 재기동 시 선언 상태와 원장 replay를 통해 상태를 100% 복원한다. 단 ARM은 항상 LOCKED로 복원한다.

### 3.3 불변식 (CI 검사)

| 번호 | 불변식 | 검사 |
|---|---|---|
| I1 | Core는 Addon을 import하지 않는다 | import-linter |
| I2 | 모든 애드온 호출은 `unit/isolate.py` 래퍼를 거친다 | addon_host 외 직접 호출 금지 |
| I3 | 애드온은 intent/데이터만 반환하고 부수효과 API를 받지 않는다 | `AddonContext`에 주문·저장 API 없음 |
| I4 | 유닛 간 통신은 Manager/Bridge 경유 | import-linter |
| I5 | 주문 TR(kt10000~kt10003)은 `central/core/order_gate.py`만 호출 | `scripts/audit_order_tr.py` |
| I6 | 미확인 애드온 설정과 알 수 없는 JSON 키는 보존 | G8 |
| I7 | JSON 복원만으로 ARMED 불가 | G3 |
| I8 | 원장 UPDATE/DELETE 불가 | SQLite 트리거 + 테스트 |
| I9 | 엔진(`engine/`)은 bridge와 주문을 모른다 | import-linter |
| I10 | 브라우저는 지표·전략을 계산하지 않는다 | 리뷰 + `web/src` grep |

---

## 4. 유닛 구성

| 유닛 | Core | Addon 예 |
|---|---|---|
| central(서버) | 9장 모듈 전체 | 조건검색 소스, 피처, 필터, 랭커, 전략, 청산, 사이저, Cybos 데이터 소스 |
| chart 창 | 캔들·거래량 렌더, 시간축, bars 구독, 마커 레이어 API, 상태 저장 | 지표 표시, 전략 오버레이, 비교 종목, 레전드 확장 |
| scanner 창 | 조건식 선택, 후보 목록, 편입·이탈 표시, 상태 복원 | 컬럼, 정렬·강조 규칙 |
| selection 창 | 상위 N 결과와 점수·사유 표시 | 점수 분해 뷰 |
| trade 창 | 포지션·미체결·체결 표시, 킬 스위치, 수동 주문 command 전송 | 빠른 주문 패널, 손익 그래프 |
| autotrade 창 | RUNTIME/BROKER 상태, ARM/DISARM, 전략 인스턴스 현황, 최근 신호 100건 | 통계 패널 |
| account 창 | 잔고·예수금 표시 | 비중 차트 |
| strategy 창 | DSL 편집, 서버 검증 결과, 버전·승격 상태 | 템플릿 삽입 |
| backtest 창 | 실행 요청, 리포트 표시 | MFE/MAE 차트 |
| log 창 | 시스템 이벤트, 원장 tail | 필터 |

관제판(autotrade)은 kiwoom-desk AutoTradeForm 원칙을 따른다. 주문 API, 전략 계산, 파라미터 저장을 소유하지 않고 서버 command만 보낸다.

---

## 5. 저장소 구조

```
kiwoom-trader/
  DESIGN.md
  .env.example                 # KIWOOM_USE_PAPER, KIWOOM_{MOCK,REAL}_APPKEY/SECRETKEY, KIWOOM_ACCOUNT, PORT
  pyproject.toml               # import-linter 계약 포함
  start.bat
  vendor/dsl/                  # git submodule (hoonoh57/dsl, 커밋 고정)
  server/kt/
    unit/
      contracts.py             # Unit/Addon/Intent/Event 타입 (7장)
      addon_host.py            # 발견, 등록, attach/detach, 격리, 재부착
      isolate.py               # timeout, 예외 포착, 실패 카운트, 격리
    central/
      core/
        clock.py arm.py kill_switch.py risk.py
        order_gate.py order_fsm.py synthetic_orders.py
        positions.py protection.py ledger.py reconcile.py
        bar_store.py sub_registry.py candidates.py selection.py
        promotion.py boot.py pnl.py ticks.py
      bridge/
        kiwoom/
          spec.py              # TR id, 필드명, 코드값. 미확인 값은 UNVERIFIED
          parse.py             # _number/_stamp/_symbol 이식
          token.py             # 메모리 보관, 단일 갱신 락
          rest.py              # 우선순위 스케줄러 (10.1)
          ws.py                # 단일 세션, trnm 직렬화, 재REG (10.2)
          adapter.py           # candles, quote, order, balance, open_orders, fills, conditions
        cybos/
          reader.py            # history.sqlite 읽기, manifest 확인
        client_hub.py          # /ws/app 세션, 구독 팬아웃
        addon_context.py       # 애드온용 읽기 전용 컨텍스트
      engine/
        dsl_adapter.py         # vendor/dsl 유일 진입점
        market_frame.py        # bar_store → dsl market 입력 변환
        profile.py             # 전략 메타(워밍업, timeframe, 허용 주문)
        strategy_runtime.py    # 인스턴스별 평가 스케줄
        indicator_service.py   # 차트 지표 계산 (dsl ta 재사용)
        pool.py                # ProcessPool 관리
      addons/
        sources/kiwoom_condition/
        features/basic_quote/ features/trading_value/
        filters/price_range/ filters/min_trading_value/ filters/exclude_list/
        rankers/trading_value_rank/ rankers/expr_rank/
        strategies/dsl_strategy/
        exits/trailing_stop/ exits/time_stop/
        sizers/fixed_amount/
        datasources/cybos_history/
      state/
        declared_store.py      # JSON 원자 저장 (store.py 수정판)
        journal_store.py       # ledger.sqlite
        history_store.py       # history.sqlite
        migrations/
      manager/
        central_manager.py     # 유닛 조립, 이벤트 버스, command 라우팅
        bus.py
    backtest/
      runner.py report.py
    api/
      main.py                  # FastAPI 앱, REST, /ws/app
      protocol.py              # pydantic 메시지 (13장)
  strategies/<strategy_id>/<version>/strategy.dsl, meta.json
  state/                       # 실행 시 생성 (gitignore, fixture만 커밋)
    workspace.json risk.json scanner.json strategies.json addons.json calendar.json fees.json
  data/
    ledger.sqlite history.sqlite
  artifacts/
    logs/ daily/ backtests/
  web/
    src/
      gen/protocol.ts
      app/client.ts            # /ws/app 단일 연결, 재연결, 구독 복원
      vd/                      # kiwoom-auto desk/runtime/proptree/screens 이식
      unit/                    # 웹 유닛 공통 계약
      units/chart/ scanner/ selection/ trade/ autotrade/ account/ strategy/ backtest/ log/
      addons/                  # import.meta.glob 선택 로드. 폴더가 없어도 빌드·동작
  workers/cybos32/
    download.py manifest.py requirements32.txt
  scripts/
    audit_order_tr.py gen_protocol.py fake_kiwoom.py
  tests/
    unit/ integration/ gates/ fixtures/
```

---

## 6. State 4종 분리

| 종류 | 내용 | 저장 | 복원 |
|---|---|---|---|
| 선언 상태 | VD·창 배치, 창 props, 애드온 설정, 전략 인스턴스, 조건식 선택, N값, 리스크 수치, 킬 레벨 | JSON(`state/*.json`), schemaVersion, 마이그레이션 | 100% (ARM 제외) |
| 원장 | 신호, 결정, intent, 주문 제출·응답, 체결, 대조, ARM/KILL 변경 | `data/ledger.sqlite` append-only | replay로 주문·포지션 투영 재구성 |
| 외부 진실 | 키움 잔고, 미체결, 체결 | 저장 안 함 | 부팅·주기 대조로 항상 재조회 |
| 휘발 | 시세, 열린 봉, 지표 캐시, 구독 refcount | 메모리 | 재구독으로 재생성 |

선언 상태 저장 규칙 (kiwoom-auto store.py 결함 수정):

- 파싱 실패 시 `StateCorrupt` 예외를 낸다. 빈 dict로 덮어쓰지 않는다. 원본은 `*.corrupt-<ts>`로 보존하고 해당 유닛은 복구 모드로 진입한다(PATCH는 409).
- 저장 순서: `mkstemp` → write → `flush` → `os.fsync` → `os.replace`. PermissionError는 재시도한다. 가능하면 디렉터리도 fsync한다.
- 루트 삭제는 거부한다. 알 수 없는 키는 그대로 왕복시킨다(I6).

---

## 7. 공통 계약 (`server/kt/unit/contracts.py`)

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping, Protocol, Sequence


class AddonKind(str, Enum):
    SOURCE = "source"
    FEATURE = "feature"
    FILTER = "filter"
    RANKER = "ranker"
    STRATEGY = "strategy"
    EXIT = "exit"
    SIZER = "sizer"
    DATASOURCE = "datasource"


@dataclass(frozen=True)
class AddonManifest:
    id: str                      # "filters.min_trading_value"
    kind: AddonKind
    version: str                 # semver
    api: int                     # 계약 버전. 호스트와 불일치하면 attach 거부
    config_schema: Mapping[str, Any]
    compute: Literal["inline", "pool"] = "inline"
    timeout_ms: int = 200


class AddonState(str, Enum):
    DETACHED = "DETACHED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    INCOMPATIBLE = "INCOMPATIBLE"


class Addon(Protocol):
    manifest: AddonManifest
    def attach(self, ctx: "AddonContext", config: Mapping[str, Any]) -> None: ...
    def detach(self) -> None: ...


class Unit(Protocol):
    unit_id: str
    def boot(self) -> None: ...                       # 애드온 0개로 성공해야 함
    def snapshot_state(self) -> dict: ...
    def restore_state(self, raw: dict) -> None: ...   # 알 수 없는 키 보존


# ---- 데이터 ----
@dataclass(frozen=True)
class Bar:
    ts: int          # 봉 시작 시각, KST 기준 epoch 초
    o: int
    h: int
    l: int
    c: int
    v: int
    amount: int      # 거래대금(원). 없으면 c*v 추정 후 flag
    confirmed: bool


@dataclass(frozen=True)
class Quote:
    code: str
    ts_ms: int
    price: int
    change_pct: float
    volume: int
    amount: int
    best_ask: int | None
    best_bid: int | None
    upper_limit: int | None
    lower_limit: int | None


@dataclass(frozen=True)
class CandidateEvent:
    source_id: str
    code: str
    kind: Literal["IN", "OUT"]
    ts_ms: int
    meta: Mapping[str, Any]


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str


@dataclass(frozen=True)
class RankScore:
    code: str
    score: float
    parts: Mapping[str, float]


@dataclass(frozen=True)
class Signal:
    instance_id: str
    code: str
    tf: str
    bar_ts: int
    name: str                     # "long_entry", "long_exit", "ARM" 등
    orders: Sequence[Mapping[str, Any]]


Origin = Literal["STRATEGY", "EXIT", "PROTECTION", "MANUAL", "FLATTEN", "TTL"]


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str                # ULID
    origin: Origin
    source_id: str                # 전략 인스턴스 id / 애드온 id / "core"
    code: str                     # 6자리, v1은 KRX만
    action: Literal["ENTRY_LONG", "EXIT_LONG", "CANCEL"]
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"] = "MARKET"
    qty: int | None = None        # None: 진입은 사이저 결정, EXIT는 전량
    limit_price: int | None = None
    stop_price: int | None = None
    target_order_key: str | None = None   # CANCEL 대상
    bar_ts: int | None = None
    reason: str = ""
    dedupe_key: str = ""          # 비어 있으면 gate가 생성


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    order_key: str | None
    reject_code: str | None       # 9.5 거부 코드
    detail: str = ""
```

### 7.1 애드온 종류별 인터페이스

| 종류 | 메서드 | 실행 |
|---|---|---|
| source | `start(emit)`, `stop()`; `emit(CandidateEvent)` | inline, 비동기 작업은 ctx 스케줄러 사용 |
| feature | `compute(code, quote, bars: Mapping[tf, Sequence[Bar]]) -> Mapping[str, float]` | inline 또는 pool |
| filter | `check(code, features) -> FilterResult` | inline |
| ranker | `score(codes, features) -> list[RankScore]` | inline |
| strategy | `profile() -> StrategyProfile`, `evaluate(frame) -> list[Signal]` | pool |
| exit | `on_bar(position, bars) -> list[TradeIntent]`, `on_quote(position, quote) -> list[TradeIntent]` | inline |
| sizer | `size(intent, quote, account) -> int` | inline |
| datasource | `load(code, tf, start, end) -> list[Bar]` | inline (I/O는 to_thread) |

`AddonContext` 제공 항목: 읽기 전용 `bars()`, `quote()`, `position()`, `account_summary()`, `clock`, `log`, 설정 값. 주문, 저장, 키움 호출 API는 없다(I3).

### 7.2 격리 정책 (`isolate.py`)

- 호출마다 `timeout_ms`를 적용하고 예외를 포착한다.
- 5분 안에 3회 실패하면 `QUARANTINED`로 바꾸고 원장에 `ADDON_QUARANTINE`을 기록하고 UI에 표시한다. 재부착은 수동 command로만 한다.
- pool 작업이 시간을 초과하면 해당 워커 프로세스를 종료하고 재생성한다.
- inline 애드온은 스레드를 강제 종료할 수 없으므로 소요 시간을 측정해 초과하면 실패로 계산한다.

격리 시 코어 처리:

| 격리된 애드온 | 코어 처리 |
|---|---|
| strategy | 해당 인스턴스 신규 진입 중단. 보유 포지션은 코어 보호(하드 손절, 마감 정리)로 관리 |
| exit | 하드 손절과 마감 정리만으로 관리 |
| source/filter/ranker | 선별 갱신 중단. 기존 선택 유지 |
| sizer | 코어 기본 고정금액 사이징(`risk.perTradeAmount`) |

---

## 8. 도메인 규칙

시간:
- 모든 시각은 KST. 봉 `ts`는 봉 시작 시각이다. 키움 분봉 시각 기준은 C9에서 확인하고 `parse.py`에서 정규화한다.

장 운영 (`clock.py`, `calendar.json`):

| 구간 | 시각 |
|---|---|
| 장시작 동시호가 | 08:30–09:00 |
| 정규장 | 09:00–15:30 |
| 장마감 동시호가 | 15:20–15:30 |

- 휴장일·특수일(수능일 지연 개장 등)은 `calendar.json`에서 읽는다.
- 진입 허용 창 `risk.entryWindow`(기본 09:05–15:00), 마감 정리 `risk.flattenAt`(기본 15:15).
- 동시호가 중 시장가 주문 금지.

종목 코드:
- 6자리. 조회용으로만 `_AL`(SOR), `_NX`(NXT) 접미사를 허용하고, 모의 서버에서는 접미사를 거부한다(kiwoom-auto `_query_symbol` 규칙). 주문은 v1에서 KRX만.

가격:
- 정수 원 단위. 키움 응답의 `+`/`-` 부호 접두 문자열은 `parse.number()`가 처리하고, 가격 필드는 절대값을 쓴다.

호가단위 (`ticks.py`, C11 재확인):

| 가격대(원) | 호가단위(원) |
|---|---|
| 2,000 미만 | 1 |
| 2,000 이상 5,000 미만 | 5 |
| 5,000 이상 20,000 미만 | 10 |
| 20,000 이상 50,000 미만 | 50 |
| 50,000 이상 200,000 미만 | 100 |
| 200,000 이상 500,000 미만 | 500 |
| 500,000 이상 | 1,000 |

- 매수 지정가는 내림, 매도 지정가는 올림으로 정규화한다. 상하한가를 넘으면 거부한다.

수수료·세금 (`fees.json`):
- 매수·매도 수수료율, 매도 거래세율을 설정 값으로 둔다. 2026년 세율은 C10에서 확인하고 코드 상수로 쓰지 않는다.

---

## 9. 중앙관리자 Core 모듈

### 9.1 `boot.py` 부팅 상태기계

```
BOOTING → LOADING(선언 상태) → REPLAYING(원장) → CONNECTING(토큰·WS)
→ RECONCILING → SAFE → ATTACHING(애드온) → RESUBSCRIBE → READY(LOCKED)
```

- READY 후 사용자가 ARM하면 ARMED.
- 어느 단계든 실패하면 SAFE에 머문다. SAFE는 조회·표시만 가능하고 주문은 PROTECTION/FLATTEN만 허용한다.
- 대조 불일치 시 READY로 가되 kill을 `PAUSE_ENTRIES`로 설정하고 사유를 표시한다.
- 애드온 0개여도 READY까지 도달해야 한다(G1).

### 9.2 `arm.py`

- 상태는 `LOCKED`(기본) / `ARMED`. 영속 저장하지 않고 재기동 시 항상 LOCKED(I7).
- ARM command는 `confirm`으로 계좌번호 끝 4자리가 필요하고, 실전 모드에서는 `"LIVE"` 문자열 입력도 필요하다.
- DISARM은 즉시 적용된다. 진행 중 주문은 유지하고 신규 자동 주문만 차단한다.
- 변경은 원장 `ARM`에 기록한다.

### 9.3 `kill_switch.py`

| 레벨 | 신규 진입 | 전략·청산 애드온 EXIT | PROTECTION/FLATTEN | 수동 주문 |
|---|---|---|---|---|
| NONE | 허용 | 허용 | 허용 | 확인 후 허용 |
| PAUSE_ENTRIES | 차단 | 허용 | 허용 | 매도만 |
| FLATTEN | 차단 | 허용 | 미체결 진입 주문 전부 취소 → 전 포지션 시장가 매도 → 완료 시 HALT | 차단 |
| HALT | 차단 | 차단 | `protection.activeWhenHalted`가 true면 하드 손절만 | 확인 후 매도만 |

- 자동 전환(→ PAUSE_ENTRIES): 일일 손실 한도 도달, 대조 불일치, 연속 주문 거부 5회, WS 끊김 60초 초과.
- 해제는 수동으로만 한다. 변경은 원장 `KILL`에 기록한다.
- 킬 레벨은 선언 상태에 저장하고 재기동 후에도 유지한다(위험을 줄이는 상태이므로 유지가 안전하다).

### 9.4 `risk.py`와 `risk.json`

```json
{
  "schemaVersion": 1,
  "maxPositions": 5,
  "perTradeAmount": 1000000,
  "maxOrderAmount": 2000000,
  "maxOrderQty": 10000,
  "maxTotalExposure": 8000000,
  "minCashBuffer": 100000,
  "dailyLossLimit": -300000,
  "flattenOnDailyLoss": false,
  "maxOrdersPerMinute": 20,
  "reentryCooldownSec": 600,
  "hardStopPct": -3.0,
  "entryWindow": ["09:05", "15:00"],
  "flattenAt": "15:15",
  "staleQuoteSec": 5,
  "entryTtlSec": 30,
  "priceBandPct": 2.0,
  "protection": {
    "activeWhenLocked": true,
    "activeWhenHalted": true,
    "protectExternal": false
  }
}
```

- 일일 손익 = 당일 실현손익 + 보유 평가손익(수수료·세금 반영, `pnl.py`).
- `priceBandPct`: 지정가가 현재가 대비 ±값(%)을 넘으면 거부한다(오입력 방지).
- `protectExternal`: 시스템이 만들지 않은 보유 종목(EXTERNAL)을 하드 손절 대상에 넣을지 여부. 기본값 false.

### 9.5 `order_gate.py` 단일 주문 게이트

모든 주문의 유일한 경로(I5).

```python
async def submit(intent: TradeIntent) -> GateResult:
    ledger.append("INTENT", intent)
    for check in CHECKS:                                   # 아래 표 순서
        code = check(intent, ctx)
        if code:
            ledger.append("GATE_REJECT", {"intent_id": intent.intent_id, "code": code})
            return GateResult(False, None, code)
    if intent.order_type in ("STOP", "STOP_LIMIT"):
        key = synthetic_orders.register(intent)            # 트리거 시 MARKET/LIMIT intent로 재진입
        return GateResult(True, key, None, "SYNTHETIC")
    order = fsm.create(intent, qty, price)                 # NEW
    ledger.append("ORDER_NEW", order)                      # 원장 먼저
    fsm.to(order, "SUBMITTING")
    ledger.append("ORDER_SUBMIT", order)
    try:
        resp = await kiwoom.order(order, priority=P0, retry=False)
    except (Timeout, NetworkError):
        fsm.to(order, "UNKNOWN")
        ledger.append("ORDER_UNKNOWN", order)
        reconcile.schedule_unknown(order)                  # 재전송 금지
        return GateResult(True, order.key, None, "UNKNOWN")
    if resp.ok and resp.ord_no:
        fsm.to(order, "ACCEPTED", ord_no=resp.ord_no)
        ledger.append("ORDER_ACK", order)
        return GateResult(True, order.key, None)
    fsm.to(order, "REJECTED")
    ledger.append("REJECTED", order)
    return GateResult(False, order.key, "BROKER_REJECT", resp.message)
```

검사 순서와 거부 코드:

| 순서 | 검사 | 거부 코드 | 대상 |
|---|---|---|---|
| 1 | boot가 READY인가 (SAFE면 PROTECTION/FLATTEN만) | `NOT_READY` | 전체 |
| 2 | ARM (LOCKED면 STRATEGY/EXIT 차단, PROTECTION/FLATTEN은 activeWhenLocked 따름, MANUAL은 confirm 필요) | `LOCKED` | 전체 |
| 3 | 킬 레벨 표(9.3) | `KILL_<LEVEL>` | 전체 |
| 4 | 승격: STRATEGY 버전이 모드별 허용 상태인가(9.15) | `NOT_PROMOTED` | STRATEGY |
| 5 | 시간: 진입 창, 동시호가 시장가 금지, 휴장 | `OUT_OF_WINDOW` | 진입 전체, 시장가 |
| 6 | 종목 잠금: in-flight 주문, UNKNOWN 주문, 기존 포지션(진입), 재진입 쿨다운 | `SYMBOL_BUSY` / `SYMBOL_UNKNOWN` / `HAS_POSITION` / `COOLDOWN` | 진입, UNKNOWN은 전체 |
| 7 | 시세 신선도 (마지막 0B 수신 후 staleQuoteSec 이내) | `STALE_QUOTE` | 진입 |
| 8 | 수량 결정(사이저 또는 코어 기본), 매도 수량 ≤ 보유 − 대기 매도 | `BAD_QTY` | 전체 |
| 9 | 한도: maxPositions, maxOrderAmount/Qty, maxTotalExposure, minCashBuffer, dailyLossLimit, maxOrdersPerMinute | `LIMIT_<NAME>` | 진입 (주문 빈도는 전체) |
| 10 | 가격 정규화: 호가단위, 상하한가, priceBand | `BAD_PRICE` | 지정가 |
| 11 | 중복 키: 기본 `f"{source_id}:{code}:{bar_ts}:{action}"`, 당일 원장에 있으면 거부 | `DUPLICATE` | 전체 |
| 12 | 모드: 모의 서버에서 KRX 외 시장 거부, 공매도 금지 | `MODE` | 전체 |

### 9.6 `order_fsm.py`

```
NEW → SUBMITTING → ACCEPTED → PARTIAL → FILLED
                 ↘ REJECTED
                 ↘ UNKNOWN → (대조) → ACCEPTED | PARTIAL | FILLED | REJECTED | NOT_FOUND
ACCEPTED/PARTIAL → CANCEL_REQ → CANCELED (부분체결 수량 유지)
```

- 허용되지 않은 전이는 예외로 처리하고, 원장에 `FSM_VIOLATION`을 기록한 뒤 PAUSE_ENTRIES로 전환한다.
- 주문번호 수신 ≠ 체결. 체결은 WS `00`(주문체결) 또는 체결 조회 결과로만 반영한다.
- 진입 지정가가 `entryTtlSec` 안에 체결되지 않으면 origin=TTL CANCEL intent를 생성한다.

### 9.7 `positions.py`

- 원장 FILL replay로 계산한다. 종목별 수량, 평균단가, 진입 시각, origin(SYSTEM/EXTERNAL), 소속 전략 인스턴스.
- 대조 결과가 외부 진실과 다르면 외부 진실을 채택하고 차이를 `RECONCILE_ADJUST`로 기록한다.

### 9.8 `protection.py`

- 하드 손절가 = 평균단가 × (1 + hardStopPct/100). 0B 시세가 이하이면 origin=PROTECTION 전량 시장가 매도 intent를 생성한다.
- `flattenAt`에 SYSTEM 포지션 전량을 origin=FLATTEN으로 매도한다. 전략 메타 `holdOvernight: true` 인스턴스는 제외(v1 기본 false).
- 같은 종목 보호 주문은 in-flight 동안 재생성하지 않는다. 미체결 30초 경과 시 취소 후 재시도하되, UNKNOWN 상태가 아닐 때만 재시도한다.
- 애드온 0개여도 동작한다(G1, G2).

### 9.9 `ledger.py` (DDL 16장)

- 해시 체인 `hash = sha256(prev_hash + kind + payload)`. 부팅 시 검증하고 깨져 있으면 SAFE에 머문다.
- 쓰기는 단일 asyncio 태스크에서 직렬화하고 항목마다 commit한다(synchronous=FULL).

### 9.10 `reconcile.py`

- 실행 시점: 부팅 시, 장중 `reconcile.intervalSec`(기본 60초)마다, WS 재연결 직후, UNKNOWN 발생 시 즉시(5초 간격 최대 12회).
- 조회 대상: 잔고(kt00018 계열), 미체결(ka10075 계열), 당일 체결(ka10076 계열). 정확한 TR은 C4.
- UNKNOWN 해소: 주문번호가 있으면 주문번호로, 없으면 (종목, 방향, 수량, 제출 시각 ±60초)로 매칭한다. 끝까지 못 찾으면 `NOT_FOUND`로 두고 종목 잠금을 유지한다. 해제는 수동 command.

### 9.11 `bar_store.py`

- 키 `(code, tf)`. 지원 tf: `tick, 1m, 3m, 5m, 10m, 15m, 30m, 60m, 1d, 1w, 1M`.
- 메모리 링버퍼 `MAX_BARS`(기본 1,200). 과거 봉은 `history.sqlite`에서 채운다.
- 초기 적재: history 우선 → 부족 구간은 키움 차트 TR(P3) → 병합.
- 실시간: 0B 체결로 1m 봉을 집계하고 상위 분봉은 1m에서 파생한다. 열린 봉 갱신은 `replace`, 새 봉 시작은 `append`(kiwoom-desk 계약).
- 봉 확정: 다음 봉 첫 체결 수신 또는 봉 종료 후 `bar.confirmGraceSec`(기본 2초) 경과.
- 월봉 확정은 달력 월 기준으로 한다(kiwoom-auto 30일 고정 결함 수정).
- 틱 봉은 같은 초의 여러 틱을 버리지 않는다. 키는 (ts, seq)(kiwoom-auto 결함 수정).
- 이벤트: `BarsReset(code, tf)`, `BarChanged(code, tf, bar, 'append'|'replace')`, `BarConfirmed(code, tf, bar)`.

### 9.12 `sub_registry.py`

- (topic, key) 단위 refcount. WS REG/REMOVE는 refcount가 0↔1로 바뀔 때만 보낸다.
- 업스트림 등록 상한 `ws.maxItems`(C8). 초과 시 우선순위: 보유 포지션 > 선택된 상위 N > 창에서 연 종목 > 후보. 등록하지 못한 구독은 REST 저속 폴링(P2, 5초)으로 대체하고 UI에 표시한다.

### 9.13 `candidates.py`

- 소스별 출처(provenance, 조건식 seq 등)와 함께 후보 집합을 유지한다.
- OUT 이벤트 후 `candidates.outGraceSec`(기본 30초) 동안 보존해 깜박임을 막는다.
- 후보 상한 `scanner.maxCandidates`(기본 100).

### 9.14 `selection.py` 상위 N 선별

- `scanner.rankIntervalSec`(기본 15초)마다: 후보 → 피처 → 필터 AND → 랭커 점수 → 상위 N.
- 히스테리시스: 기존 선택 종목은 새 종목이 점수 차 `swapMargin`(기본 10%) 이상 앞서고 `minHoldSec`(기본 120초)이 지났을 때만 교체한다.
- 선택에서 빠진 종목은 신규 진입만 중단하고 보유 포지션은 계속 관리한다.
- 선택 종목마다 `scanner.json` 템플릿으로 전략 인스턴스를 만든다. id는 `tpl:<templateId>:<code>`.
- 랭커 애드온이 없으면 선별을 중단한다. 고정 감시목록 인스턴스는 계속 동작한다.

### 9.15 `promotion.py`

| 상태 | 의미 |
|---|---|
| DRAFT | 저장만 됨. 차트 표시용 |
| BACKTESTED | 백테스트 리포트 해시 연결됨 |
| PAPER | 모의 서버 자동매매 허용 |
| LIVE | 실전 서버 자동매매 허용 |

- BACKTESTED → PAPER: 리포트 존재. PAPER → LIVE: 모의 운용 `promotion.minPaperDays`(기본 5) 거래일 이상, FSM 위반 0건.
- 승격은 수동 command + confirm으로만 한다. 버전 파일은 불변이며 수정하면 새 버전이 된다.

---

## 10. Bridge

### 10.1 키움 REST (`bridge/kiwoom/rest.py`)

| 우선순위 | 용도 | 재시도 |
|---|---|---|
| P0 | 주문(kt10000~10003) | 금지. 시간 초과는 UNKNOWN |
| P1 | 대조용 계좌·미체결·체결 | 1회 |
| P2 | 현재가, 폴링 대체 시세 | 1회 |
| P3 | 차트 초기 적재 | 1회 |
| P4 | 백필, 조건식 목록 등 | 1회 |

- 우선순위별 큐를 둔다. 단일 디스패처가 토큰 버킷 `rest.ratePerSec`(기본 3)과 `rest.orderRatePerSec`(기본 3)을 확인하고 P0부터 꺼낸다. 실제 한도는 C7.
- 한도 초과 응답(C7) 시 버킷을 절반으로 줄이고 30초 후 복구한다.
- 락은 토큰 갱신에만 건다. HTTP 호출 중에는 락을 잡지 않는다(kiwoom-auto 단일 락 결함 수정).
- single-flight: 같은 (api_id, 본문 해시) 요청이 진행 중이면 Future를 공유한다.
- HTTP 401 또는 코드 8005: 토큰 재발급 후 1회 재시도. P0는 재시도하지 않고 UNKNOWN 처리.
- 연속 조회: `cont-yn`, `next-key` 헤더, 최대 20페이지, 같은 next-key 반복 시 중단. 페이지 사이에 큐를 양보해 P0가 끼어들 수 있게 한다.
- 토큰은 메모리에만 둔다. 디스크 캐시가 필요하면 Windows DPAPI로 암호화한다.

kiwoom-auto에서 확인된 TR 매핑 (spec.py로 이식):

| 용도 | api-id | 비고 |
|---|---|---|
| tick | ka10079 | 틱 차트 |
| 1m~60m | ka10080 | 분봉, tic_scope로 주기 지정 |
| 1d | ka10081 | 일봉 |
| 1w | ka10082 | 주봉 |
| 1M | ka10083 | 월봉 |
| 현재가 | ka10001 | 주식기본정보 |
| 주문 | kt10000 매수, kt10001 매도, kt10002 정정, kt10003 취소 | `/api/dostk/ordr`, 정정·취소 필드는 C2 |

- 엔드포인트: 토큰 `/oauth2/token`, 차트 `/api/dostk/chart`, 종목정보 `/api/dostk/stkinfo`, 주문 `/api/dostk/ordr`.
- 기본 URL: `https://mockapi.kiwoom.com`(모의), `https://api.kiwoom.com`(실전).

### 10.2 키움 WebSocket (`bridge/kiwoom/ws.py`)

- 서버 전체에 업스트림 세션은 하나(kiwoom-desk 탭별 연결 결함 수정).
- URL: `wss://mockapi.kiwoom.com:10000/api/dostk/websocket`(모의), `wss://api.kiwoom.com:10000/api/dostk/websocket`(실전).
- 연결 후 `LOGIN`(token) 전송. 서버 `PING`은 그대로 반송한다.
- trnm별 직렬 큐: 같은 trnm은 응답 수신 후 다음 요청을 보낸다. 10초 무응답 시 해당 trnm을 차단하고 재연결한다.
- 재연결: 지수 백오프(1, 2, 4, …, 최대 30초). 성공하면 sub_registry 전체 재REG, 조건검색 재요청, 대조 1회.
- 실시간 타입: `0B` 주식체결, `0D` 주식호가잔량, `00` 주문체결, `04` 잔고. 필드 번호는 C5.
- 조건검색: `CNSRLST`(목록), `CNSRREQ`(요청, 실시간 옵션), `CNSRCLR`(해제), 실시간 편입·이탈 수신. 형식은 C6.

### 10.3 Cybos 워커 (`workers/cybos32/`)

- 별도 32비트 Python 프로세스, 작업 스케줄러로 야간 실행. 서버와 직접 IPC 없음.
- 출력: `data/history.sqlite`의 `bars`에 `source='cybos'`로 기록하고, 완료 시 `manifest`에 (code, tf, from, to, rows, finished_at)를 기록한다.
- 서버는 `datasources/cybos_history` 애드온으로 읽는다. 애드온이 없어도 키움 TR 초기 적재로 동작한다.
- Cybos 요청 제한과 분봉 시각 표기는 C13. 시각은 읽을 때 정규화한다.

---

## 11. 엔진 (dsl 통합)

### 11.1 원칙

- `vendor/dsl`은 수정하지 않는다. 필요한 수정은 upstream에 먼저 반영한다.
- 서버에서 dsl을 import하는 곳은 `engine/dsl_adapter.py` 하나뿐이다(import-linter 강제).

확인된 dsl API:

- `core.dsl_parser.DSLParser().parse(source)` → `StrategyNode`. fail-closed, 오류 코드 제공.
- `core.dsl_runtime.DSLRuntime.compile(code, market, *, context_binding=None, target_times=None)` → `CompiledStrategy(name, timeframe, values, events, context_binding_hash)`. `events`는 `CompiledEvent(condition, orders, line)` 목록, `signal(name)`은 bool series 반환.
- 주문 명령: `order.buy`, `order.close_long`, `order.close_position`, `order.cancel`, `order.sell_short`, `order.close_short`. 타입 MARKET/LIMIT/STOP/STOP_LIMIT, tif GTC/DAY.
- `core.broker.BrokerEmulator`: 백테스트 체결 시뮬레이터.
- `market` 입력 형식은 미확인(C12). M5 시작 시 `dsl_runtime.py`, `backtest.py`를 읽고 `market_frame.py`에 반영한다.

### 11.2 `dsl_adapter.py` 인터페이스

```python
@dataclass(frozen=True)
class StrategyProfile:
    strategy_id: str
    version: str
    timeframe: str
    warmup_bars: int            # meta.json 값, 없으면 300
    allows: frozenset[str]      # v1: {"order.buy","order.close_long","order.close_position","order.cancel"}
    source_hash: str


def validate(source: str) -> list[DslIssue]: ...                    # 파서·의미 분석 결과, 오류 코드 그대로
def load_profile(source: str, meta: dict) -> StrategyProfile: ...   # 금지 명령(sell_short 등) 있으면 실패
def evaluate_last(source: str, frame: MarketFrame) -> list[Signal]: ...
def evaluate_series(source: str, frame: MarketFrame) -> SeriesResult: ...  # 차트 마커·백테스트용
def indicator(name: str, args: dict, frame: MarketFrame) -> dict[str, np.ndarray]: ...
```

실시간 평가 (prefix 동일성):

- `BarConfirmed` 시 확정 봉만으로 길이 `warmup_bars + 200`의 frame을 만들고 pool에서 `evaluate_last`를 실행한다. 마지막 봉에서 참이 된 이벤트만 Signal로 만든다.
- dsl은 무재도색 MTF와 batch/streaming prefix 동일성 테스트를 보유하므로 백테스트 전체 계산과 결과가 같아야 한다. G4로 검증한다.
- v1은 열린 봉으로 전략을 평가하지 않는다. 열린 봉 값은 차트 지표 표시에만 쓴다.

dsl 주문 → TradeIntent 변환:

| dsl | TradeIntent |
|---|---|
| `order.buy(type="MARKET")` | ENTRY_LONG MARKET |
| `order.buy(type="LIMIT", limit=x)` | ENTRY_LONG LIMIT, x는 호가단위 정규화 |
| `order.buy(type="STOP", stop=x)` / STOP_LIMIT | synthetic_orders 등록 |
| `order.close_long()` / `order.close_position()` | EXIT_LONG 전량 MARKET |
| `order.cancel(id=...)` | CANCEL, 같은 인스턴스의 dsl id에 매핑 |
| `sell_short`, `close_short` | profile 로드 단계에서 거부 |

- tif DAY는 장 마감 시 취소한다. GTC는 v1에서 DAY로 취급하고 검증 경고를 띄운다.

### 11.3 `strategy_runtime.py`

- 인스턴스: (instance_id, strategy_id@version, code, tf, params, mode).
- 출처: (a) 선별 템플릿(9.14), (b) 고정 인스턴스(`strategies.json`), (c) 차트 창 전략 오버레이. (c)는 표시 전용이며 주문할 수 없다.
- 신호 처리: 원장 `SIGNAL` 기록 → `signals` evt 전송 → `mode == "auto"`이면 intent를 `order_gate`로 전달.
- 평가 실패는 isolate 정책을 따른다. 격리되어도 보유 포지션은 코어가 보호한다.

### 11.4 `indicator_service.py`

- 차트가 `ind:<code>:<tf>:<indicatorId>:<paramsHash>`를 구독하면, `BarsReset` 시 전체 결과를 snap으로, `BarChanged` 시 마지막 값만 evt로 보낸다(kiwoom-desk reset/update 계약).
- 지표 정의는 dsl `ta.*` 이름과 파라미터를 쓴다. 기본 제공: sma, ema, rsi, macd, supertrend, atr, jma, vwap(세션 기준, dsl 지원 여부 확인). 브라우저는 결과 배열만 그린다.

---

## 12. 자동매매 일일 타임라인

| 시각 | 동작 |
|---|---|
| 08:00 | 서버 기동 → 부팅 → READY(LOCKED), 대조 완료 |
| 08:30 | 조건검색 실시간 요청 시작(`scanner.json` 조건식 seq 목록). 사용자 ARM |
| 09:00~ | 후보 편입·이탈 → 15초마다 선별 → 선택 종목 구독(0B, 1m) → 전략 인스턴스 생성·워밍업(history + TR) |
| 09:05~15:00 | 확정 봉 → 전략 평가 → ENTRY intent → 게이트 → 주문 → 체결 → 포지션 → exit 애드온·하드 손절 감시 |
| 15:00 | 신규 진입 종료, 미체결 진입 주문 취소 |
| 15:15 | 마감 정리(FLATTEN) |
| 15:30 이후 | 최종 대조, 일일 리포트 `artifacts/daily/<date>.json`(원장 기반), 자동 DISARM |
| 야간 | Cybos 배치 다운로드 |

---

## 13. 클라이언트 프로토콜 (`api/protocol.py`)

### 13.1 연결과 보안

- `/ws/app` 단일 WebSocket. 서버는 `127.0.0.1`에만 바인드하고 `TrustedHostMiddleware`를 적용한다. WS Origin이 자기 자신이 아니면 거부한다.
- 세션 토큰: 기동 시 무작위로 생성해 index.html meta 태그로 전달한다. WS 첫 메시지 `hello`와 모든 REST 쓰기 요청에서 확인한다(DNS rebinding 방지).
- 키움 범용 프록시 API는 두지 않는다.

### 13.2 메시지

```python
from typing import Any, Literal
from pydantic import BaseModel

# 클라이언트 → 서버
class Hello(BaseModel):
    t: Literal["hello"]
    token: str
    client: str

class Sub(BaseModel):
    t: Literal["sub"]
    id: str
    topic: str
    args: dict = {}

class Unsub(BaseModel):
    t: Literal["unsub"]
    id: str

class Cmd(BaseModel):
    t: Literal["cmd"]
    id: str
    name: str
    args: dict = {}

# 서버 → 클라이언트
class Welcome(BaseModel):
    t: Literal["welcome"]
    boot: str
    mode: Literal["paper", "live"]
    arm: str
    kill: str
    server_time_ms: int

class Snap(BaseModel):
    t: Literal["snap"]
    sub: str
    seq: int
    data: Any

class Evt(BaseModel):
    t: Literal["evt"]
    sub: str
    seq: int
    data: Any

class Ack(BaseModel):
    t: Literal["ack"]
    id: str
    ok: bool
    code: str | None = None
    detail: str = ""
    data: Any = None
```

- seq는 구독별로 단조 증가한다. 클라이언트가 빈 번호를 발견하면 재구독해 snap을 다시 받는다.

### 13.3 토픽

| topic | args | snap / evt |
|---|---|---|
| `system` | | 부팅·ARM·킬·연결 상태 / 변경 |
| `bars` | code, tf, limit | 봉 배열 / append·replace |
| `ind` | code, tf, id, params | 지표 시리즈 / 마지막 값 |
| `markers` | instance_id | 전략 마커 / 신규 |
| `quote` | code | 현재가 / 갱신 |
| `orderbook` | code | 호가 / 갱신 |
| `candidates` | | 후보·출처 / IN·OUT |
| `selection` | | 상위 N·점수 분해 / 변경 |
| `signals` | | 최근 100건 / 신규 |
| `orders` | | 당일 주문 투영 / FSM 전이 |
| `positions` | | 포지션 / 변경 |
| `account` | | 예수금·평가 / 대조 시 갱신 |
| `strategies` | | 인스턴스 현황(RUNTIME ONLINE/WAIT, 격리) / 변경 |
| `addons` | | 애드온 상태 / 변경 |
| `backtests` | | 실행 목록 / 진행·완료 |
| `log` | level | tail / 신규 |

### 13.4 커맨드

| name | args | 비고 |
|---|---|---|
| `arm` | confirm, live_phrase? | 9.2 |
| `disarm` | | |
| `kill.set` | level, reason | NONE 해제는 confirm 필요 |
| `order.manual` | code, side, qty, type, price?, confirm | origin=MANUAL로 게이트 통과 |
| `order.cancel` | order_key | |
| `order.unknown.resolve` | order_key, resolution | NOT_FOUND 수동 해소 |
| `scanner.set` | conditions[], n, filters[], ranker, template | `scanner.json` 갱신 |
| `strategy.save` | strategy_id, source, meta | 새 버전 생성 + validate |
| `strategy.promote` | strategy_id, version, to, confirm | |
| `strategy.instance.upsert` / `remove` / `start` / `stop` | | |
| `addon.config.set` | addon_id, config | 스키마 검증, 알 수 없는 키 보존 |
| `addon.reattach` | addon_id | 격리 해제 |
| `workspace.patch` | json-patch | 복구 모드면 409 |
| `backtest.run` | strategy_id, version, codes, tf, from, to, params | 비동기, 결과는 `backtests` 토픽 |

### 13.5 REST

- `GET /api/health`
- `GET /api/workspace`, `PUT /api/workspace`
- `GET /api/bars?code&tf&limit` (초기 차트, 캐시 우선)
- `GET /api/strategies`
- `GET /api/backtests/{id}`
- `GET /api/ledger?from&kind` (읽기 전용)

---

## 14. 웹 (가상화면과 창 유닛)

### 14.1 kiwoom-auto에서 살리는 것

- VD 다중 데스크와 창 배치(desk.js, runtime.js), 속성 트리(proptree.js), 화면 레지스트리(screens.js), 9자리 종목 입력과 `_AL`/`_NX` 링크·복원, 종목명 표시, 워크스페이스 v6 스키마.
- `web/src/vd/`로 TS 이식. 워크스페이스는 v7로 올리고 v6→v7 마이그레이션과 fixture 테스트를 만든다(`workspace.v6.fixture.json` 재사용).
- 차트 렌더링은 lightweight-charts v5 기준으로 이식한다. 지표 계산 코드는 이식하지 않는다(I10).

### 14.2 웹 유닛 계약 (`web/src/unit/`)

```ts
export interface WindowUnit {
  kind: string;                                                      // 'chart' | 'scanner' | ...
  mount(el: HTMLElement, state: unknown, bridge: UnitBridge): void;  // 애드온 0개로 동작
  update(state: unknown): void;                                      // props 변경
  snapshot(): unknown;                                               // 알 수 없는 키 보존
  unmount(): void;                                                   // 구독 해제 필수
}

export interface UnitBridge {                                        // 창의 유일한 외부 통로
  sub(topic: string, args: object, on: (msg: Snap | Evt) => void): () => void;
  cmd(name: string, args: object): Promise<Ack>;
  link: {                                                            // 창 간 종목 연동
    emit(code: string): void;
    on(cb: (code: string) => void): () => void;
  };
}

export interface WindowAddon {
  id: string;
  unit: string;
  attach(api: unknown, cfg: unknown): void;
  detach(): void;
}
```

- 웹 애드온은 `import.meta.glob('../addons/**/register.ts')`로 선택 로드한다. 폴더가 없어도 빌드·실행이 성공해야 한다(kiwoom-desk 방식).
- VD 간 창 이동은 unmount → mount + snapshot 복원으로 처리한다. 서버 구독은 refcount이므로 중복 등록이 없다.
- `app/client.ts`는 연결이 끊기면 재연결 후 모든 sub를 재전송하고 "재연결 중" 배너를 표시한다.

### 14.3 수동 주문 창 (kiwoom-auto `order.js` 결함 수정)

- 시장가 체크박스를 실제로 반영하고, 지정가 입력란과 호가단위 표시를 추가한다.
- 전송은 `order.manual` command로만 하고 confirm 대화상자를 거친다.

---

## 15. 부팅·복구 시나리오

| 상황 | 동작 |
|---|---|
| 정상 재기동 | 선언 상태 로드 → 원장 replay → 대조 일치 → READY(LOCKED). 창·VD·인스턴스·선별 설정 100% 복원 |
| 장중 kill -9 | 위와 같음. SUBMITTING으로 끝난 주문은 UNKNOWN 전환 → 대조로 해소 |
| 원장 해시 깨짐 | SAFE, UI 경고, 원장 사본 보존, 수동 조치 |
| 선언 JSON 손상 | 해당 파일만 복구 모드(읽기 전용, PATCH 409), 기본값으로 부팅, 원본 보존 |
| 대조 불일치 | 외부 진실 채택, `RECONCILE_ADJUST` 기록, PAUSE_ENTRIES |
| 키움 연결 실패 | SAFE에서 재시도, 포지션이 있으면 경고 배너 |
| 애드온 로드 실패 | INCOMPATIBLE/QUARANTINED 표시, 나머지는 READY |

---

## 16. 데이터베이스 DDL

```sql
-- data/ledger.sqlite
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS journal(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  trade_date TEXT NOT NULL,          -- 'YYYYMMDD' KST
  kind TEXT NOT NULL,                -- INTENT, GATE_REJECT, ORDER_NEW, ORDER_SUBMIT, ORDER_ACK, ORDER_UNKNOWN,
                                     -- FILL, CANCEL_REQ, CANCELED, REJECTED, UNKNOWN_RESOLVED, SIGNAL, DECISION,
                                     -- ARM, KILL, RECONCILE, RECONCILE_ADJUST, ADDON_QUARANTINE, FSM_VIOLATION, BOOT
  order_key TEXT,
  code TEXT,
  payload TEXT NOT NULL,             -- JSON
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_j_date_kind ON journal(trade_date, kind);
CREATE INDEX IF NOT EXISTS ix_j_order ON journal(order_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_j_dedupe
  ON journal(trade_date, json_extract(payload, '$.dedupe_key'))
  WHERE kind = 'ORDER_NEW';
CREATE TRIGGER IF NOT EXISTS j_no_update BEFORE UPDATE ON journal
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS j_no_delete BEFORE DELETE ON journal
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- data/history.sqlite
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bars(
  code TEXT NOT NULL,
  tf TEXT NOT NULL,
  ts INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  o INTEGER, h INTEGER, l INTEGER, c INTEGER, v INTEGER, amount INTEGER,
  source TEXT NOT NULL,              -- 'kiwoom' | 'cybos'
  PRIMARY KEY(code, tf, ts, seq)
);
CREATE TABLE IF NOT EXISTS manifest(
  code TEXT, tf TEXT, source TEXT,
  from_ts INTEGER, to_ts INTEGER, rows INTEGER, finished_at INTEGER,
  PRIMARY KEY(code, tf, source)
);
```

---

## 17. 선언 상태 파일 스키마

| 파일 | 주요 키 |
|---|---|
| `workspace.json` | schemaVersion 7, desks[], windows[{id, kind, desk, rect, props, addons{}}], links, activeDesk |
| `risk.json` | 9.4 참조, kill 레벨 포함 |
| `scanner.json` | conditions[{seq, name}], maxCandidates, n, rankIntervalSec, swapMargin, minHoldSec, outGraceSec, filters[{addon, config}], ranker{addon, config}, template{strategy_id, version, tf, params, mode} |
| `strategies.json` | instances[{instance_id, strategy_id, version, code, tf, params, mode: "auto" 또는 "signal"}] |
| `addons.json` | {addon_id: {enabled, config, ...알 수 없는 키 보존}} |
| `calendar.json` | holidays[], special[{date, open, close}] |
| `fees.json` | buyFeeRate, sellFeeRate, sellTaxRate{KOSPI, KOSDAQ}, effectiveFrom |
| `strategies/<id>/<ver>/meta.json` | timeframe, warmup_bars, params schema, status, backtest_report_hash, holdOvernight |

---

## 18. 백테스트

- 종목 단위: `backtest/runner.py`가 history에서 frame을 만들고 dsl backtest/BrokerEmulator로 실행한다. 체결 정책은 `next_open`(기본) 또는 `bar_close`, 수수료·세금은 `fees.json`.
- 리포트: 거래 목록, 승률(청산 거래만 분모, dsl 감사 P1 반영), 손익비, MDD, MFE/MAE, 노출 시간. `artifacts/backtests/<id>.json`에 저장하고 해시를 생성한다.
- 포트폴리오 수준(조건검색·선별 포함 재생)은 v1 범위 밖이다. 대신 페이퍼 원장을 같은 형식으로 집계하는 `report.from_ledger()`를 제공한다.

---

## 19. 보안·운영

- 비밀값은 `.env`에만 둔다. appkey, secretkey, token이 로그에 찍히지 않도록 마스킹 필터를 둔다.
- `127.0.0.1` 바인드, 세션 토큰, Origin 검사(13.1).
- 로그는 `artifacts/logs/`에 날짜별로 회전 저장한다(원장과 별개).
- Windows `start.bat`: venv 활성화 → uvicorn 실행, PORT 기본 8077.

---

## 20. 테스트 게이트

| 게이트 | 내용 | 마일스톤 |
|---|---|---|
| G1 | 애드온 폴더 삭제 상태로 서버·웹 부팅 → READY. 하드 손절·마감 정리 동작 | M0, M4 |
| G2 | 장애 주입: 애드온 예외·타임아웃·무한루프 → 격리, 코어 지속 | M0, M5 |
| G3 | kill -9 재기동 → 상태 100% 복원, ARM은 LOCKED, SUBMITTING은 UNKNOWN 처리 | M4 |
| G4 | parity: 같은 봉으로 백테스트 신호 = 스트리밍 prefix 신호 = 차트 마커 | M5 |
| G5 | FSM: 모든 허용·금지 전이, UNKNOWN 재전송 금지 (hypothesis 상태 테스트) | M3 |
| G6 | 리스크: 9.5 거부 코드를 하나씩 재현 | M3 |
| G7 | 대조: FakeKiwoom 잔고·미체결 불일치 → ADJUST + PAUSE_ENTRIES | M4 |
| G8 | 경계 감사: import-linter, audit_order_tr, 알 수 없는 키 왕복, 웹 ta 계산 grep | M0~ |
| G9 | E2E(Playwright + FakeKiwoom): 조건 편입 → 선별 → 신호 → 주문 → 체결 → 하드 손절 → 마감 | M7 |
| G10 | 모의 서버 6시간 soak: 메모리 증가 없음, WS 재연결 3회 이상 정상 복구, FSM 위반 0 | M9 |

FakeKiwoom(`scripts/fake_kiwoom.py`): REST와 WS를 함께 흉내 낸다. 시나리오 스크립트로 응답 지연, 401/8005, 한도 초과, 주문 타임아웃, 체결 지연·부분체결, 조건 편입·이탈을 주입한다. 모든 통합 테스트의 기반이다.

---

## 21. 확인 필요 항목 (추측 구현 금지)

키움 공식 REST API 문서(키움 OpenAPI 포털)와 모의 서버 실제 응답 캡처로 확정한다. 캡처는 `tests/fixtures/kiwoom/`에 저장한다.

| 번호 | 항목 | 사용처 |
|---|---|---|
| C1 | 주문 kt10000/10001 요청 필드(`dmst_stex_tp`, `stk_cd`, `ord_qty`, `ord_uv`, `trde_tp`, `cond_uv`)와 응답(`ord_no`) | order_gate |
| C2 | 정정 kt10002, 취소 kt10003 필드(원주문번호, 수량) | order_gate |
| C3 | `trde_tp` 코드표(보통 0, 시장가 3으로 알려짐; IOC/FOK/스톱지정가 여부) | spec.py |
| C4 | 잔고·미체결·당일체결 TR id와 응답 필드(kt00018, ka10075, ka10076로 알려짐) | reconcile |
| C5 | WS 실시간 00/04/0B/0D 필드 번호와 의미 | ws.py, bar_store |
| C6 | 조건검색 CNSRLST/CNSRREQ/CNSRCLR 요청·응답·실시간 편입이탈 형식, 동시 실시간 조건식 수 제한 | sources |
| C7 | REST 호출 한도(초당·분당, TR 그룹별)와 초과 응답 코드 | rest.py |
| C8 | WS 세션당 등록 종목 상한, grp_no 사용법 | sub_registry |
| C9 | 분봉 시각이 시작인지 종료인지, 틱 차트 시각 해상도 | parse.py |
| C10 | 2026년 증권거래세율, 키움 수수료율(모의·실전) | fees.json |
| C11 | 호가단위표 현행 여부, 상하한가 필드 위치 | ticks.py |
| C12 | dsl `market` 입력 형식, `backtest.py` 진입 함수, `dsl_semantic.py`·`indicators.py`·`multitimeframe.py` 공개 API | dsl_adapter |
| C13 | Cybos StockChart 요청 제한, 분봉 시각 표기, 수정주가 옵션 | cybos32 |
| C14 | 모의 서버의 시장가, 정정·취소, 부분체결 지원 범위 | G9, G10 |
| C15 | 토큰 유효기간 필드(`expires_dt`)와 갱신 정책 | token.py |

---

## 22. 구현 순서와 완료 기준

| 단계 | 산출물 | 완료 기준 |
|---|---|---|
| M0 | 저장소 골격, contracts, addon_host, isolate, declared_store, journal_store, import-linter, audit 스크립트, 빈 FastAPI + /ws/app hello | G1(서버), G2(isolate 단위), G8 |
| M1 | bridge/kiwoom: spec, parse, token, rest 스케줄러, ws 세션, FakeKiwoom | P3 백필 중 P0 지연 1초 미만, 401 재발급, 재연결 재REG |
| M2 | bar_store, sub_registry, history_store, client_hub, 웹 VD 이식, chart 유닛 core | 차트 열기·VD 이동·재기동 복원, 실시간 봉 append/replace |
| M3 | ledger, order_fsm, order_gate, risk, arm, kill_switch, ticks, pnl, trade 창, 수동 주문 | G5, G6, 모의 서버 수동 주문 1건 왕복 |
| M4 | positions, reconcile, protection, boot 상태기계, synthetic_orders | G3, G7, G1 전체(하드 손절 포함) |
| M5 | engine: dsl_adapter, market_frame, pool, strategy_runtime, indicator_service, 차트 지표·마커, strategy 창 | G4, 서버 계산 지표 표시 |
| M6 | candidates, selection, sources/kiwoom_condition, 기본 필터·랭커·사이저, scanner·selection 창 | 편입·이탈 → 상위 N 갱신, 히스테리시스 테스트 |
| M7 | 파이프라인 연결, exit 애드온(trailing, time_stop), autotrade 관제판, 일일 리포트 | G9, 모의 서버 1거래일 무인 운용 |
| M8 | backtest runner·report, promotion, Cybos 워커, datasource 애드온 | 리포트 해시 → 승격 연결, 야간 배치 1회 성공 |
| M9 | soak, 운영 문서, 실전 전환 체크리스트 | G10, 실전 소액 1주 수동 감시 운용 |

### 22.1 첫 세션 작업 묶음 (M0)

1. 저장소 생성, `vendor/dsl` submodule 추가 및 커밋 고정.
2. `pyproject.toml`: 의존성, import-linter 계약(I1, I4, I9, dsl 단일 진입점).
3. `unit/contracts.py`: 7장 코드 그대로.
4. `unit/isolate.py`: timeout, 실패 카운트, 격리, 원장 이벤트 콜백.
5. `unit/addon_host.py`: `addons/**/manifest.py` 발견 → api 버전 검사 → attach/detach/reattach, 상태 조회 API.
6. `central/state/declared_store.py`: 6장 규칙(StateCorrupt, fsync, 알 수 없는 키 보존).
7. `central/state/journal_store.py`: 16장 DDL, 해시 체인, 단일 writer 태스크, replay iterator.
8. `api/main.py`: `/api/health`, `/ws/app` hello/welcome, 세션 토큰, TrustedHost.
9. `scripts/audit_order_tr.py`: `kt1000[0-3]` 문자열이 `order_gate.py`, `spec.py` 밖에 있으면 실패.
10. 테스트: G1(애드온 디렉터리 없이 부팅), G2 단위(예외·타임아웃 애드온 격리), G8(linter, audit, JSON 왕복), 원장 UPDATE/DELETE 거부.

---

## 23. 참조 저장소 이식 맵

| 출처 | 가져올 것 | 가져오지 않을 것 |
|---|---|---|
| kiwoom-auto | VD·창·속성트리·화면 레지스트리, 워크스페이스 v6 스키마·fixture, `store.py` 원자 저장(수정판), `config.py` .env 패턴, `kiwoom.py` CHARTS 매핑·`_number`/`_stamp`/`_query_symbol`, `data.py` 캐시 키 검증 | 단일 RLock + 1.2초 직렬 호출, 차트 폴링 구조, `strategy.py` MA 크로스(dsl로 대체), 틱 중복 제거, 30일 월봉 판정, price 0 고정 주문 |
| kiwoom-desk | 지표 reset/update 계약, 틱 집계 개념, trnm 직렬화·차단 개념, AutoTradeForm 상태 표기(RUNTIME ONLINE/WAIT, BROKER LOCKED/ARMED), 재시작 시 LOCKED, 주문번호≠체결 원칙, TradeProtectionRuntime 개념, `import.meta.glob` 선택 로드, VWAP-JMA Reclaim 검증용 참조 전략 | 탭별 WS 업스트림, 범용 프록시 `/api/kiwoom/call`, 평문 토큰 캐시, 브라우저 측 지표·전략 계산 |
| dsl | 엔진 전체(submodule): 파서, 의미 분석, 런타임, bar_runtime, MTF, BrokerEmulator, function_registry, 예제 전략 | 연구 계층(tactical_*, stage1_*, evolution, LLM memory)은 서버에서 import하지 않음 |

dsl 감사 문서의 미해결 과제(부분체결, 피라미딩, on_tick 롤백 등)는 v1 범위 밖이다. 부분체결은 실거래 FSM이 처리하고 dsl은 신호만 담당하므로 v1은 해당 기능 없이 동작한다.
