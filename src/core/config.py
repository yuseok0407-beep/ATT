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
BINANCE_FUTURES_API_KEY = os.getenv("BINANCE_FUTURES_API_KEY", "")
BINANCE_FUTURES_API_SECRET = os.getenv("BINANCE_FUTURES_API_SECRET", "")
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

# 다종목 감시 — 여러 심볼을 동시에 감시하되, 거래당 리스크(FUTURES_RISK_PER_TRADE)는 종목별로 그대로
# 두고 대신 "동시에 열 수 있는 포지션 개수"를 제한해 총 노출을 억제한다.
#
# 2026-08-12 스크리닝(scripts/run_symbol_screen_backtest.py, 36종목·1h·365일)+아웃오브샘플 검증
# (scripts/run_symbol_oos_backtest.py, 전반/후반 둘 다 총R 양수인 것만 PASS) 결과로 구성:
#   - BTC/ETH: OOS는 FAIL(후반 구간 마이너스)이지만 이미 실거래 중이라 사용자 판단으로 유지
#   - SOL/XRP: 스크리닝 최상위 + OOS PASS (엣지 가장 강함)
#   - CRCL/TSLA/BNB: 신규 추가, OOS PASS. SOXL은 데모 트레이딩 계좌에 아예 없어서(상장 자체가
#     안 됨, initialize()가 매번 건너뜀) 자리만 차지하던 걸 이걸로 교체
FUTURES_SYMBOLS = [s.strip() for s in os.getenv(
    "FUTURES_SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,CRCL/USDT:USDT,TSLA/USDT:USDT,BNB/USDT:USDT",
).split(",") if s.strip()]
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
