"""실거래 기간(2026-06 ~ 홀드아웃 시작 전)의 1시간봉을 받는다 — 실거래와 백테스트 대조용.

`.ohlcv_cache`(실험 데이터, 2026-09-22까지)를 건드리지 않으려고 따로 둔다. 홀드아웃
(`run_experiment.HOLDOUT_START`) 이후 봉은 받지 않는다. 6월부터 받는 이유: 상위봉 12h 필터의
워밍업(약 50일)."""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

import run_experiment as rx  # noqa: E402
from src.backtest.data import cache_path  # noqa: E402
from src.core.config import FUTURES_SYMBOLS  # noqa: E402
from src.data.futures_exchange import OHLCV_COLUMNS, get_futures_market_data_client  # noqa: E402

OUT = PROJECT_ROOT / ".ohlcv_cache_recent"
START = pd.Timestamp("2026-06-01")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    client = get_futures_market_data_client()
    client.load_markets()
    end = int(rx.HOLDOUT_START.timestamp() * 1000)
    for symbol in [s for s in FUTURES_SYMBOLS if s in client.markets]:
        rows, since = [], int(START.timestamp() * 1000)
        while since < end:
            batch = client.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
            time.sleep(0.6)   # 봇·대시보드와 IP 한도를 같이 쓴다
            if not batch:
                break
            rows += [r for r in batch if r[0] < end]
            since = batch[-1][0] + 3600_000
            if len(batch) < 1000:
                break
        df = pd.DataFrame(rows, columns=OHLCV_COLUMNS).drop_duplicates("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.to_json(cache_path(OUT, symbol))
        print(f"{symbol:<22}{len(df):>5}봉 ~ {df['timestamp'].iloc[-1]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
