# auto2 — 바이낸스 선물 자동 트레이딩 봇

USDT-M 선물(레버리지 롱/숏)을 **순수 규칙 기반**으로 상시 감시한다(외부 API 판단 없음):
신호 탐지 → 리스크 검증 → 진입 + 손절/익절 브라켓 → 저널링 → 대시보드/텔레그램.
데모 계좌와 실계좌를 독립 프로세스로 동시에 운영한다.

새 세션을 시작할 때 읽는 순서:
1. **`docs/OBJECTIVE.md`** — 이 프로젝트의 목표(**월 +1% 자산 수익률을 복리로**, 2026-09-26에 +10%에서 개정)와 지금 거기서 얼마나
   떨어져 있는지. **모든 작업은 이 거리를 줄이는지로 판단한다.** 측정 단위는 R이 아니라 자산이다.
2. **`NEXT_STEPS.md`** — 그래서 지금 무엇을 하는가. 실험 루프의 현재 위치, 손대면 안 되는 함정.
3. `UPDATE_LOG.md`의 최근(위쪽) 항목 몇 개 — 최근 발견된 실전 버그, 진행 중이던 전략 실험,
   사용자에게 아직 전달 안 한 후속 조치가 시간순으로 있다.

**2026-09-24에 `IMPROVEMENT_RECOMMENDATIONS.md`를 삭제했다** — 3개 권고가 전부 반영됐거나
낡았기 때문이다(과최적화 검증 → `docs/BACKTEST_PROTOCOL.md`, 미마감 캔들 → 2026-08-22에 이미
수정됨, 저널 읽기 캐시 → 2026-09-12). git 이력에 그대로 있다.

**2026-09-12에 안 쓰는 계열 3개를 삭제했다**(현물 리밸런싱 전체, Claude 기반 선물 파이프라인,
대안 전략 실험 모듈 4종과 일회성 스윕 스크립트). 지금 이 저장소에 Claude API 호출은 없고
`anthropic` 의존도 없다. 옛 코드가 필요하면 git 이력에 그대로 있다(`git log -- src/pipeline.py`).

## 규칙
- 기본은 항상 데모 계좌(`env="demo"`). 실계좌 전환은 사용자 명시적 승인 필요.
- API 키는 `.env`에만 저장, 절대 커밋하지 않음.
- 리스크 관리(`src/core/risk.py`의 서킷브레이커, `futures_risk.py`의 사이징/청산가 검증)를 거치지
  않은 주문은 실행 금지.
- 새 모듈 추가 시 `tests/`에 대응 테스트 작성.
- **데모와 실계좌를 동시에 운영한다**(2026-08-22) — 아래 "데모/실계좌 동시 운영" 절 참고.
  전역 설정이 아니라 함수마다 명시적으로 넘기는 `env: "demo"|"live"`가 계정을 결정한다
  (옛 현물 시절의 `USE_TESTNET`은 2026-09-12에 제거됨).

## 구조
- `src/data/futures_exchange.py` — 거래소 연동(클라이언트 생성, 잔고/포지션/레버리지 조회) +
  캔들 조회(`fetch_ohlcv_df`, `OHLCV_COLUMNS`). **마지막 봉은 아직 마감 안 된 봉이다**(아래 주의).
- `src/core/` — 전략/리스크 로직. `futures_strategy.py`(신호·필터·브라켓가), `futures_risk.py`
  (레버리지 사이징·청산가), `risk.py`(서킷브레이커 임계치), `indicators.py`(ADX/RSI/SMA/ATR),
  `config.py`(.env 설정), `signal_status.py`(조건 근접도), `state.py`(연속손실·일일손익)
- `src/futures_rule_bot.py` — 감시 루프 본체(테스트 가능한 핵심 로직), `scripts/run_futures_bot.py`는
  얇은 무한루프 진입점. 이 2계층 패턴을 텔레그램 봇도 같이 쓴다.
- `src/backtest/` — 백테스트 하네스(`engine`/`data`/`report`/`optimize`). 실거래와 **같은**
  `futures_strategy`를 호출한다 — 전략 조건을 여기 따로 적지 말 것. `run_backtest`의 기본값이
  실거래 설정이다(수수료·레짐 필터·저변동 필터 전부, 2026-09-22) — 인자를 안 넘기면 실거래와
  같은 규칙을 돈다. `report.portfolio_stats`는 종목별 거래를 청산 시각 순으로 한 곡선에 합쳐
  **포트폴리오 낙폭**을 낸다(`aggregate_stats`의 `max_drawdown_r`은 종목별 최악값일 뿐이다).
