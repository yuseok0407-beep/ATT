import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import run_cycle

if __name__ == "__main__":
    results = run_cycle(consecutive_losses=0, daily_pnl_pct=0.0)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
