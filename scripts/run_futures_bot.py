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
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              # 화면 출력은 stdout으로 — 대시보드가 분리해서 띄우면 stdout은 버려지고 stderr는
              # 시작 전 오류용 파일(logs/*_process.err)이라, 여기로 보내면 로그가 거기 중복으로 쌓인다.
              logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_futures_bot")

_SILENT_EVENTS = ("no_signal", "holding_position", "skipped_max_positions")

if __name__ == "__main__":
    from src.core.config import FUTURES_SYMBOLS, MAX_CONCURRENT_POSITIONS, POLL_INTERVAL_SECONDS
    from src.data.futures_exchange import get_futures_client
    from src.execution.heartbeat import LIVE_DEFAULT_PATH as HEARTBEAT_LIVE_PATH, write_heartbeat
    from src.execution.heartbeat import DEFAULT_PATH as HEARTBEAT_DEMO_PATH
    from src.execution import bot_process, equity_log, income_ledger
    from src.futures_rule_bot import (
        LIVE_JOURNAL_PATH,
        LIVE_LAST_TRADE_STATE_PATH,
        LIVE_STATE_PATH,
        initialize,
        log_config_change,
        run_once,
        seconds_until_next_cycle,
    )
    from src.futures_rule_bot import JOURNAL_PATH as DEMO_JOURNAL_PATH
    from src.futures_rule_bot import LAST_TRADE_STATE_PATH as DEMO_LAST_TRADE_PATH
    from src.futures_rule_bot import STATE_PATH as DEMO_STATE_PATH

    env = _ENV
    journal_path = LIVE_JOURNAL_PATH if env == "live" else DEMO_JOURNAL_PATH
    state_path = LIVE_STATE_PATH if env == "live" else DEMO_STATE_PATH
    last_trade_path = LIVE_LAST_TRADE_STATE_PATH if env == "live" else DEMO_LAST_TRADE_PATH
    heartbeat_path = HEARTBEAT_LIVE_PATH if env == "live" else HEARTBEAT_DEMO_PATH
    equity_log_path = (equity_log.LIVE_DEFAULT_PATH if env == "live"
                       else equity_log.DEFAULT_PATH)

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

    # 입출금을 자산 기록에 붙인다(2026-10-03) — 안 그러면 입금이 수익률로 잡힌다. 수입 내역 조회는
    # 가중치 30이라 6시간에 한 번만. 시작할 때는 90일을 훑는다(같은 내역은 tranId로 걸러진다).
    TRANSFER_SYNC_SECONDS = 6 * 3600
    last_transfer_sync = 0.0

    def sync_transfers() -> None:
        lookback = 90 * 86400 if last_transfer_sync == 0.0 else TRANSFER_SYNC_SECONDS + 3600
        rows = income_ledger.fetch_income(client, int((time.time() - lookback) * 1000), income_type="TRANSFER")
        added = equity_log.record_transfers(rows, path=equity_log_path)
        if added:
            logger.info("recorded %d deposit/withdrawal(s) to the equity log", added)

    def wait_or_stop(seconds: float) -> bool:
        """seconds 동안 기다리되, 대시보드/텔레그램이 남긴 종료 요청을 1초마다 확인한다.
        봇은 띄운 창과 분리돼 있어 콘솔 신호(Ctrl+C)가 안 닿으므로 이게 정상 종료 경로다 —
        사이클(주문 포함)이 끝난 뒤에만 멈추므로 주문 도중에 끊기지 않는다."""
        deadline = time.time() + seconds
        while True:
            if bot_process.stop_requested(env):
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(1.0, remaining))

    while True:
        cycle_count += 1
        try:
            cycle = run_once(client, env=env, symbols=symbols, leverage_by_symbol=leverage_by_symbol,
                              journal_path=journal_path, state_path=state_path, last_trade_path=last_trade_path)
            blocked = cycle.get("event") == "circuit_breaker_blocked"
            write_heartbeat(cycle_count, cycle.get("open_position_count", 0), cycle["margin_equity"],
                             path=heartbeat_path, breaker_reason=(cycle.get("reason") or "사유 미상") if blocked else None)
            # 하트비트는 **마지막 한 순간**만 덮어쓰므로 "어제 자산이 얼마였나"를 답할 수 없다.
            # 일일 요약이 계좌 총자산과 맞춰볼 수 있으려면 날짜별 시작/종료가 남아야 한다.
            equity_log.record(cycle["margin_equity"], path=equity_log_path)
            if time.time() - last_transfer_sync >= TRANSFER_SYNC_SECONDS:
                try:
                    sync_transfers()
                except Exception:
                    logger.exception("deposit/withdrawal sync failed — retrying next window")
                last_transfer_sync = time.time()

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

        # 평소엔 POLL_INTERVAL_SECONDS, 봉 마감이 그보다 먼저 오면 마감 직후에 깬다
        if wait_or_stop(seconds_until_next_cycle(time.time(), POLL_INTERVAL_SECONDS)):
            logger.info("stop requested — exiting after the last completed cycle")
            break
