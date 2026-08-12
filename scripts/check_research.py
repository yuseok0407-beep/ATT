import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.research import build_snapshot

if __name__ == "__main__":
    snapshot = build_snapshot("BTC/USDT", timeframe="1h")
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
