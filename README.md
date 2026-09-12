# auto2 — 바이낸스 선물 자동 트레이딩 봇

USDT-M 선물(레버리지 롱/숏)을 **순수 규칙 기반**으로 상시 감시하다가 조건이 맞으면 손절+익절을
동시에 걸고 진입하는 봇. 외부 API(Claude 등) 호출이 없어 운영 비용이 들지 않는다.
데모 트레이딩 계좌와 실계좌를 **완전히 독립된 프로세스로 동시에** 운영한다.

구성은 네 덩어리다.

| 프로세스 | 하는 일 | 실행 |
|---|---|---|
| 감시 봇 | 신호 탐지 → 리스크 검증 → 진입/브라켓 주문 → 저널링 | `scripts/run_futures_bot.py --env demo\|live` |
| 웹 대시보드 | 포지션/성과/조건 근접도/차단 통계 조회, 봇 시작·중지 | `dashboard/app.py` (http://127.0.0.1:5055) |
| 텔레그램 봇 | 진입·청산·경보 푸시, 폰에서 원격 시작·중지 | `scripts/run_telegram_bot.py` |
| 백테스트 | 전략/파라미터/심볼 검증(OOS·워크포워드) | `scripts/run_*_backtest.py` 등 |

> 2026-09-12에 현물 리밸런싱 계열과 Claude 기반 선물 파이프라인(둘 다 2026-08 이후 미사용),
> 대안 전략 실험 모듈을 전부 걷어냈다. 이력은 git에 남아 있다(`git log -- src/pipeline.py`).

## 빠른 시작

```
./venv/Scripts/python.exe -m pip install -r requirements.txt
copy .env.example .env                                  # 키 입력 (아래 "키 발급" 참고)
./venv/Scripts/python.exe scripts/futures_check_connection.py --env demo   # 연결 확인
./venv/Scripts/python.exe scripts/run_futures_bot.py --env demo            # 감시 시작
start_dashboard.bat                                     # 대시보드 (별도 창)
```

## 전략 (순수 규칙, `src/core/futures_strategy.py`)

진입 판단은 **반드시 마감된 캔들만** 쓴다 — 바이낸스가 돌려주는 마지막 봉은 아직 진행 중이라
그 값으로 판단하면 백테스트가 검증한 적 없는 일시적 스파이크에 반응한다.

**신호** (`RULE_TIMEFRAME`, 기본 1시간봉)
- ADX(14) ≥ `RULE_ADX_THRESHOLD`(기본 30) — 추세가 확인된 구간에서만
- 종가가 SMA(`RULE_SMA_PERIOD`, 기본 10)를 **돌파하는 순간**(레벨이 아니라 크로스) + RSI(14)가 50 기준 같은 방향
- 롱: 상향 돌파 + RSI≥50 / 숏: 하향 돌파 + RSI≤50

**진입을 막는 필터** (전부 백테스트·워크포워드로 검증해서 넣은 것들 — 근거는 `UPDATE_LOG.md`)
- 상승 레짐 숏 차단: 종가가 SMA(`RULE_REGIME_SMA_PERIOD`, 기본 400) 위면 숏 신호를 버린다
- 저변동 차단: ATR이 손절폭의 `MIN_ATR_TO_STOP_RATIO`(기본 0.64)배 미만이면 건너뛴다
- 같은 신호 봉 재진입 잠금 — 한 봉으로는 한 번만 진입한다(백테스트와 의미를 맞춤)
- 진입가 괴리 검사: 현재가가 신호 봉 종가에서 손절폭의 `MAX_ENTRY_PRICE_DRIFT_R`(기본 0.5)배 넘게 벌어지면 건너뛴다

**손절/익절** — 코드가 고정 결정한다(판단 여지 없음)
- 손절: 진입가 대비 `STOP_LOSS_PCT`(기본 1.25%)
- 익절: 손절폭 × `TAKE_PROFIT_RR`(기본 2.0배)
- 진입과 **동시에** 반대방향 reduceOnly STOP_MARKET + TAKE_PROFIT_MARKET을 건다. 바이낸스 선물엔
  네이티브 OCO가 없어 하나가 체결되면 나머지가 고아로 남으므로, 감시 루프가 포지션이 없을 때마다
  `cleanup_stale_orders()`로 정리한다.
- 진입 전 `check_stop_before_liquidation()`으로 손절가가 청산가 대비 충분히 여유 있는지 검증한다.

**리스크 한도**
- 거래당 리스크 `FUTURES_RISK_PER_TRADE`(마진 자산 대비), 동시보유 `MAX_CONCURRENT_POSITIONS`
- 서킷브레이커: 일일 손실 `MAX_DAILY_LOSS_PCT`(5%) 또는 연속손실 `MAX_CONSECUTIVE_LOSSES`(5회)
- 손절 주문이 없는 포지션이 감지되면 저널에 남기고 대시보드 배너 + 텔레그램으로 즉시 알린다

## 백테스트 / 검증

```
./venv/Scripts/python.exe scripts/run_backtest.py                      # 현재 설정 그대로
./venv/Scripts/python.exe scripts/run_symbol_screen_backtest.py        # 종목 스크리닝
./venv/Scripts/python.exe scripts/run_symbol_oos_backtest.py           # 종목 아웃오브샘플
./venv/Scripts/python.exe scripts/run_oos_backtest.py                  # 전·후반 분할
./venv/Scripts/python.exe scripts/run_regime_filter_walkforward.py     # 4분할 워크포워드
./venv/Scripts/python.exe scripts/run_hyperopt.py                      # 파라미터 그리드서치
```

**설정을 바꿔 실거래에 반영할 땐 워크포워드를 통과한 것만 쓸 것.** 봇이 시작할 때 그 시점의 설정을
저널에 `config_changed`로 남기므로(2026-09-12), 나중에 대시보드 자산곡선에서 "어떤 규칙으로 낸
구간인지"를 경계선으로 확인할 수 있다.

## 선물 데모 트레이딩 키 발급 (현물 테스트넷과 별개!)

바이낸스가 예전 독립 테스트넷(GitHub 로그인 방식의 testnet.binancefuture.com)을 폐지하고, 실제
계정으로 로그인해서 쓰는 "Demo Trading"으로 통합했다. `testnet.binancefuture.com`으로 들어가면
이제 로그인 화면 없이 `demo.binance.com`으로 리다이렉트된다.

1. https://accounts.binance.com 에서 **실제 바이낸스 계정으로 로그인**(또는 가입)
2. https://demo.binance.com/en/my/settings/api-management 접속 → 데모 트레이딩 전용 API 키 발급
   (실거래 키와는 별개, demo-fapi.binance.com에서만 동작함)
3. `.env`에 `BINANCE_FUTURES_API_KEY` / `BINANCE_FUTURES_API_SECRET` 입력

코드에서는 `client.enable_demo_trading(True)`로 이 데모 환경에 연결한다(현물처럼
`set_sandbox_mode`가 아님) — `src/data/futures_exchange.py` 참고.

---

## 운영

### 감시 봇 실행

```
./venv/Scripts/python.exe scripts/run_futures_bot.py --env demo   # 기본값, --env 생략 가능
./venv/Scripts/python.exe scripts/run_futures_bot.py --env live   # 실계좌 — 아래 절 참고
```

터미널을 계속 띄워두는 상시 프로세스다. `logs/futures_rule_bot.log`(`live`는 `.live.log`)에
로그가 남고, `journal/futures_rule_trades.jsonl`(`live`는 `.live.jsonl`)에 신호/진입/거부 이력이
쌓인다. 데모/실계좌는 완전히 독립된 프로세스라 둘 다 동시에 띄워둘 수 있다. Ctrl+C로 중단.

### 웹 대시보드

```
./venv/Scripts/python.exe dashboard/app.py
```

http://127.0.0.1:5055 접속 — 마진 잔고, 보유 포지션(진입가·현재가·미실현손익·청산가), 걸려있는
손절가/익절가, 최근 판단 이력을 5초마다 자동 갱신해서 보여준다. 봇 루프와는 별개 프로세스라
동시에 띄워놓고 봐도 되고, 봇이 꺼져있어도 계좌 현재 상태만 확인하는 용도로도 쓸 수 있다.

### 데모/실계좌 동시 운영 (2026-08-22)

대시보드 상단의 DEMO/LIVE 버튼으로 계좌를 전환한다 — 새로고침하면 항상 DEMO로 돌아오고(실수로
실계좌 화면인 줄 모르고 조작하는 사고 방지), LIVE 탭은 빨간 배너/테두리로 확실히 구분된다.

실계좌를 붙이려면:

1. https://accounts.binance.com 에서 **실제 계정**으로 로그인 → API 관리에서 선물 거래용 키 발급
   (데모 트레이딩 키와는 완전히 별개 — demo.binance.com이 아니라 binance.com에서 발급받을 것).
2. `.env`에 추가:
   ```
   BINANCE_FUTURES_LIVE_API_KEY=...
   BINANCE_FUTURES_LIVE_API_SECRET=...
   ```
3. 대시보드 재시작 후 LIVE 탭 클릭 → 잔고/레버리지가 정상 조회되면 연결 성공(이 단계는 주문을
   전혀 내지 않는다). LIVE 탭에서 "시작"을 눌러야 실계좌 감시 루프(`run_futures_bot.py --env live`)가
   뜨고, 그 순간부터 신호가 나면 실제 주문이 나간다 — 리스크 설정(손절폭/레버리지/거래당 리스크%/
   동시 포지션 수)은 데모와 완전히 동일하게 적용된다.

키가 없는 채로 LIVE 탭을 열거나 시작을 누르면 서버가 죽지 않고 명확한 오류 메시지만 뜬다.

대시보드 LIVE 탭엔 이 PC의 현재 공인 IP도 표시된다(120초 캐시) — 바이낸스 API 키의 IP 화이트리스트에
등록된 값과 다르면 `-2015 Invalid API-key, IP, or permissions` 오류가 나므로, 화이트리스트에는
**사설 IP(192.168.x.x 등)가 아니라 여기 표시되는 공인 IP**를 등록해야 한다.

### 텔레그램 알림 + 원격 시작/중지 (2026-08-22)

`scripts/run_telegram_bot.py`를 상시 띄워두면 진입/청산/서킷브레이커 발동, 감시 봇 켜짐/꺼짐,
공인 IP 변경을 폰으로 알려주고, 폰에서 `/start_demo`·`/start_live` 등으로 봇을 원격 시작/중지할
수 있다. 대시보드/봇 루프와는 완전히 별개 프로세스라 대시보드를 안 보고 있어도 동작한다.

**설정:**

1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 이름/username 입력 → 토큰 발급
2. 만든 봇과 대화를 시작(아무 메시지나 전송) — chat_id를 알아내려면 필요
3. 브라우저로 `https://api.telegram.org/bot<발급받은토큰>/getUpdates` 열어서
   `"chat":{"id":...}`의 숫자를 확인
4. `.env`에 추가:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
5. (선택) BotFather의 `/setcommands`로 명령어 자동완성 메뉴 등록:
   ```
   start_demo - 데모 봇 시작
   stop_demo - 데모 봇 중지
   start_live - 실계좌 봇 시작
   stop_live - 실계좌 봇 중지
   status - 데모/실계좌 상태 조회
   help - 도움말
   ```
6. 실행 — 대시보드 상단의 "텔레그램 봇" 카드에서 **시작** 버튼을 누르면 된다(가장 쉬움). 터미널에서
   직접 띄우고 싶으면:
   ```
   ./venv/Scripts/python.exe scripts/run_telegram_bot.py
   ```
   대시보드 버튼/터미널 실행/Windows 작업 스케줄러 "로그온 시 시작" 등록 중 편한 방식을 쓰면 된다
   (전부 같은 PID 파일을 공유해서 서로 안전하게 겹쳐 쓸 수 있음). 로그는 `logs/telegram_bot.log`.
   토큰/채팅ID가 `.env`에 없는 채로 대시보드에서 시작을 누르면 서버가 죽지 않고 명확한 오류만 뜬다.

**명령어:**
- `/status` — 데모/실계좌 각각 실행 여부·마지막 사이클·마진 자산에 더해 **보유 중인 포지션마다
  진입가·현재가·미실현손익·손절가·익절가**까지 보여준다.
- `/start_demo`, `/stop_demo`, `/start_live`, `/stop_live` — 원격 시작/중지(대시보드 버튼과 동일하게
  동작).
- 진입/청산/서킷브레이커 발동은 명령을 안 보내도 **자동으로** 알림이 온다(최대 `TELEGRAM_POLL_
  INTERVAL_SECONDS`만큼 지연, 기본 15초).

**안전장치**: `TELEGRAM_CHAT_ID`와 일치하지 않는 발신자의 메시지는 전부 무시한다(응답도 안 함) —
봇 토큰이 유출돼도 다른 사람이 원격으로 봇을 켜고 끌 수 없다. `/start_live`도 `/start_demo`와
동일하게 즉시 실행되니(추가 확인 절차 없음), 텔레그램 계정 자체의 보안(2단계 인증 등)을 반드시
챙길 것.
