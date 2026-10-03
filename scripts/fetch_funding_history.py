"""후보 종목들의 펀딩비 이력(공개 API)을 받는다 — 대안 전략 백테스트용(docs/ALT_XS_MOMENTUM.md).

`.ohlcv_cache_universe`에 캔들이 있는 종목만, 같은 기간. 결과: `.ohlcv_cache_universe/funding/*.json`.
펀딩 이력 요청은 IP당 5분 500회 한도가 따로 있다 — 천천히 받는다(봇과 IP를 같이 쓴다)."""
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from src.data.futures_exchange import get_futures_market_data_client  # noqa: E402

CACHE = PROJECT_ROOT / ".ohlcv_cache_universe"
START = pd.Timestamp("2025-09-22")
END = pd.Timestamp("2026-09-23")


def main() -> int:
    out_dir = CACHE / "funding"
    out_dir.mkdir(exist_ok=True)
    client = get_futures_market_data_client()
    client.load_markets()
    for path in sorted(CACHE.glob("*.json")):
        if path.name.startswith("_"):
            continue
        base, rest = path.stem.split("_", 1)
        market = client.markets.get(f"{base}/{rest.replace('-', ':')}")
        target = out_dir / path.name
        if market is None or target.exists():
            continue
        rows, since, end = [], int(START.timestamp() * 1000), int(END.timestamp() * 1000)
        while since < end:
            batch = client.fapiPublicGetFundingRate({"symbol": market["id"], "startTime": since, "limit": 1000})
            time.sleep(1.3)
            if not batch:
                break
            rows += [r for r in batch if int(r["fundingTime"]) < end]
            since = int(batch[-1]["fundingTime"]) + 1
            if len(batch) < 1000:
                break
        target.write_text(json.dumps([{"t": int(r["fundingTime"]), "rate": float(r["fundingRate"])} for r in rows]),
                          encoding="utf-8")
        print(f"{market['id']:<16}{len(rows):>5}건", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
