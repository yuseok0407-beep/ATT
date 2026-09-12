import argparse
import logging
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# --env는 로깅 설정(로그 파일 경로가 env별로 갈라짐)보다 먼저 파싱돼야 한다. 프로세스 식별에도
# 이 값이 argv에 그대로 남아있어야 한다(bot_process._is_our_bot_process가 cmdline에서
# "--env {env}"를 찾아 데모/실계좌 프로세스를 구분하므로, 2026-08-22).
_parser = argparse.ArgumentParser()
_parser.add_argument("--env", choices=["demo", "live"], default="demo")
_ENV = _parser.parse_args().env

LOG_PATH = PROJECT_ROOT / "logs" / ("futures_rule_bot.log" if _ENV == "demo" else "futures_rule_bot.live.log")
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
    from src.execution.heartbeat import LIVE_DEFAULT_PATH as HEARTBEAT_LIVE_PATH, write_heartbeat
    from src.execution.heartbeat import DEFAULT_PATH as HEARTBEAT_DEMO_PATH
    from src.futures_rule_bot import (
        LIVE_JOURNAL_PATH,
        LIVE_LAST_TRADE_STATE_PATH,
        LIVE_STATE_PATH,
        initialize,
        log_config_change,
        run_once,
    )
    from src.futures_rule_bot import JOURNAL_PATH as DEMO_JOURNAL_PATH
    from src.futures_rule_bot import LAST_TRADE_STATE_PATH as DEMO_LAST_TRADE_PATH
    from src.futures_rule_bot import STATE_PATH as DEMO_STATE_PATH

    env = _ENV
    journal_path = LIVE_JOURNAL_PATH if env == "live" else DEMO_JOURNAL_PATH
    state_path = LIVE_STATE_PATH if env == "live" else DEMO_STATE_PATH
    last_trade_path = LIVE_LAST_TRADE_STATE_PATH if env == "live" else DEMO_LAST_TRADE_PATH
    heartbeat_path = HEARTBEAT_LIVE_PATH if env == "live" else HEARTBEAT_DEMO_PATH

    logger.info("futures rule bot starting env=%s — watching %s (max %d concurrent, poll every %ss)",
                env, ", ".join(FUTURES_SYMBOLS), MAX_CONCURRENT_POSITIONS, POLL_INTERVAL_SECONDS)

    client = get_futures_client(env)
    leverage_by_symbol = initialize(client)
    symbols = list(leverage_by_symbol)
    skipped = [s for s in FUTURES_SYMBOLS if s not in symbols]
    if skipped:
        logger.warning("skipped (not available on this exchange, or leverage rejected with no usable tier): %s",
                        ", ".join(skipped))
    logger.info("leverage/margin mode set — entering monitoring loop. Ctrl+C to stop. %s",
                ", ".join(f"{s}={lev}x" for s, lev in leverage_by_symbol.items()))

    # 이 프로세스가 어떤 규칙으로 도는지를 저널에 남긴다(직전 기록과 같으면 아무것도 안 남긴다).
    # 나중에 성과를 볼 때 설정이 바뀐 경계를 저널만으로 알 수 있게 하려는 것 — 사람 기억이나
    # UPDATE_LOG.md에만 있으면 "이 구간은 어떤 규칙이었나"를 화면에서 대조할 수 없다.
    config_change = log_config_change(journal_path=journal_path)
    if config_change is not None:
        logger.info("strategy config recorded to journal: %s",
                    config_change["changes"] if not config_change["first_record"] else "(first record)")

    # 신호가 없으면 로그를 안 남기는 게 기본 동작이라, "봇이 조용한 것"과 "봇이 멈춘 것"을
    # 로그만 보고는 구분할 수 없었다 — 주기적 하트비트로 생존 여부를 항상 확인 가능하게 한다.
    HEARTBEAT_EVERY_N_CYCLES = max(1, int(600 / POLL_INTERVAL_SECONDS))  # 대략 10분마다
    cycle_count = 0

    while True:
        cycle_count += 1
        try:
            cycle = run_once(client, env=env, symbols=symbols, leverage_by_symbol=leverage_by_symbol,
                              journal_path=journal_path, state_path=state_path, last_trade_path=last_trade_path)
            write_heartbeat(cycle_count, cycle.get("open_position_count", 0), cycle["margin_equity"],
                             path=heartbeat_path)

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
