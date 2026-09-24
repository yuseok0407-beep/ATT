import time

import pandas as pd

from src.data.futures_exchange import OHLCV_COLUMNS


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


def cache_path(cache_dir, symbol: str):
    """심볼 하나의 캔들 캐시 경로. 파일명 규칙을 한 곳에만 둔다."""
    from pathlib import Path
    return Path(cache_dir) / f"{symbol.replace('/', '_').replace(':', '-')}.json"


def load_cached_ohlcv(client, symbol: str, *, timeframe: str = "1h", days: int = 365,
                      cache_dir=None) -> pd.DataFrame:
    """캐시가 있으면 읽고, 없으면 받아서 저장한다.

    같은 캔들로 설정만 바꿔가며 수십 번 돌리는 실험 루프가 매번 거래소를 때리지 않게 하려는 것.
    `cache_dir`가 None이면 그냥 받아온다(옛 동작).
    """
    if cache_dir is None:
        return fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)

    path = cache_path(cache_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return pd.read_json(path)
    df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
    df.to_json(path)
    return df


def slice_window(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """[start, end) 구간만 남기고 봉 위치를 0부터 다시 매긴다.

    위치 재부여가 중요하다 — `portfolio.simulate_portfolio`가 봉을 **위치**로 다루므로
    인덱스를 안 고치면 잘라낸 구간의 거래가 엉뚱한 봉에 붙는다.
    """
    mask = pd.Series(True, index=df.index)
    if start is not None:
        mask &= df["timestamp"] >= start
    if end is not None:
        mask &= df["timestamp"] < end
    return df.loc[mask].reset_index(drop=True)
