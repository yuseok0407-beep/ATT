import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))

# 선물(futures) 전용 설정 — 바이낸스 선물 테스트넷(testnet.binancefuture.com)은 현물
# 테스트넷(testnet.binance.vision)과 별개 사이트라 키도 별도로 발급받아야 한다.
# 아래 BINANCE_FUTURES_API_KEY/SECRET은 데모 트레이딩(demo.binance.com) 전용 키다.
BINANCE_FUTURES_API_KEY = os.getenv("BINANCE_FUTURES_API_KEY", "")
BINANCE_FUTURES_API_SECRET = os.getenv("BINANCE_FUTURES_API_SECRET", "")
# 실계좌(진짜 자금) 전용 키 — binance.com 실제 계정의 API 관리에서 발급(데모 계정 아님).
# 데모와 실계좌를 동시에 운영하기 위한 별도 키 쌍(2026-08-22, get_futures_client(env=...) 참고).
BINANCE_FUTURES_LIVE_API_KEY = os.getenv("BINANCE_FUTURES_LIVE_API_KEY", "")
BINANCE_FUTURES_LIVE_API_SECRET = os.getenv("BINANCE_FUTURES_LIVE_API_SECRET", "")
FUTURES_SYMBOL = os.getenv("FUTURES_SYMBOL", "BTC/USDT:USDT")
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
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))

# 텔레그램 알림/원격 시작·중지 (2026-08-22) — src/telegram_bot.py, scripts/run_telegram_bot.py 참고.
# 토큰/채팅ID가 비어 있으면 run_telegram_bot.py가 시작 시 바로 종료한다(조용히 무동작하지 않음).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_POLL_INTERVAL_SECONDS = int(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", "15"))
