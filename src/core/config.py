import os
from dotenv import load_dotenv

load_dotenv()


# 데모 트레이딩(demo.binance.com) 전용 키 — 바이낸스가 옛 선물 테스트넷
# (testnet.binancefuture.com)을 폐지하고 실제 계정으로 로그인해 쓰는 데모 트레이딩으로
# 통합했다. 현물의 set_sandbox_mode()가 아니라 enable_demo_trading()으로 붙는다.
BINANCE_FUTURES_API_KEY = os.getenv("BINANCE_FUTURES_API_KEY", "")
BINANCE_FUTURES_API_SECRET = os.getenv("BINANCE_FUTURES_API_SECRET", "")
# 실계좌(진짜 자금) 전용 키 — binance.com 실제 계정의 API 관리에서 발급(데모 계정 아님).
# 데모와 실계좌를 동시에 운영하기 위한 별도 키 쌍(2026-08-22, get_futures_client(env=...) 참고).
BINANCE_FUTURES_LIVE_API_KEY = os.getenv("BINANCE_FUTURES_LIVE_API_KEY", "")
BINANCE_FUTURES_LIVE_API_SECRET = os.getenv("BINANCE_FUTURES_LIVE_API_SECRET", "")
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.0125"))  # 진입가 대비 손절 거리 (기본 1.25%)
FUTURES_RISK_PER_TRADE = float(os.getenv("FUTURES_RISK_PER_TRADE", "0.02"))  # 마진 자산 대비 거래당 리스크

# 규칙 기반 상시 감시 봇(Claude 미사용) 전용 설정
TAKE_PROFIT_RR = float(os.getenv("TAKE_PROFIT_RR", "2.0"))  # 손익비 — 손절폭의 몇 배를 익절폭으로 잡을지
RULE_TIMEFRAME = os.getenv("RULE_TIMEFRAME", "1h")  # 신호 계산에 쓰는 캔들 주기
# 2026-08-11 백테스트(최근 1년, 5종목, 전반/후반 아웃오브샘플)로 25->30, SMA20->SMA10이 일관되게
# 더 나은 것으로 확인되어 기본값 변경 (UPDATE_LOG.md 참고). RSI 필터는 그대로 유지(제거하면 악화 확인).
RULE_ADX_THRESHOLD = float(os.getenv("RULE_ADX_THRESHOLD", "30"))
RULE_SMA_PERIOD = int(os.getenv("RULE_SMA_PERIOD", "10"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))  # 감시 루프가 상태를 확인하는 주기

# 장기추세 레짐 필터 (2026-09-07) — 종가가 이 기간의 SMA 위면 숏 진입을 막는다(롱은 안 건드림).
# 4분할 워크포워드(scripts/run_regime_filter_walkforward.py, 12종목·1h·365일) 결과 지금 설정
# (필터 없음)은 마지막 구간 -5.4R로 OOS FAIL인 반면 SMA400/SMA700 숏차단만 전 구간 양수로 PASS했고,
# 그중 총R이 더 나은 400을 채택(400 +171.0R / 700 +161.5R). 0으로 두면 필터가 꺼진다.
RULE_REGIME_SMA_PERIOD = int(os.getenv("RULE_REGIME_SMA_PERIOD", "400"))
# 신호를 만든 마감 봉의 종가에서 현재가가 손절폭의 몇 배까지 벌어져도 진입을 허용할지.
# 이걸 넘으면 진입을 건너뛴다 — 손절/익절가가 전부 그 봉 종가 기준으로 계산되므로, 현재가가
# 이미 손절선 근처/너머면 진입하자마자 손절되거나 브라켓 주문이 -2021로 거부된다
# (2026-09-07 실계좌에서 3분 만에 연속손실 5회로 서킷브레이커가 걸린 사고의 직접 원인).
# 2026-09-22에 0.5 -> 0.1로 낮췄다. 0.5는 손절폭의 절반까지 불리한 체결을 허용한다는 뜻인데,
# 백테스트(365일 12종목 포트폴리오)에서 이 전략은 **편도 0.05R 슬리피지에서 이미 무너진다**
# (총R +127.6 -> +22.3, 4분할 OOS FAIL. 0.10R이면 -83R). 즉 전략이 죽는 수준의 10배를 구조적으로
# 허용하고 있었다 — 건당 기대값이 +0.121R뿐이라 허용 오차가 기대값보다 4배 큰 구조였다.
MAX_ENTRY_PRICE_DRIFT_R = float(os.getenv("MAX_ENTRY_PRICE_DRIFT_R", "0.1"))
# 저변동 구간 진입 차단 (2026-09-09) — ATR이 손절폭의 이 배수보다 작으면 진입하지 않는다.
# 근거: 손절 1.25%/익절 2.5%인데 ATR이 0.5%면 익절까지 5 ATR을 가야 한다. 1시간봉 스케일에서
# 그건 거의 안 일어나고 대신 시간이 흐르며 손절로 흘러간다 — 실제로 후보 신호 1999건을 특성별로
# 쪼개보면 저변동 구간이 일관되게 손실 구간이었다. ATR% 절대값이 아니라 손절폭 대비 비율로 쓰는
# 이유는 STOP_LOSS_PCT를 바꿔도 조건이 자동으로 따라오게 하기 위함.
# 포트폴리오 시뮬레이션(12종목·1h·365일, 동시보유/서킷브레이커 반영): 필터 없음은 4분할 OOS를
# 배정순서 50회 전부 FAIL(총R +157.6, MDD -32.0%)인 반면 k=0.64는 전부 PASS(+185.0, -25.6%).
# k를 0.4~1.2로 훑어도 전부 PASS라 특정 값에만 맞은 게 아니다. 0으로 두면 필터가 꺼진다.
MIN_ATR_TO_STOP_RATIO = float(os.getenv("MIN_ATR_TO_STOP_RATIO", "0.64"))

