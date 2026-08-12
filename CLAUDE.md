# auto2 — 자동 트레이딩 봇

바이낸스(Binance) 대상 자동 트레이딩 봇. 리서치 → 레짐 감지 → 배분 → 리스크 검증 → Claude 의사결정 → 주문 실행 → 저널링 순서로 동작.

새 세션을 시작할 때는 `UPDATE_LOG.md`의 최근(위쪽) 항목 몇 개를 먼저 읽을 것 — 최근 발견된 실전 버그,
진행 중이던 전략 실험, 사용자에게 아직 전달 안 한 후속 조치 등이 여기 시간순으로 기록되어 있다.

## 규칙
- 기본값은 항상 테스트넷(`USE_TESTNET=true`). 실거래 전환은 사용자 명시적 승인 필요.
- API 키는 `.env`에만 저장, 절대 커밋하지 않음.
- 리스크 관리(`src/core/risk.py`)를 거치지 않은 주문은 실행 금지.
- 새 모듈 추가 시 `tests/`에 대응 테스트 작성.

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
- 선물은 현물과 달리 `enable_demo_trading(True)`로 연결한다(구 testnet.binancefuture.com 방식인
  `set_sandbox_mode`가 아님). 키는 실제 바이낸스 계정 로그인 후 demo.binance.com/en/my/settings/api-management 에서 발급.
- `client.fetch_my_trades(symbol, limit=N)`을 `since` 없이 부르면 "최신 N개"가 아니라 계좌에 쌓인
  체결 중 **가장 오래된 N개**가 돌아온다(실전에서 확인된 동작). `check_and_log_closed_trade`가 이걸
  간과해서 BTC 청산을 진입 체결로 오인해 손익을 0으로 잘못 기록한 사고가 실제 있었음(2026-08-12,
  UPDATE_LOG.md 참고) — 이제는 진입 시각을 `since`로 앵커링해서 고쳐놨다. 거래소 체결 내역을 다시
  조회하는 코드를 새로 짤 땐 항상 `since`를 명시할 것.
