import logging
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

LOG_PATH = PROJECT_ROOT / "logs" / "futures_rule_bot.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("run_futures_bot")

_SILENT_EVENTS = ("no_signal", "holding_position", "skipped_max_positions")

if __name__ == "__main__":
    from src.core.config import FUTURES_SYMBOLS, MAX_CONCURRENT_POSITIONS, POLL_INTERVAL_SECONDS
    from src.data.futures_exchange import get_futures_client
    from src.execution.heartbeat import write_heartbeat
    from src.futures_rule_bot import initialize, run_once

    logger.info("futures rule bot starting — watching %s (max %d concurrent, poll every %ss)",
                ", ".join(FUTURES_SYMBOLS), MAX_CONCURRENT_POSITIONS, POLL_INTERVAL_SECONDS)

    client = get_futures_client()
    leverage_by_symbol = initialize(client)
    symbols = list(leverage_by_symbol)
    skipped = [s for s in FUTURES_SYMBOLS if s not in symbols]
    if skipped:
        logger.warning("skipped (not available on this exchange, or leverage rejected with no usable tier): %s",
                        ", ".join(skipped))
    logger.info("leverage/margin mode set — entering monitoring loop. Ctrl+C to stop. %s",
                ", ".join(f"{s}={lev}x" for s, lev in leverage_by_symbol.items()))

    # 신호가 없으면 로그를 안 남기는 게 기본 동작이라, "봇이 조용한 것"과 "봇이 멈춘 것"을
    # 로그만 보고는 구분할 수 없었다 — 주기적 하트비트로 생존 여부를 항상 확인 가능하게 한다.
    HEARTBEAT_EVERY_N_CYCLES = max(1, int(600 / POLL_INTERVAL_SECONDS))  # 대략 10분마다
    cycle_count = 0

    while True:
        cycle_count += 1
        try:
            cycle = run_once(client, symbols=symbols, leverage_by_symbol=leverage_by_symbol)
            write_heartbeat(cycle_count, cycle.get("open_position_count", 0), cycle["margin_equity"])

            if cycle.get("event") == "circuit_breaker_blocked":
                logger.info("circuit_breaker_blocked margin_equity=%.2f", cycle["margin_equity"])
            else:
                for symbol, result in cycle["symbols"].items():
                    if result["event"] not in _SILENT_EVENTS:
                        logger.info("symbol=%s event=%s margin_equity=%.2f",
                                    symbol, result["event"], cycle["margin_equity"])
                if cycle_count % HEARTBEAT_EVERY_N_CYCLES == 0:
                    logger.info("heartbeat #%d — alive, open_positions=%d/%d margin_equity=%.2f",
                                cycle_count, cycle["open_position_count"], MAX_CONCURRENT_POSITIONS,
                                cycle["margin_equity"])
        except KeyboardInterrupt:
            logger.info("stopped by user")
            break
        except Exception:
            logger.exception("cycle raised an error — continuing after backoff")

        time.sleep(POLL_INTERVAL_SECONDS)