# 편도 거래 수수료율 — 백테스트의 R배수 차감과 실거래 R의 수수료 보정이 **같은 값**을 쓴다.
# 여기 한 곳에만 두는 이유: 이 값이 스크립트 7개에 각각 `FEE_PCT_PER_SIDE = 0.0004`로 복사돼
# 있었고, 엔진 기본값은 그와 별개로 0.0이었다 — 엔진을 직접 부르면 수수료 0인 결과가 조용히
# 나온다(2026-09-22).
#
# **이 전략에서 수수료는 오차항이 아니다.** 손절폭이 1.25%일 때 왕복 수수료의 R 환산은
# 2 x 0.0004 / 0.0125 = 0.064R이고, 실거래 121~105건의 평균 R이 각각 +0.02R / +0.06R이므로
# 수수료가 기대값과 같은 크기다. 데모 저널의 총R은 +2.50인데 이 수수료를 넣으면 -5.25가 된다.
#
# 기본값 0.0004(0.04%)는 보수적으로 잡은 값이다 — 2026-09-22에 실제 계좌 체결 내역
# (`fetch_my_trades`의 `commission`)에서 관측된 실효 편도 수수료는 명목가의 약 0.025%였다.
# 낙관 방향으로 틀리는 것보다 보수적으로 틀리는 편이 낫다.
FEE_PCT_PER_SIDE = float(os.getenv("FEE_PCT_PER_SIDE", "0.0004"))

# 연속손실 서킷브레이커가 자동으로 풀리기까지의 시간(시). 0이면 자동 해제 없음(= 2026-09-22
# 이전 동작).
#
# **왜 필요한가:** 이 브레이커는 원래 자동 해제가 없는 "걸쇠"였다. compute_consecutive_losses가
# 저널을 뒤에서부터 훑어 손실이 아닌 청산을 만날 때까지 세는데, 한도에 닿으면 신규 진입이 막히고
# 막히면 새 청산이 안 생기므로 **카운터가 저절로 안 내려간다.** 풀리는 길은 (a) 걸린 시점에 이미
# 열려 있던 포지션이 이익으로 닫히거나 (b) 사람이 수동 리셋을 누르는 것뿐이었고, 포지션이 다 닫힌
# 뒤에 걸리면 (a)가 없어서 사람이 알아챌 때까지 그대로 멈춘다 — 2026-09-22에 데모 봇이 실제로
# 2일간 그 상태였다(연속손실 5/5, 마지막 거래 09-20 02:40).
#
# 저널 실측 수동 리셋은 6주간 6~7회(연 ~55회)였다. 즉 정지 시간이 상당했다.
#
# 기본값 24시간의 근거: 포트폴리오 스윕(scripts/run_portfolio_sweep.py)에서 날짜 경계 리셋
# (≈24시간 쿨다운)과 브레이커 없음을 비교하면 동시보유 8에서 총R +111.5 vs +133.7이다 —
# 브레이커는 수익을 깎는 보험이고, 쿨다운은 그 보험료를 "무한 정지"에서 "최대 하루"로 묶는다.
# 더 짧게 잡으면 보호 효과가 줄고, 더 길게 잡으면 정지 시간이 다시 길어진다.
CONSECUTIVE_LOSS_COOLDOWN_HOURS = float(os.getenv("CONSECUTIVE_LOSS_COOLDOWN_HOURS", "24"))

