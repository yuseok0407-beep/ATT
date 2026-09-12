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
MAX_ENTRY_PRICE_DRIFT_R = float(os.getenv("MAX_ENTRY_PRICE_DRIFT_R", "0.5"))
# 저변동 구간 진입 차단 (2026-09-09) — ATR이 손절폭의 이 배수보다 작으면 진입하지 않는다.
# 근거: 손절 1.25%/익절 2.5%인데 ATR이 0.5%면 익절까지 5 ATR을 가야 한다. 1시간봉 스케일에서
# 그건 거의 안 일어나고 대신 시간이 흐르며 손절로 흘러간다 — 실제로 후보 신호 1999건을 특성별로
# 쪼개보면 저변동 구간이 일관되게 손실 구간이었다. ATR% 절대값이 아니라 손절폭 대비 비율로 쓰는
# 이유는 STOP_LOSS_PCT를 바꿔도 조건이 자동으로 따라오게 하기 위함.
# 포트폴리오 시뮬레이션(12종목·1h·365일, 동시보유/서킷브레이커 반영): 필터 없음은 4분할 OOS를
# 배정순서 50회 전부 FAIL(총R +157.6, MDD -32.0%)인 반면 k=0.64는 전부 PASS(+185.0, -25.6%).
# k를 0.4~1.2로 훑어도 전부 PASS라 특정 값에만 맞은 게 아니다. 0으로 두면 필터가 꺼진다.
MIN_ATR_TO_STOP_RATIO = float(os.getenv("MIN_ATR_TO_STOP_RATIO", "0.64"))

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

# 하루 한 번 전날 성과를 텔레그램으로 보낸다(2026-09-09). 로컬 시각 기준 이 시(hour)를 지나면
# 전날치를 한 번 쏜다 — 일일 손실 한도가 리셋되는 경계(date.today())와 같은 기준이라 "하루"의
# 정의가 대시보드와 어긋나지 않는다. -1이면 이 기능만 끈다.
TELEGRAM_DAILY_SUMMARY_HOUR = int(os.getenv("TELEGRAM_DAILY_SUMMARY_HOUR", "9"))
