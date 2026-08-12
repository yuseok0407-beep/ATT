import time

import pandas as pd

from src.data.exchange import OHLCV_COLUMNS


def fetch_historical_ohlcv(client, symbol: str, timeframe: str = "1h", days: int = 365) -> pd.DataFrame:
    """오늘로부터 최대 `days`일 전까지의 캔들을 페이지네이션으로 전부 받아온다.

    심볼의 실제 상장일이 그보다 늦으면(예: SOXL) 거래소가 상장일부터 자동으로 잘라서 주므로
    별도 처리 없이 짧은 기간만 반환된다."""
    ms_per_candle = client.parse_timeframe(timeframe) * 1000
    since = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000

    rows = []
    while True:
        batch = client.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + ms_per_candle
        if len(batch) < 1000:
            break
        time.sleep(client.rateLimit / 1000)

    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS).drop_duplicates(subset="timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df