- `docs/BACKTEST_PROTOCOL.md` + `src/backtest/gate.py` + `scripts/run_experiment.py` +
  `docs/experiments.tsv` — **실험 프로토콜**(2026-09-24, 로드맵 Phase 0). 파라미터를 바꿔 보는
  일은 전부 이 경로로 한다. 문서가 규칙, `gate.py`가 **탐색 중에 고치지 않는 심판**,
  실행기가 한 건을 판정, `experiments.tsv`가 지워지지 않는 원장이다. 존재 이유: 스윕 표에는
  "그 결론이 몇 번째 시도인지"가 안 남고, 30칸에서 최고를 고르는 것은 30번의 시도라 잡음에서도
  그럴듯한 칸이 나온다. 세 가지를 코드가 강제한다 — 선언 안 된 파라미터 거부, **최근 90일
  홀드아웃 봉인**(탐색 구간 PASS + `--confirm-holdout` 없이는 안 열림), 20회 초과 시 다중비교
  경고. 기준값은 문서와 `gate.py` 두 곳에 있고 `test_backtest_gate.py`가 일치를 고정한다 —
  **기준을 바꾸려면 `GATE_VERSION`을 올리고 버전이 다른 원장 행끼리 비교하지 말 것.**
- `src/execution/export_log.py` / `scripts/export_trade_log.py` — 저널을 분석용 CSV로 내보낸다
  (`exports/`). 결합은 `performance.resolve_closed_trades`를 그대로 불러서 하므로 CSV와 대시보드가
  같은 수를 말한다.
- `src/execution/cost_report.py` / `scripts/report_execution_costs.py` — 실거래 **체결 비용 분포**
  (2026-09-28). 진입 슬리피지(신호 봉 종가 대비), **청산 슬리피지(손절/익절 발동가 대비)**, 수수료,
  그 합을 평균·p90·최악으로. 거래별 값은 `export_log`가 계산한다(`adverse_slippage_r` — 양수가
  손해). 익절 조건주문은 발동 뒤 시장가라 되돌림에서 체결돼 평균 0.076R을 잃는다(손절은 0.005R).
- `src/backtest/sizing.py` / `scripts/run_risk_sizing.py` — R 결과를 **거래당 리스크 x%의 복리
  자산 경로**로 환산(월 수익률·최대 낙폭·월 1%로 회복 기간). 리스크 비율은 결과의 크기만 바꾸고
  부호는 못 바꾼다. 일일 손실 한도(자산 %)의 R 환산이 비율마다 달라 비율마다 다시 시뮬레이션한다.