# 다종목 감시 — 여러 심볼을 동시에 감시하되, 거래당 리스크(FUTURES_RISK_PER_TRADE)는 종목별로 그대로
# 두고 대신 "동시에 열 수 있는 포지션 개수"를 제한해 총 노출을 억제한다.
#
# 2026-08-12 스크리닝(scripts/run_symbol_screen_backtest.py, 36종목·1h·365일)+아웃오브샘플 검증
# (scripts/run_symbol_oos_backtest.py, 전반/후반 둘 다 총R 양수인 것만 PASS) 결과로 구성:
#   - BTC/ETH: OOS는 FAIL(후반 구간 마이너스)이지만 이미 실거래 중이라 사용자 판단으로 유지
#   - SOL/XRP: 스크리닝 최상위 + OOS PASS (엣지 가장 강함)
#   - CRCL/TSLA/BNB: 신규 추가, OOS PASS. SOXL은 데모 트레이딩 계좌에 아예 없어서(상장 자체가
#     안 됨, initialize()가 매번 건너뜀) 자리만 차지하던 걸 이걸로 교체
# SOXL/USDT:USDT: 실거래 선물엔 있지만 데모 트레이딩 계좌엔 없는 심볼(initialize()/
# _available_symbols()가 client.markets 기준으로 자동 필터링하므로 데모에선 조용히 스킵되고
# 실계좌 연결 시에만 활성화됨 — 2026-08-11 초기 백테스트에서 완만한 양의 결과, 다만 이후 도입된
# OOS 분할검증 방식으로는 미검증. AAPL/MSFT/SOXS도 실거래 마켓엔 있으나 스크리닝 스크립트가
# 데모 계좌 기준으로 후보를 걸러 애초에 백테스트된 적이 없어(성과가 나쁜 게 아니라 테스트 자체를
# 안 함) 이번엔 제외 — 실거래 마켓 기준으로 스크리닝을 다시 돌리는 게 후속 검토 대상.
# 2026-08-29: run_symbol_screen_backtest.py로 데모 계좌 거래 가능 36종목을 같은 전략(ADX30+SMA10)으로
# 스크리닝한 결과(1h, 365일) 상위 후보인 1000PEPE/TAO/SUI/ZEC를 추가(총R +17~+27, 거래수 130+,
# 최대DD 대비 R 양호). 사용자 확인 후 반영 — ETH는 이 스크리닝에서 유일하게 총R -4.56이었으나
# 손실 폭이 크지 않아(전체 4075건 중 165건) 유지하기로 함.
FUTURES_SYMBOLS = [s.strip() for s in os.getenv(
    "FUTURES_SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,CRCL/USDT:USDT,TSLA/USDT:USDT,"
    "BNB/USDT:USDT,SOXL/USDT:USDT,1000PEPE/USDT:USDT,TAO/USDT:USDT,SUI/USDT:USDT,ZEC/USDT:USDT",
).split(",") if s.strip()]
# 2026-09-09: 동시보유 상한이 '좋은 거래'를 막고 있었다 — 여러 종목이 동시에 신호를 낼 때
# 그 거래들이 오히려 더 좋은데(거래당 +0.164R, 전·후반 일관) 낮은 상한이 정확히 그때 걸린다.
# 정정된 포트폴리오 시뮬레이션에서 8이 정점(+185.0R/MDD -25.6%)이고 그 이상은 오히려 나빠진다
# (12는 +157.9R/-30.1%) — 동시보유가 많을수록 손실이 큰 뭉치로 도착해 서킷브레이커가 훨씬 자주
# 걸리기 때문. 실운영 값은 .env가 관리한다.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))

# 텔레그램 알림/원격 시작·중지 (2026-08-22) — src/telegram_bot.py, scripts/run_telegram_bot.py 참고.
# 토큰/채팅ID가 비어 있으면 run_telegram_bot.py가 시작 시 바로 종료한다(조용히 무동작하지 않음).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_POLL_INTERVAL_SECONDS = int(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", "15"))
# 하트비트가 이 시간 넘게 안 갱신되면 텔레그램으로 알린다(2026-09-09). 프로세스가 살아있는 것과
# 사이클이 실제로 도는 것은 다른 문제라, 지금까지 "프로세스는 떠 있는데 거래소 응답을 못 받아
# 멈춘" 상태는 /status를 직접 쳐보기 전엔 알 방법이 없었다. POLL_INTERVAL_SECONDS(30초)의 배수로
# 넉넉히 잡아서 일시적 지연에는 안 울리게 한다.
HEARTBEAT_STALE_SECONDS = int(os.getenv("HEARTBEAT_STALE_SECONDS", "600"))

# 서킷브레이커 정지가 이 시간 넘게 계속되면 텔레그램으로 한 번 알린다(2026-09-22). 저널의
# circuit_breaker_blocked 알림은 "막히기 시작했다"는 한 통뿐이라 "아직도 막혀 있다"로 읽히지
# 않았다 — 실제로 데모 봇이 연속손실 5/5로 2일간 멈춰 있는 걸 아무도 몰랐다. 연속손실 쿨다운
# (24시간)보다 충분히 짧게 잡아서, 사람이 개입할지 쿨다운을 기다릴지 고를 시간을 준다.
# 0 이하면 이 알림만 끈다.
BREAKER_HALT_ALERT_HOURS = float(os.getenv("BREAKER_HALT_ALERT_HOURS", "6"))

# 하루 한 번 전날 성과를 텔레그램으로 보낸다(2026-09-09). 로컬 시각 기준 이 시(hour)를 지나면
# 전날치를 한 번 쏜다 — 일일 손실 한도가 리셋되는 경계(date.today())와 같은 기준이라 "하루"의
# 정의가 대시보드와 어긋나지 않는다. -1이면 이 기능만 끈다.
TELEGRAM_DAILY_SUMMARY_HOUR = int(os.getenv("TELEGRAM_DAILY_SUMMARY_HOUR", "9"))
