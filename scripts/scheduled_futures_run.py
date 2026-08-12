import json
import logging
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

LOG_PATH = PROJECT_ROOT / "logs" / "futures_scheduler.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("scheduled_futures_run")

if __name__ == "__main__":
    from src.futures_pipeline import run_futures_cycle

    logger.info("futures cycle start")
    try:
        cycle = run_futures_cycle()
        entry = cycle["results"][0]

        print(f"마진 자산: ${cycle['margin_equity']:,.2f} | 오늘 손익률: {cycle['daily_pnl_pct'] * 100:+.2f}%")
        print(f"포지션 보유 중: {cycle['has_position']}")
        if "decision" in entry:
            d = entry["decision"]
            print(f"판단: {d['decision']} (신뢰도 {d['confidence']:.2f}) — {d['reasoning']}")
        if "execution" in entry:
            print(f"실행 결과: {entry['execution']['status']}")
        if "event" in entry:
            print(f"이벤트: {entry['event']} — {entry.get('reason', '')}")

        logger.info("futures cycle done: %s", json.dumps(
            {"has_position": cycle["has_position"], "decision": entry.get("decision", {}).get("decision"),
             "event": entry.get("event")}, ensure_ascii=False))
    except Exception:
        logger.exception("futures cycle failed")
        raise