- `src/execution/futures_orders.py` — 주문 실행(브라켓 동시 발주, 고아 주문 정리)
- `dashboard/app.py` — 선물 봇 상태를 보여주는 Flask 웹 대시보드 (http://127.0.0.1:5055).
  화면은 `templates/index.html`(구조) + `static/dashboard.css` + `static/dashboard.js`로 나뉜다
  (2026-09-28 개편 — 거래소형 레이아웃, 휴대폰 폭에서는 아래 메뉴로 화면 전환). 차트는 외부
  라이브러리 없이 canvas로 직접 그린다. 새 API: `/api/tickers`(현재가·24h), `/api/equity`(날짜별
  자산 + 30일 수익률 vs 월 +1%), `/api/chart?tf=`(`CHART_TIMEFRAMES`만 허용, 시각은 epoch ms).
  - **휴대폰 접속**: `DASHBOARD_HOST=0.0.0.0` + `DASHBOARD_TOKEN`. 127.0.0.1에서 온 요청만 토큰 없이
    통과하고(프록시 헤더가 붙어 있으면 로컬로 안 믿음), 나머지는 `/login`에서 토큰 입력 → HMAC 쿠키.
    토큰 없이 PC 밖 주소로 열면 `check_bind_is_safe`가 시작을 거부한다 — 긴급청산·실계좌 봇 시작
    버튼이 있어서. 새 라우트를 추가해도 `before_request`가 자동으로 막으므로 따로 할 일은 없다
    (인증 없이 열어야 하는 경로만 `_PUBLIC_PATHS`에 넣을 것).
- `src/core/signal_status.py` — "진입 조건에 지금 얼마나 가까운지" 계산. 대시보드
  `/api/conditions`와 텔레그램 `/conditions`가 **같은 함수**를 쓴다(조건을 두 군데 적으면
  실거래 로직과 어긋나므로). 실제 판정은 `futures_strategy`를 그대로 호출하고 거리/점수만 덧붙인다.
- `src/execution/performance.py` — 저널 → 대시보드 성과 요약. `summarize_performance`(달러 기준),
  `summarize_r_performance`(R배수 + 자산곡선 + 방향별), `summarize_recent_issues`(거래소 거부
  24시간 집계), `summarize_day`(일일 요약 푸시용). **R 복원은 `resolve_closed_trades` 한 곳에서
  한다** — 2026-09-09 이전 청산 기록엔 방향도 손절가도 없어서 진입 기록에서 끌어와야 하고,
  R과 실현손익의 부호가 모순되면 그 R은 버린다(아래 주의 참고).
- `src/execution/equity_log.py` — **날짜별 마진 자산(시작/종료)**. 감시 루프가 하트비트와 같은
  자리에서 매 사이클 남긴다(하트비트는 마지막 한 순간만 덮어써서 "어제 자산이 얼마였나"를 답
  못 한다). 존재 이유(2026-09-24): 일일 요약이 `realized_pnl`만 말해서 "수익 +5 USDT"라고
  알리는데 계좌 총자산은 그대로이거나 줄어 있는 일이 있었다 — 거래소의 `realizedPnl`에 수수료가
  안 들어있고, 실현손익은 미실현 변동·펀딩비·입출금을 못 담기 때문이다. **자산만이 계좌 화면의
  숫자와 직접 맞춰볼 수 있는 값이고, 목표(월 +1%)도 자산 기준이라 `period_return`이 그걸
  낸다.** 서킷브레이커가 쓰는 `state/futures_rule_daily_equity*.json`과 파일을 나눈 이유: 그쪽은
  오늘의 시작 자산 한 줄만 들고 날짜가 바뀌면 덮어쓰는 구조라, 이력을 얹으면 일일 손실 한도
  계산이 읽는 형식이 바뀐다.
- `src/execution/filter_stats.py` — 저널에 안 남는 진입 차단 사유(레짐숏/저변동/같은봉/가격이탈)의
  **일별 집계**. 저널에 못 남기는 이유(30초마다 재발생)는 그대로라 카운터 파일에 따로 쌓는다.
- `src/execution/excursion.py` — 보유 중 최고/최저 지점(MFE/MAE)을 R배수로 추적. 청산 시
  `check_and_log_closed_trade`가 꺼내서 청산 기록에 옮겨 적는다.
- `src/telegram_bot.py` / `scripts/run_telegram_bot.py` — 텔레그램 알림 + 원격 시작/중지 (아래
  "텔레그램 알림" 절 참고), `src/data/public_ip.py` — 공인 IP 조회(대시보드와 공유)
- `src/execution/journal.py` — 저널 읽기/쓰기. `read_entries`는 **파일의 (mtime, 크기)로 무효화되는
  읽기 캐시**를 들고 있다(2026-09-12) — 사이클 하나가 같은 저널을 10~20번 다시 훑는 구조라
  저널이 커질수록 그 비용만 늘었다. 돌려주는 리스트는 캐시와 같은 객체이므로 **호출자는 읽기만
  할 것**(값을 바꿔야 하면 `performance.resolve_closed_trades`처럼 `dict(entry)`로 복사한다).
- `journal/futures_rule_trades.jsonl` — 데모 저널, `.live.jsonl` — 실계좌 저널(전부 `.live.` 인픽스로 분리).
  `journal/trades.jsonl`과 `futures_trades.jsonl`은 2026-09-12에 삭제한 현물/Claude 경로가 남긴 과거
  기록이라 읽는 코드가 더는 없다 — 이력 보존용으로만 남겨뒀다(gitignore 대상).

## 선물(레버리지) 관련 특히 주의할 것
- 손절폭은 `STOP_LOSS_PCT`, 익절은 손절폭×`TAKE_PROFIT_RR`로 코드가 고정 결정한다.
- **수수료는 이 전략에서 오차항이 아니라 기대값과 같은 크기다**(2026-09-22). 손절폭 1.25%에서
  왕복 수수료의 R 환산은 `2 × 편도율 / 손절폭%` — 편도 0.04%면 0.064R이고, 실거래 저널의 건당
  평균 R이 데모 +0.02R / 실계좌 +0.06R이다. **저널의 총R(데모 +2.50, 실계좌 +6.64)은 수수료를
  빼면 -5.25 / -0.12로 부호가 바뀐다.** 그래서 대시보드는 순R을 앞에 놓고 수수료 전 값을 뒤에
  적는다(`performance.summarize_r_performance`의 `total_net_r`).
  - 이유 둘: (1) **바이낸스의 `realizedPnl`에는 수수료가 안 들어있다** — `commission`이 별개
    필드다(실계좌 체결 내역으로 확인: CRCL 청산 한 건이 realizedPnl -48.23에 수수료 1.62가
    따로). (2) 저널의 `realized_r`은 **신호 봉 종가**를 진입가로 써서 잰 값이라 진입 슬리피지도
    빠져 있다(주문 생성 응답에는 체결가가 안 담겨 온다 — `avgPrice`가 `"0.00"`으로 온다).
  - 이제 청산 기록에 실측 `entry_fee`/`exit_fee`/`total_fee`/`fee_r`/`net_realized_r`과 **실제
    진입 체결가**(`actual_entry_price`)를 남긴다(`_aggregate_closing_trades`가 같은
    `fetch_my_trades` 응답에서 뽑으므로 추가 조회 없음). 그 이전 기록은 `FEE_PCT_PER_SIDE`와
    손절폭으로 추정하고, 추정임을 화면에 밝힌다.
  - **실계좌 실측 수수료는 편도 0.05%**(테이커 기본 등급, 2026-09-28)이고 `FEE_PCT_PER_SIDE`
    기본값도 0.0005다(그 전 0.0004). 바꾸면 백테스트 결과가 바뀌므로 `GATE_VERSION`을 같이 올린다(v2).
  - 수수료율의 정의는 `config.FEE_PCT_PER_SIDE` **한 곳**이다 — 예전엔 스크립트 7개에 각각
    `0.0004`로 복사돼 있었고 `run_backtest`의 기본값은 그와 무관하게 `0.0`이었다.
- 포지션 진입은 항상 반대방향 reduceOnly STOP_MARKET(손절) + 익절 주문을 동시에 건다
  (`futures_orders.open_position_with_bracket`). **익절은 2026-09-28부터 익절가에 걸어 두는
  reduce-only 지정가**다(`TAKE_PROFIT_ORDER_TYPE=limit`, `market`이면 옛 TAKE_PROFIT_MARKET).
  조건부 시장가 익절은 발동 뒤 시장가라 되돌림 순간에 체결돼 평균 0.076R을 잃었다(손절은 0.005R).
  지정가 익절은 **algo가 아니라 일반 주문 목록**에 있다 — 익절가를 읽는 코드는
  `get_bracket_prices`를 쓸 것(두 목록을 다 본다). 백테스트도 같은 규칙이다(`engine.exit_pnl_r`:
  익절 청산만 메이커 수수료·청산 슬리피지 없음).
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
- **같은 신호 봉으로는 두 번 진입하지 않는다**(2026-09-07 추가). 미마감 캔들 트리밍 때문에
  `detect_signal`이 보는 마지막 마감 봉은 그 시간봉 내내 고정이고, 포지션이 도중에 청산되면
  같은 신호가 `POLL_INTERVAL_SECONDS`마다 계속 재발생해 재진입이 무한 반복된다(실계좌 ZEC가
  한 시간에 같은 값으로 5번 진입, 수수료만 태운 왕복으로 3분 만에 연속손실 5회 → 서킷브레이커).
  `_evaluate_symbol`이 신호 봉 시각을 `signal_bar_timestamp`로 저널의 `entered` 기록에 남기고,
  같은 값이면 `skipped_same_signal_bar`로 건너뛴다 — 백테스트(`run_backtest`)가 봉당 한 번만
  진입하는 것과 의미론을 맞추는 것이 목적. 새 진입 경로를 만들면 이 잠금을 반드시 같이 걸 것.
- **이 전략의 엣지는 체결 비용보다 작다**(2026-09-22 측정). 365일 12종목 포트폴리오에서 건당
  기대값이 +0.121R인데, 편도 슬리피지를 **0.05R**(가격으로 6bp)만 넣으면 총R이 +127.6 → +22.3로
  떨어지고 4분할 OOS가 깨진다. 0.10R이면 -83R이다. 수수료만 2배로 해도 4분할이 깨진다
  (`run_backtest(slippage_r_per_side=...)`로 재현 가능).
  - **그런데 `MAX_ENTRY_PRICE_DRIFT_R`이 0.5다** — 전략이 죽는 수준의 10배를 구조적으로 허용한다.
  - 비용이 R에서 차지하는 비중은 `1/손절폭%`에 비례한다 — 손절 1.25%처럼 좁으면 수수료·슬리피지가
    전부 증폭된다. 손절폭을 건드리는 변경은 비용 구조 자체를 바꾸는 것이므로 전략 재검증이 필요하다.
  - **롱/숏 비중은 원인이 아니다.** 실거래 6주는 롱 +43R/숏 -43R이지만 365일 백테스트는 반대로
    숏이 총R의 2/3(+87.3R)이고 롱 온리는 총R이 낮고 낙폭은 더 크며 한 구간이 -38R로 FAIL이다.
    실거래 표본(숏 94건, 상승장 6주)으로 숏을 막으면 안 된다.
- **진입 직전 신호가-현재가 괴리를 검사한다**(`MAX_ENTRY_PRICE_DRIFT_R`, 기본 0.5). 손절/익절가가
  전부 신호 봉 종가 기준이라 현재가가 손절폭의 절반 넘게 벌어지면 그 브라켓은 이미 무의미하다 —
  진입 즉시 손절되거나 브라켓이 -2021 "Order would immediately trigger"로 거부된다. 현재가는
  티커를 새로 조회하지 않고 트리밍 전 마지막(진행중) 봉의 종가를 쓴다.
- **저변동 구간에서는 진입하지 않는다**(`MIN_ATR_TO_STOP_RATIO`, 기본 0.64 — 0이면 필터 off).
  마감봉 기준 ATR이 손절폭의 0.64배 미만이면 `skipped_low_volatility`로 건너뛴다. 이유: 손절
  1.25%/익절 2.5%인데 ATR이 0.5%면 익절까지 5 ATR을 가야 해서 사실상 도달이 어렵고, 대신 시간이
  흐르며 손절로 흘러간다(후보 신호 1999건 특성 분해에서 일관된 손실 구간으로 확인, 2026-09-09).
  **ATR% 절대값이 아니라 손절폭 대비 비율**로 쓰는 게 중요하다 — `STOP_LOSS_PCT`를 바꿔도 조건이
  자동으로 따라온다. 규칙의 정의는 `futures_strategy.passes_volatility_floor()` 한 곳이고
  실거래 봇·조건 근접도 화면(`signal_status`)·백테스트 엔진(`run_backtest`의
  `min_atr_to_stop_ratio` 인자, 기본값이 실거래값)이 전부 이걸 부른다. **2026-09-22까지 이
  필터는 어떤 백테스트 경로에도 없었다** — 그래서 그 전의 모든 백테스트 수치는 실거래가
  거부하는 진입을 포함한 다른 전략의 결과다.
- **`config.py`가 근거로 인용했던 포트폴리오 시뮬레이션 코드는 저장소에 없었다**(2026-09-22).
  `MIN_ATR_TO_STOP_RATIO=0.64`와 `MAX_CONCURRENT_POSITIONS=8`의 주석은 "12종목·1h·365일,
  동시보유/서킷브레이커 반영, 배정순서 50회"를 인용하지만 그 코드가 git 전체 이력에 없었다
  (`git log --all -S"배정순서"`가 config.py 주석만 찾는다). **`src/backtest/portfolio.py`와
  `scripts/run_portfolio_sweep.py`가 그 구멍을 메운다** — 이제 두 값을 다시 검증할 수 있다.
- `src/backtest/portfolio.py` — **여러 종목을 한 계좌에서** 굴리는 통합 진입 시뮬레이션.
  종목별 `run_backtest`를 합산하는 것으로는 안 되는 이유가 두 개다:
  - 동시보유 한도가 진입을 버리면 그 종목의 **이후** 거래 순서까지 달라진다(포지션을 안 들고
    있으면 다음 신호를 받는다) — 사후에 거래 목록을 걸러내는 방식으로는 재현 불가능하다.
  - **서킷브레이커는 청산 시각 기준으로만 갱신해야 한다.** 진입 시각 기준으로 세면 동시보유를
    늘릴수록 좋아지는 것처럼 보인다(2026-09-09에 이 오류로 상한 12를 잘못 권고했다).
  진입 후보는 `engine.gated_signals`, 청산은 `engine._check_exit`을 그대로 쓴다 — 단일 종목
  백테스트와 **같은 함수**라 규칙이 갈라질 수 없다. 같은 봉의 다중 신호 배정 순서는 결과를
  바꾸는 임의적 선택이라 `simulate_many`로 여러 seed를 돌려 **분포**를 본다(한 번의 숫자를
  결론으로 쓰면 그 임의성을 성과로 착각한다). 일일 손실 한도는 자산 %를 R로 환산한다
  (`daily_loss_limit_r`, 5%/0.75% = 6.67R).
- `engine.gated_signals(df, ...)` — 봉 위치 → 게이트를 모두 통과한 진입 신호. `run_backtest`와
  `portfolio.simulate_portfolio`가 **이 함수 하나**를 공유한다. 새 진입 조건을 만들면 여기에
  넣어야 두 경로에 동시에 반영된다. 순서는 실거래 `_evaluate_symbol`과 같다:
  `detect_signal` → `apply_regime_filter` → `passes_volatility_floor`.
- **엔진의 기본값은 "지금 실거래 설정"이어야 한다.** 이게 깨져서 실제로 두 번 사고가 났다 —
  `sma_period`가 20으로 하드코딩돼 실거래값(10)과 다른 전략이 조용히 측정됐고(2026-08-23),
  `fee_pct_per_side`가 0.0이라 수수료 없는 결과가 나왔다(2026-09-22). 지금은
  `run_backtest`/`gated_signals`의 진입 관련 기본값이 전부 config에서 오고,
  `test_engine_defaults_match_the_live_config`와 `test_gated_signals_defaults_match_...`가
  그것을 고정한다 — **새 전략 파라미터를 엔진에 추가하면 기본값을 config에서 가져오고 그 두
  테스트에도 넣을 것.**
- `aggregate_stats`의 `max_drawdown_r`을 **포트폴리오 낙폭으로 쓰지 말 것** — 종목별 낙폭 중
  가장 나쁜 하나일 뿐이고, 암호화폐는 상관이 높아 손실이 여러 종목에 동시에 온다(종목 3개가
  같은 주에 -8R씩 빠지면 실제 -24R인데 이 값은 -8R로 보고한다). 진짜 낙폭은
  `report.portfolio_stats(trades_by_symbol)`이 청산 시각 순으로 합쳐서 낸다.
- **연속손실 서킷브레이커에는 자동 쿨다운이 있다**(`CONSECUTIVE_LOSS_COOLDOWN_HOURS`, 기본 24시간).
  `compute_consecutive_losses`가 그 시간보다 오래된 청산을 세지 않으므로, "마지막 손실 이후
  24시간 동안 새 손실이 없으면" 카운터가 0으로 돌아가 봇이 스스로 재개한다. 0으로 두면 옛
  동작(수동 리셋만)으로 되돌아간다. 창의 기준은 **가장 최근 손실**이라 손실이 계속되는 동안에는
  보호가 풀리지 않는다.
  - **왜 넣었나 — 원래는 자동 해제가 없는 "걸쇠"였다**(2026-09-22 확인).
    한도(5)에 닿으면 신규 진입이 막히고, **막히면 새 청산이 안 생기므로 카운터가 저절로 안
    내려갔다.** 풀리는 경로가 둘뿐이었다: (a) 브레이커가 걸린 시점에 **이미 열려 있던** 포지션이
    이익으로 닫히거나, (b) 사람이 대시보드 버튼/텔레그램으로 수동 리셋
    (`futures_rule_bot.reset_consecutive_losses` → 저널에 `consecutive_loss_reset` 경계선).
    저널 실측 수동 리셋은 6주간 데모 6회·실계좌 7회(평균 6~7일에 한 번)였다.
  - **2026-09-22 시점에 데모 봇이 이 상태로 2일간 정지해 있었다**(연속손실 5/5, 마지막 거래
    09-20 02:40). 포지션이 다 닫힌 뒤 걸리면 (a) 경로가 없어서 사람이 알아채기까지 그대로
    멈춘다 — 텔레그램은 `circuit_breaker_blocked`를 알리지만 그게 "정지가 계속되고 있다"는
    신호로는 읽히지 않는다(→ 2026-09-22에 `check_breaker_halt` 정지 지속 알림을 추가).
  - 백테스트에서 이걸 모델링하지 않으면 **365일 시뮬레이션이 첫 5연패에서 영구 정지**되어
    동시보유 상한 6/8/10/12가 전부 똑같은 결과를 낸다(실제로 그렇게 나왔다). 그래서
    `portfolio.simulate_portfolio`는 `breaker_reset="daily"|"never"|"off"`로 사람의 개입 주기를
    **명시적 가정**으로 받는다 — 결론을 낼 때 이 가정에 대한 민감도를 반드시 같이 볼 것.
- **동시보유 상한을 늘리면 서킷브레이커가 훨씬 자주 걸린다**(2026-09-09). 손실이 큰 뭉치로
  도착하기 때문 — 상한 4→12에서 연속손실 발동이 연 60→80회, 일일손실 발동이 4→14회로 늘고 그
  정지가 나쁜 타이밍에 들어가 총R이 오히려 떨어진다. 시뮬레이션상 8이 정점이라 `.env`는 8이다.
  **백테스트에 리스크 규칙을 넣을 땐 그 규칙이 어떤 정보를 언제 알 수 있는지를 실거래와 맞출 것** —
  연속손실은 "청산이 저널에 기록된 뒤에만" 알 수 있는데 진입 시각 기준으로 세면 동시보유를
  늘릴수록 결과가 좋아지는 것처럼 보인다(실제로 이 오류로 상한 12를 잘못 권고했다가 정정함).
- **상승 레짐에서는 숏 진입을 막는다**(`RULE_REGIME_SMA_PERIOD`, 기본 400 — 0이면 필터 off).
  종가가 1시간봉 SMA400 위면 숏 신호를 버린다(롱은 안 건드림). 4분할 워크포워드에서 필터 없는
  현재 설정은 마지막 구간 -5.4R로 FAIL, SMA400/700 숏차단만 전 구간 양수 PASS라서 총R이 더 나은
  400을 채택(`scripts/run_regime_filter_walkforward.py`). 규칙의 유일한 정의는
  `futures_strategy.apply_regime_filter()`이고 백테스트(`engine.run_backtest`의
  `regime_sma_period` 인자, 기본값이 실거래값)와 실거래 봇이 둘 다 이걸 부른다 —
  조건을 두 군데 따로 적지 말 것. 이 필터 때문에 캔들 조회 개수가 101 → `SMA기간+2`로 늘었다.
- `skipped_same_signal_bar`/`skipped_price_drift`는 `_SILENT_EVENTS`라 저널에 안 남는다(매
  사이클 반복되므로) — 현재 사이클 결과에는 실려서 대시보드로는 보인다. 대신 **일별 집계는
  `filter_stats`에 쌓는다**(2026-09-09) — 필터가 백테스트대로 도는지 확인할 유일한 수단이라.
  집계는 `(심볼, 신호봉)` 단위로 중복 제거하므로 30초마다 반복돼도 한 봉은 1로만 센다. 레짐
  필터가 버린 숏도 `no_signal`이 아니라 `skipped_regime`으로 구분해서 셀 수 있게 해뒀다.
  **새 차단 사유를 만들면 결과에 `signal_bar_timestamp`를 반드시 실을 것** — 없으면 안 세진다.
- **포지션이 있는데 손절 주문이 없으면 `unprotected_position`을 저널에 남긴다**(2026-09-09,
  `check_position_protection`). 10배 레버리지에서 가장 비싼 실패인데 그동안 대시보드 카드의
  "-" 한 글자로만 표시돼 정상 상태와 구분이 안 됐다. 저널에 남기면 텔레그램 알림 경로를 그대로
  타고, 대시보드는 화면 맨 위 빨간 배너로 띄운다. 보호가 복구되면 `position_protected`를 한 번
  남긴다. **판정에는 반드시 `get_bracket_prices(..., strict=True)`를 쓸 것** — 기본값은 조회
  실패도 `(None, None)`으로 뭉뚱그려서 일시적 API 오류가 무보호 경보로 둔갑한다.
- 이 두 이벤트가 생기면서 "봇이 이 포지션을 아는가" 판정은 **마지막 기록이 아니라 마지막
  *생애주기* 기록**(`_LIFECYCLE_EVENTS`)을 봐야 한다 — 안 그러면 무보호 기록이 마지막이 되는
  순간 `check_and_log_untracked_position`이 매 사이클 진입 기록을 중복 백필한다.
- 청산 기록(`event="closed"`)에는 실현손익($) 외에 **방향/손절가/익절가/진입시각/`realized_r`/
  `max_favorable_r`/`max_adverse_r`**가 같이 들어간다(2026-09-09). $ 금액만으로는 사이징에 따라
  같은 -20달러가 -0.3R일 수도 -1R일 수도 있어 백테스트와 비교가 안 됐고, 방향·손절가가 없어
  사후 복원도 불가능했다. R은 가격만으로 계산해서(손절가까지가 -1R) 수량/레버리지와 무관하다.
- **가격으로 잰 R은 저널의 진입가가 그 청산과 맞을 때만 유효하다.** 2026-09-07 ZEC 재진입 루프
  때는 청산 6건이 같은 `entered` 기록에 묶여서, 실현손익 -1.53인 거래의 R이 +2.63으로 계산됐다.
  `resolve_closed_trades`는 **R과 실현손익의 부호가 모순되면 그 R을 버린다**(달러 통계에는 그대로
  남는다) — 손절/익절 판정에 이미 쓰던 것과 같은 기준이다. 그래서 화면의 달러 거래수와 R 거래수가
  다를 수 있고, 대시보드는 그 차이를 곡선 위에 명시한다.
- **거래가 어떤 설정으로 나왔는지는 저널이 스스로 들고 있어야 한다**(2026-09-12,
  `current_strategy_config()` / `log_config_change()`). 봇이 시작할 때 진입 판단에 영향을 주는
  설정(ADX/SMA·레짐SMA·최소변동성·손절폭/손익비·레버리지·동시보유 상한·**감시 종목 목록** 등)이
  직전 기록과 다르면 `config_changed`를 한 줄 남긴다. 이유: 저널에 거래만 있으면 화면의 총R이
  레짐/저변동 필터 전후를 섞은 숫자인지 알 수 없어 백테스트와 비교 자체가 불가능하다 — 그 경계가
  사람 기억과 UPDATE_LOG에만 있었다. 대시보드는 자산곡선에 그 경계를 세로선으로 긋고 "현재 설정
  이후 N건/총R"을 적으며(`performance.config_changes` / `since_config_change`), 텔레그램도
  알린다(.env만 고치고 재시작을 안 했거나 엉뚱한 env를 재시작한 게 이 한 통으로 드러난다).
  **새 전략 파라미터를 만들면 `current_strategy_config()`에도 넣을 것** — 대시보드 설정 표시도
  같은 함수에서 나오므로 한 곳만 고치면 된다.
- **전략 버전**(2026-09-22, `src/execution/strategy_versions.py`). 위 `config_changed` 경계에
  번호(v1, v2 …)를 붙이고 버전별 성과를 낸다 — 대시보드 성과 섹션의 버전 표, CSV의
  `strategy_version` 칼럼과 `versions_{env}.csv`, 텔레그램 설정 변경 알림이 전부 여기서 나온다.
  - `config_changed` 기록 이전(09-12 전)의 규칙 변경은 `HISTORICAL_VERSIONS`에 복원해 뒀다(v1~v4,
    경계 시각은 저널 흔적/UPDATE_LOG 기반 추정). **새 변경을 거기 적지 말 것** — 자동으로 버전이 된다.
  - 버전을 올리지 않는 기록: `first_record`(추적 시작)와 바뀐 항목이 전부 `from=None`인 기록
    (`current_strategy_config()`에 추적 항목을 새로 추가한 것).
  - 거래는 **진입 시각**으로 버전에 배정한다(청산이 재시작 뒤여도 진입 때의 규칙이 만든 거래다).
  - **설정값이 아니라 코드로 적힌 진입/청산 규칙을 바꾸면 `futures_strategy.STRATEGY_LOGIC_REVISION`을
    1 올릴 것.** 안 그러면 config가 같아서 새 버전이 안 생긴다(마감봉 신호 전환 같은 변경이 그랬다).
- 선물은 현물과 달리 `enable_demo_trading(True)`로 연결한다(구 testnet.binancefuture.com 방식인
  `set_sandbox_mode`가 아님). 키는 실제 바이낸스 계정 로그인 후 demo.binance.com/en/my/settings/api-management 에서 발급.
- **바이낸스 요청 한도는 IP 단위다 — 데모 봇·실계좌 봇·대시보드·텔레그램 봇이 한 한도(USDT-M 분당
  가중치 2400)를 나눠 쓴다**(2026-09-28 IP 차단 사고, 418 -1003). 한도를 넘기면 IP가 몇 분씩 차단되고
  그동안 **봇은 "실행 중"인데 캔들·잔고를 못 받아 아무것도 못 한다.** 차단 중에도 요청이 계속되면
  차단이 연장·확대된다(최대 3일). 원인은 둘이었다: (1) `get_futures_market_data_client()`가 부를
  때마다 새 클라이언트를 만들어 종목마다 거래소 정보를 다시 받았다 → 이제 프로세스당 하나.
  (2) 대시보드가 화면마다 5초 주기로 잔고·종목별 포지션·전 종목 시세를 물었다 → 포지션은
  `get_positions`로 한 번에, 거래소 응답은 `dashboard/app.py`의 `_cached`로 화면끼리 공유.
  **새 조회를 추가할 땐 종목 루프 안에서 거래소를 부르지 말고(한 번에 받는 API를 쓸 것), 대시보드
  라우트는 `_cached`를 거칠 것.** `fetch_tickers()`/`fetch_open_orders()`를 심볼 없이 부르면 가중치 40이다.
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
- **봇은 띄운 쪽과 분리된 프로세스다**(2026-09-28, `bot_process.start`가 `CREATE_NO_WINDOW`로 띄움 — `DETACHED_PROCESS`는 venv 실행기의 자식이 콘솔 창을 새로 띄워서 안 된다).
  예전엔 대시보드 콘솔을 물려받아서 대시보드 창을 닫으면 봇까지 같이 죽었다. 콘솔이 없으니 끄는 것도
  콘솔 신호(CTRL_BREAK)가 아니라 **종료 요청 파일**(`state/*.stop`)이다 — 봇 루프가 사이클 사이
  대기 중에 1초마다 확인하고 스스로 끝낸다(주문 도중에 끊기지 않게). 30초 안에 안 끝나면 강제 종료.
  **Windows에서 분리된 프로세스에 CTRL_BREAK를 보내지 말 것** — 보내는 쪽 콘솔 전체에 갈 수 있다.
  화면 로그는 stdout(분리 시 버려짐), 로깅 시작 전 오류만 `logs/{key}_process.err`.
- **감시 루프는 봉 마감 직후(+3초)에 한 번 더 깬다**(2026-09-28, `seconds_until_next_cycle`). 신호는
  마감 봉으로만 생기므로 30초 주기로만 돌면 신호 뒤 진입까지 기다리는 시간이 곧 진입 슬리피지다
  (실측 중간값 39초). 주기 전체를 줄이지 않은 이유: 요청 수가 그만큼 늘고 IP 한도는 대시보드와 공유다.
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
- 저널의 `entered`/`closed`/`circuit_breaker_blocked`/`unprotected_position`/`position_protected`
  이벤트만 알림 대상(`src.telegram_bot._NOTIFY_EVENTS`) — `rejected_exchange_error` 등은 매
  사이클 반복될 수 있어 노이즈라 제외한다(대신 대시보드가 24시간 코드별 건수로 집계해서 보여준다,
  `performance.summarize_recent_issues`. 실제로 몇 달간 TSLA -2027이 141건 쌓이도록 아무도 몰랐다).
- **하루 한 번 전날 성과를 자동으로 보낸다**(2026-09-09, `check_daily_summary`). 로컬 시각
  `TELEGRAM_DAILY_SUMMARY_HOUR`(기본 9시)를 지나면 **전날 하루치**를 한 통 — "지금까지의 오늘"이
  아니라 완결된 하루라야 의미가 있고, 일일 손실 한도가 리셋되는 경계(로컬 날짜)와 같은 기준을
  써야 대시보드가 말하는 "오늘"과 어긋나지 않는다. `/summary`로 아무 때나 다시 볼 수도 있다.
  다른 알림과 달리 최초 실행에서 건너뛰지 않는다 — 하루 한 통이라 과거를 쏟아낼 위험이 없다.
- **프로세스 생존과 사이클이 도는 것은 다른 문제다**(2026-09-09). `check_bot_status_change`는
  프로세스 생사만 보므로 "떠 있는데 거래소 응답 대기로 멈춘" 상태를 못 잡는다 —
  `check_heartbeat_stall`이 하트비트 나이가 `HEARTBEAT_STALE_SECONDS`(기본 600초)를 넘으면
  한 번 알리고, 다시 돌기 시작하면 한 번 더 알린다. 봇이 꺼져 있을 땐 아무 말도 안 한다.
- **서킷브레이커 정지가 계속되는 것도 따로 알린다**(2026-09-22, `check_breaker_halt`). 저널의
  `circuit_breaker_blocked`는 막히기 시작할 때 한 줄뿐이라 "지금도 막혀 있는가"는 **하트비트의
  `circuit_breaker_blocked`/`breaker_reason`에만 있다**(봇이 매 사이클 씀). 정지가
  `BREAKER_HALT_ALERT_HOURS`(기본 6시간) 넘게 이어지면 한 번, 풀리면 한 번 더 알린다.
  하트비트에 그 필드가 없으면(옛 봇 코드) 판단하지 않는다.
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
