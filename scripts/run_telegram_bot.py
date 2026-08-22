import logging
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

LOG_PATH = PROJECT_ROOT / "logs" / "telegram_bot.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("run_telegram_bot")

if __name__ == "__main__":
    from src.core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_POLL_INTERVAL_SECONDS
    from src.telegram_bot import STATE_PATH, load_state, run_once, save_state

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 .env에 설정되지 않았습니다 — 종료합니다.")
        sys.exit(1)

    logger.info("telegram bot starting — polling every ~%ss (long-poll), notifying chat_id=%s",
                TELEGRAM_POLL_INTERVAL_SECONDS, TELEGRAM_CHAT_ID)

    state = load_state(STATE_PATH)

    while True:
        try:
            state = run_once(state, TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN)
            save_state(state, STATE_PATH)
        except KeyboardInterrupt:
            logger.info("stopped by user")
            break
        except Exception:
            # get_updates()의 long-poll 대기가 정상 경로에선 이미 주기 역할을 하므로, 예외가 나서
            # 그 대기를 못 거친 경우에만 여기서 백오프 삼아 잠깐 쉰다.
            logger.exception("cycle raised an error — continuing after backoff")
            time.sleep(TELEGRAM_POLL_INTERVAL_SECONDS)
