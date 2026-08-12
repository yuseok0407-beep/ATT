import json
import logging
import sys
from pathlib import Path

# Windows 콘솔 기본 코드페이지(cp949)는 —, ⚠ 같은 문자를 인코딩하지 못해 요약표 출력이 깨진다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

LOG_PATH = PROJECT_ROOT / "logs" / "scheduler.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("scheduled_run")

if __name__ == "__main__":
    from src.monitoring.summary import format_summary
    from src.pipeline import run_cycle

    logger.info("cycle start")
    try:
        cycle = run_cycle()
        summary_text = format_summary(cycle)
        print(summary_text)

        log_summary = [
            {"symbol": r.get("symbol"), "decision": (r.get("decision") or {}).get("decision"),
             "event": r.get("event")}
            for r in cycle["results"]
        ]
        logger.info("cycle done: %s", json.dumps(log_summary, ensure_ascii=False))
    except Exception:
        logger.exception("cycle failed")
        raise
