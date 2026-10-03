"""U30 후보군 캔들 수집 — 기존 캐시와 같은 기간(2025-09-22 04:00 ~ 2026-09-22 03:00 UTC)."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8")

import pandas as pd

from src.backtest.data import cache_path
from src.data.futures_exchange import OHLCV_COLUMNS, get_futures_market_data_client

OUT = ROOT / ".ohlcv_cache_universe"
START = pd.Timestamp("2025-09-22 04:00")
END = pd.Timestamp("2026-09-22 03:00")  # 포함
STABLE = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "PYUSD", "RLUSD", "USD1", "XUSD", "BFUSD"}
PREFILTER = 80


def fetch(client, symbol):
    since = int(START.timestamp() * 1000)
    end_ms = int(END.timestamp() * 1000)
    rows = []
    while since <= end_ms:
        batch = client.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        time.sleep(0.6)  # 봇·대시보드와 IP 한도 공유 — 천천히
        if not batch:
            break
        rows.extend(b for b in batch if b[0] <= end_ms)
        since = batch[-1][0] + 3600_000
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS).drop_duplicates(subset="timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def main():
    client = get_futures_market_data_client()
    client.load_markets()
    perps = [m for m in client.markets.values()
             if m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
             and m.get("active") and m.get("base") not in STABLE]
    tickers = client.fetch_tickers([m["symbol"] for m in perps])
    ranked = sorted(perps, key=lambda m: (tickers.get(m["symbol"]) or {}).get("quoteVolume") or 0,
                    reverse=True)[:PREFILTER]
    OUT.mkdir(exist_ok=True)
    meta = []
    for m in ranked:
        sym = m["symbol"]
        path = cache_path(OUT, sym)
        if path.exists():
            df = pd.read_json(path)
        else:
            df = fetch(client, sym)
            df.to_json(path)
        meta.append({"symbol": sym, "bars": len(df),
                     "vol24h": (tickers.get(sym) or {}).get("quoteVolume"),
                     "first": str(df["timestamp"].iloc[0]) if len(df) else None})
        print(f"{sym:<22} {len(df):>5}봉 {meta[-1]['first']}", flush=True)
    (OUT / "_prefilter.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
