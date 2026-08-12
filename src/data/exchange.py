import ccxt
import pandas as pd

from src.core.config import BINANCE_API_KEY, BINANCE_API_SECRET, USE_TESTNET

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def get_client() -> ccxt.binance:
    """계좌 조회/주문 실행용 클라이언트. USE_TESTNET=true면 테스트넷(가상 자금)으로 라우팅."""
    client = ccxt.binance({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if USE_TESTNET:
        client.set_sandbox_mode(True)
    return client


def get_market_data_client() -> ccxt.binance:
    """시세/캔들 조회 전용 클라이언트. 테스트넷은 과거 데이터가 짧아(수십 개) 지표 계산에
    부적합하므로, 키 없이 실거래소의 공개 데이터를 사용한다. 주문/잔고는 절대 다루지 않음."""
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def get_balance(client: ccxt.binance) -> dict:
    return client.fetch_balance()


def get_ticker(client: ccxt.binance, symbol: str) -> dict:
    return client.fetch_ticker(symbol)


def get_ohlcv(client: ccxt.binance, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
    return client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def fetch_ohlcv_df(client: ccxt.binance, symbol: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
    ohlcv = get_ohlcv(client, symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df
