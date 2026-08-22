# auto2 — 자동 트레이딩 봇

바이낸스(Binance) 대상 자동 트레이딩 봇. 리서치 → 레짐 감지 → 배분 → 리스크 검증 → Claude 의사결정 → 주문 실행 → 저널링 순서로 동작.

새 세션을 시작할 때는 `UPDATE_LOG.md`의 최근(위쪽) 항목 몇 개를 먼저 읽을 것 — 최근 발견된 실전 버그,
진행 중이던 전략 실험, 사용자에게 아직 전달 안 한 후속 조치 등이 여기 시간순으로 기록되어 있다.

## 규칙
- 기본값은 항상 테스트넷(`USE_TESTNET=true`). 실거래 전환은 사용자 명시적 승인 필요.
- API 키는 `.env`에만 저장, 절대 커밋하지 않음.
- 리스크 관리(`src/core/risk.py`)를 거치지 않은 주문은 실행 금지.
- 새 모듈 추가 시 `tests/`에 대응 테스트 작성.
- **선물 규칙봇/대시보드는 데모와 실계좌를 동시에 운영한다**(2026-08-22) — 아래 "데모/실계좌 동시
  운영" 절 참고. 전역 `USE_TESTNET`이 아니라 함수마다 명시적으로 넘기는 `env: "demo"|"live"`가
  이제 진짜 계정 여부를 결정한다.

