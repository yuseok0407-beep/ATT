# auto2 — 바이낸스 자동 트레이딩 봇

리서치 → 레짐 감지 → 포트폴리오 배분 → 리스크 게이트 → Claude 의사결정 → 주문 실행 → 저널링 순서로 동작하는 자동 트레이딩 봇. 기본값은 항상 바이낸스 **테스트넷**(가상 자금).

## 수동 실행

```
./venv/Scripts/python.exe scripts/scheduled_run.py
```

1회 사이클(리서치 → 배분 → Claude 검토 → 필요시 테스트넷 주문)을 실행하고 `logs/scheduler.log`에 기록을 남긴다.

## 자동화(스케줄러) 등록 — 아직 비활성화 상태

준비는 되어 있지만 사용자 요청으로 아직 등록하지 않았다. 등록하려면 PowerShell(관리자 권한 불필요)에서:

```powershell
$action = New-ScheduledTaskAction -Execute "<프로젝트 경로>\venv\Scripts\python.exe" -Argument "<프로젝트 경로>\scripts\scheduled_run.py"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName "AutoTradingBot" -Action $action -Trigger $trigger -Description "1시간마다 트레이딩 봇 사이클 실행"
```

등록 후 PC가 켜져 있는 동안 1시간마다 자동 실행되며, 그때마다 Claude API 호출 비용이 발생한다(월 예산 관련 계산은 대화 기록 참고). 중단하려면:

```powershell
Unregister-ScheduledTask -TaskName "AutoTradingBot" -Confirm:$false
```

## 실거래 전환 (현물)

`.env`의 `USE_TESTNET=false`로 바꾸면 실거래 모드가 되지만, `src/execution/orders.py`의 `execute_order`는 `confirm_live=True`를 명시적으로 넘기지 않으면 주문을 차단하도록 이중 안전장치가 걸려 있다. 실거래 전환은 충분한 테스트넷 검증 후 별도로 진행할 것.

---

## 선물(BTC/USDT, 10x 레버리지 롱/숏)

현물 리밸런싱 봇과는 완전히 별개의 파이프라인이다. `src/futures_pipeline.py` / `scripts/scheduled_futures_run.py`.

### 안전장치 요약

- 손절폭은 `.env`의 `STOP_LOSS_PCT`(기본 1.25%)로 **코드가 고정**한다 — Claude는 방향(LONG/SHORT/CLOSE/HOLD)과 확신도만 판단하고 손절가는 건드릴 수 없다.
- 포지션을 열 때마다 반대방향 `reduceOnly` STOP_MARKET 주문을 동시에 걸어, 손절 없는 레버리지 포지션이 존재할 수 없게 한다.
- 진입 전 `check_stop_before_liquidation()`으로 손절가가 청산가 대비 최소 20% 이상 여유가 있는지 검증하고, 부족하면 진입 자체를 거부한다.
- `src/execution/futures_orders.py`도 이 파이프라인이 `env="live"`로 호출될 때 `confirm_live=True` 없이는 주문을 차단한다(아래 "상시 감시 봇" 절의 데모/실계좌 동시 운영과 같은 안전장치를 공유).
- 청산가 추정치는 근사값이다 — 격리마진·단일 티어 가정이며, 실제 청산가는 포지션 진입 후 거래소가 돌려주는 값(`liquidationPrice`)을 기준으로 삼아야 한다.

### 선물 데모 트레이딩 키 발급 (2026년 기준, 현물 테스트넷과 별개!)

바이낸스가 예전 독립 테스트넷(GitHub 로그인 방식의 testnet.binancefuture.com)을 폐지하고, 실제
계정으로 로그인해서 쓰는 "Demo Trading"으로 통합했다. `testnet.binancefuture.com`으로 들어가면
이제 로그인 화면 없이 `demo.binance.com`으로 리다이렉트된다.

1. https://accounts.binance.com 에서 **실제 바이낸스 계정으로 로그인**(또는 가입) — 이메일/전화번호, Google, Apple, Telegram 로그인 지원
2. https://demo.binance.com/en/my/settings/api-management 접속 → 데모 트레이딩 전용 API 키 발급
   (실거래 키와는 별개, demo-fapi.binance.com에서만 동작함)
3. `.env`에 추가:
   ```
   BINANCE_FUTURES_API_KEY=...
   BINANCE_FUTURES_API_SECRET=...
   ```

코드에서는 `client.enable_demo_trading(True)`로 이 데모 환경에 연결한다(현물처럼 `set_sandbox_mode`가 아님) — `src/data/futures_exchange.py` 참고.

### 연결 확인 및 1회 실행

```
./venv/Scripts/python.exe scripts/futures_check_connection.py
./venv/Scripts/python.exe scripts/scheduled_futures_run.py
```

### 실거래 전환 (선물) — 특히 신중히

이 파이프라인(`futures_pipeline.py`)은 현재 미사용 경로다(아래 "상시 감시 봇" 참고, 실제로 쓰는 건
그쪽). `get_futures_client()`는 이제 `env` 인자로 데모/실계좌를 명시적으로 구분한다(2026-08-22,
예전처럼 `USE_TESTNET=false`만으로는 더 이상 실계좌로 안 붙는다) — 이 파이프라인을 실거래로 쓰려면
`get_futures_client(env="live")`와 `open_position(..., env="live", confirm_live=True)`처럼 코드에서
직접 `env="live"`를 넘기도록 고쳐야 한다. 10배 레버리지는 진입가 대비 약 9~10% 역행 시 청산되므로,
데모에서 최소 며칠~몇 주간 롱/숏 전환·손절 체결·청산가 계산이 의도대로 동작하는지 충분히 지켜본 뒤
전환할 것을 강력히 권한다.

---

## 상시 감시 봇 (규칙 기반, Claude 미사용) + 웹 대시보드

`src/futures_pipeline.py`(Claude가 매 사이클 판단)와는 다른 별도 경로다. 이쪽은 **로컬 규칙만으로**
가격을 계속 감시하다가 조건이 맞으면 즉시 손절+익절을 동시에 걸고 진입한다 — API 호출(Claude) 없이
동작하므로 비용이 들지 않는다.

### 진입 규칙

- ADX(14) ≥ `RULE_ADX_THRESHOLD`(기본 25): 추세가 확인된 구간에서만
- 종가가 SMA20을 막 상향/하향 **돌파하는 순간**(레벨이 아니라 크로스) + RSI(14)가 50 기준 같은 방향
- 롱: ADX≥25 + SMA20 상향 돌파 + RSI≥50 / 숏: ADX≥25 + SMA20 하향 돌파 + RSI≤50
- 기본 캔들 주기는 `RULE_TIMEFRAME`(기본 1시간), 감시 주기는 `POLL_INTERVAL_SECONDS`(기본 30초)

### 손절/익절

- 손절: 진입가 대비 `STOP_LOSS_PCT`(기본 1.25%)
- 익절: 손절폭 × `TAKE_PROFIT_RR`(기본 2.0배, 즉 2.5%) — 진입과 동시에 반대방향 reduceOnly
  STOP_MARKET + TAKE_PROFIT_MARKET 주문을 함께 건다
- 바이낸스 선물엔 스팟 같은 네이티브 OCO가 없어서, 둘 중 하나가 체결되면 나머지 하나가 고아로 남는다.
  감시 루프는 매 사이클마다 포지션이 없으면 `cleanup_stale_orders()`를 호출해 이걸 자동 정리한다
  (실제로 고아 주문을 인위로 만들어서 자동 정리되는 것까지 검증 완료).

### 실행

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