## 구조
- `src/data/` — 거래소 연동, 시세/지표 수집 (`exchange.py`=현물, `futures_exchange.py`=선물)
- `src/core/` — 레짐 감지, 포트폴리오 배분, 리스크 로직 (`risk.py`=현물, `futures_risk.py`=레버리지)
- `src/core/decision.py` / `futures_decision.py` — Claude 의사결정 (현물 리밸런싱 / 선물 롱숏)
- `src/pipeline.py` — 현물 리밸런싱 파이프라인 (BTC/ETH 다중 종목)
- `src/futures_pipeline.py` — 선물 롱숏, Claude가 매 사이클 판단 (구버전 경로, 현재는 미사용 — 아래 참고)
- `src/futures_rule_bot.py` — 선물 롱숏, **순수 규칙 기반 상시 감시** (현재 쓰는 경로, Claude 미사용)
- `src/core/futures_strategy.py` — 규칙 기반 신호 탐지(ADX+SMA20돌파+RSI), 손절/익절가 계산
- `src/execution/` — 주문 실행, 포지션 추적 (`orders.py`=현물, `futures_orders.py`=선물)
- `dashboard/app.py` — 선물 봇 상태를 보여주는 Flask 웹 대시보드 (http://127.0.0.1:5055)
- `src/telegram_bot.py` / `scripts/run_telegram_bot.py` — 텔레그램 알림 + 원격 시작/중지 (아래
  "텔레그램 알림" 절 참고), `src/data/public_ip.py` — 공인 IP 조회(대시보드와 공유)
- `journal/trades.jsonl` — 현물, `journal/futures_trades.jsonl` — 선물(Claude), `journal/futures_rule_trades.jsonl` — 선물(규칙기반)

## 선물(레버리지) 관련 특히 주의할 것
- **현재 쓰는 건 `futures_rule_bot.py`(규칙 기반)다.** `futures_pipeline.py`/`futures_decision.py`(Claude 기반)는
  사용자 요청으로 순수 규칙 기반으로 전환하면서 남겨둔 이전 경로 — 새 기능은 `futures_rule_bot.py` 쪽에 추가할 것.
- 손절폭은 `STOP_LOSS_PCT`, 익절은 손절폭×`TAKE_PROFIT_RR`로 코드가 고정 결정한다.
- 포지션 진입은 항상 반대방향 reduceOnly STOP_MARKET(손절) + TAKE_PROFIT_MARKET(익절) 주문을 동시에 건다
  (`futures_orders.open_position_with_bracket`).
- 이 두 주문은 바이낸스의 algo/conditional 주문 계열이라 `cancel_all_orders(symbol)` 한 번으로는 안 지워진다 —
  `params={"trigger": True}`를 추가로 호출해야 한다 (`futures_orders.cleanup_stale_orders` 참고, 실전에서
  버그로 실제 발견되어 고친 부분).
- SL/TP 중 하나가 체결되면 나머지가 고아로 남으므로, 감시 루프(`futures_rule_bot.run_once`)는 포지션이 없을
  때마다 매번 `cleanup_stale_orders`를 방어적으로 호출한다.
- 진입 전 `check_stop_before_liquidation`으로 손절가가 청산가보다 충분히 여유 있는지 검증한다.
- **신호 계산은 반드시 마감된 캔들만 써야 한다.** `fetch_ohlcv_df`가 돌려주는 마지막 봉은 바이낸스가
  아직 마감 안 된(진행 중인) 캔들을 실시간 종가로 계속 갱신해서 주는 것이라, `_evaluate_symbol`이
  이걸 그대로 신호/진입가 계산에 쓰면 그 시간봉이 끝나기 전에 반전될 일시적 스파이크에도 SMA
  돌파처럼 반응해버린다 — 반면 백테스트(`run_backtest`)는 항상 이미 마감된 과거 캔들만 순회하므로
  이 노이즈를 원천적으로 재현 못 한다(2026-08-22, 실계좌 3연패 전부 이게 원인으로 확인됨). 그래서
  `fetch_ohlcv_df(..., limit=101)`로 한 봉 여유를 더 받은 뒤 `df.iloc[:-1]`로 마지막(진행중) 봉을
  버리고서 `detect_signal`/`entry_price` 계산에 쓴다 — 백테스트가 검증한 조건과 정확히 같아짐.
  새로 캔들 조회하는 코드를 짤 땐 항상 이 트리밍을 잊지 말 것.
- 선물은 현물과 달리 `enable_demo_trading(True)`로 연결한다(구 testnet.binancefuture.com 방식인
  `set_sandbox_mode`가 아님). 키는 실제 바이낸스 계정 로그인 후 demo.binance.com/en/my/settings/api-management 에서 발급.
- `client.fetch_my_trades(symbol, limit=N)`을 `since` 없이 부르면 "최신 N개"가 아니라 계좌에 쌓인
  체결 중 **가장 오래된 N개**가 돌아온다(실전에서 확인된 동작). `check_and_log_closed_trade`가 이걸
  간과해서 BTC 청산을 진입 체결로 오인해 손익을 0으로 잘못 기록한 사고가 실제 있었음(2026-08-12,
  UPDATE_LOG.md 참고) — 이제는 진입 시각을 `since`로 앵커링해서 고쳐놨다. 거래소 체결 내역을 다시
  조회하는 코드를 새로 짤 땐 항상 `since`를 명시할 것.

## 데모/실계좌 동시 운영 (2026-08-22)
- `get_futures_client(env="demo"|"live")` — env="live"는 `BINANCE_FUTURES_LIVE_API_KEY/SECRET`
  (binance.com 실제 계정 키, demo.binance.com 아님)로 진짜 자금 계좌에 붙는다. 키가 없으면
  `LiveKeysNotConfiguredError`를 던진다 — 데모 키로 조용히 폴백하지 않는다.
- `futures_rule_bot.run_once(client, env=..., journal_path=..., state_path=..., last_trade_path=...)` —
  경로들을 안 넘기면 env에 따라 데모 상수(`JOURNAL_PATH` 등) 또는 라이브 상수(`LIVE_JOURNAL_PATH` 등,
  전부 `.live.` 인픽스)로 자동 해석된다. 데모 파일은 이름이 그대로라 기존 이력이 안 끊긴다.
- `env="live"`에서 자동 진입은 `open_position_with_bracket(..., env="live", confirm_live=True)`로
  실제 주문을 낸다 — `_guard_live`가 `env != "live"`면 애초에 막을 필요가 없어서 통과시키고,
  `env="live"`인데 `confirm_live=False`면 막는다. 이 프로세스가 애초에 실계좌 client로 실행되고
  있다는 것 자체가 명시 의도이므로 매 진입마다 다시 확인하지 않는다.
- `scripts/run_futures_bot.py --env demo|live` — 두 env를 완전히 독립된 OS 프로세스로 동시에
  띄운다. `bot_process.py`는 PID 파일을 env별로 분리(`state/futures_bot.pid` / `.live.pid`)하고,
  `_is_our_bot_process`가 cmdline에서 스크립트 이름뿐 아니라 `--env {env}`까지 확인해서 데모/실계좌
  프로세스가 서로 뒤바뀌지 않게 한다.
- 대시보드는 모든 `/api/*` 라우트가 `?env=demo|live` 쿼리로 계좌를 고른다(기본 demo). 페이지는
  항상 데모로 로드되고, 새로고침해도 LIVE 탭이 기억되지 않는다(의도적 — 계좌 착각 방지).
  `env=live`인데 라이브 키가 없으면 서버가 안 죽고 400으로 명확히 응답한다.
- `FUTURES_SYMBOLS`에 `SOXL/USDT:USDT`를 다시 추가했다 — 실거래 마켓엔 있지만 데모엔 없어서
  `initialize()`/`_available_symbols()`의 `client.markets` 필터링으로 데모에선 조용히 빠지고
  실계좌 연결 시에만 활성화된다(코드 변경 없이 리스트 추가만으로 동작).

## 텔레그램 알림 + 원격 시작/중지 (2026-08-22, 대시보드 제어 버튼은 2026-08-22 후속)
- `scripts/run_telegram_bot.py`가 데모/실계좌 봇(`run_futures_bot.py`)과는 별개의 상시 프로세스로
  돈다 — 존재 이유 자체가 "대시보드를 안 보고 있을 때도 동작"이므로 관심사가 다르다. 다만 프로세스
  관리(스폰/PID 검증/graceful stop)는 `bot_process.py`에 `"telegram"`이라는 세 번째 key로 얹혀서
  대시보드의 "텔레그램 봇" 카드로 시작/중지 가능(터미널에서 직접 실행해도 되고, Windows 작업
  스케줄러 로그온 시작으로 등록해도 됨 — 셋 다 같은 PID 파일/검증 로직을 공유하므로 서로 안전하게
  겹쳐 씀). `bot_process.py`는 원래 `env`("demo"/"live")만 다루다가 이때 `key`로 일반화됨
  (`_launch_args(key)`/`_match_tokens(key)`가 key별로 스폰 인자·cmdline 검증 방식을 분기).
  `src/telegram_bot.py`가 테스트 가능한 핵심 로직(`futures_rule_bot.py`+`run_futures_bot.py`와
  같은 2계층 패턴), `scripts/run_telegram_bot.py`는 얇은 무한루프 진입점.
- `/status` 명령은 데모/실계좌 각각 실행 여부·하트비트·마진 자산에 더해 **보유 포지션마다 진입가·
  현재가·미실현손익·손절가·익절가**까지 보여준다(`format_status`). 가격 자릿수는 대시보드
  `index.html`의 `fmtPrice()`와 같은 기준(`telegram_bot._fmt_price`)을 파이썬으로 맞춰서 통일.
- `src/execution/telegram_client.py`의 `send_message`/`get_updates`는 절대 예외를 안 올린다(실패
  시 로그만 남기고 False/빈 리스트) — 알림 실패가 감시 루프를 죽이면 안 되므로.
- 저널의 `entered`/`closed`/`circuit_breaker_blocked` 이벤트만 알림 대상(`src.telegram_bot.
  _NOTIFY_EVENTS`) — `rejected_exchange_error` 등은 매 사이클 반복될 수 있어 노이즈라 제외.
- `state/telegram_bot_state.json` 하나에 데모/실계좌 각각의 마지막 처리 저널 타임스탬프, 봇
  실행여부, 마지막 공인 IP, 텔레그램 update_offset을 전부 담는다(다른 파일이 안 읽는 새 관심사라
  여러 파일로 안 쪼갬). **최초 실행 시 과거 이력을 한꺼번에 쏘지 않는다** — state가 비어있으면
  "지금 상태"만 기록하고 알림은 다음 변화부터.
- `/start_demo`, `/start_live` 등 원격 명령은 `TELEGRAM_CHAT_ID`와 일치하는 발신자만 처리한다
  (`is_authorized`) — 불일치는 응답도 없이 무시(봇 존재/동작을 노출하지 않기 위함). 이건 사용자가
  거절했던 "추가 안전 스위치"와는 다른, 원격 명령 인터페이스의 최소 접근 제어라 선택사항이 아님.
- 원격 시작은 데모/실계좌 둘 다 허용(사용자 명시 확인, 확인 질문으로 물어봄) — `/start_live`도
  `/start_demo`와 동일하게 즉시 실행되고 추가 확인 절차는 없다. `bot_process.start(env)`/
  `stop(env)`를 그대로 호출하므로 대시보드 버튼과 완전히 동일하게 동작한다.
- `src/data/public_ip.py`(대시보드 `/api/public-ip`가 쓰던 캐싱 로직을 추출)를 대시보드와
  텔레그램 봇이 공유 — 새 IP가 감지되면(TTL 120초) 텔레그램으로도 알림.
