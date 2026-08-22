import logging

import requests

from src.core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"


def send_message(text: str, chat_id: str = None, token: str = None) -> bool:
    """텔레그램으로 메시지 하나를 보낸다. 알림 실패가 감시 루프 전체를 죽이면 안 되므로 절대
    예외를 올리지 않는다 — 실패하면 로그만 남기고 False."""
    token = token or TELEGRAM_BOT_TOKEN
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("cannot send telegram message — token or chat_id not configured")
        return False
    try:
        resp = requests.post(
            _API_BASE.format(token=token) + "/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("failed to send telegram message")
        return False


def get_updates(offset: int = None, timeout: int = 20, token: str = None) -> list[dict]:
    """새 메시지(Update) 목록을 가져온다. long-polling — timeout초 동안 새 메시지가 없으면 빈
    응답으로 돌아온다(이 대기 자체가 감시 루프의 자연스러운 주기 역할을 겸함). 실패하면 빈 리스트
    — 호출자가 다음 사이클에 같은 offset으로 다시 시도하면 되므로 예외를 올릴 필요가 없다."""
    token = token or TELEGRAM_BOT_TOKEN
    if not token:
        logger.warning("cannot poll telegram updates — token not configured")
        return []
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            _API_BASE.format(token=token) + "/getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception:
        logger.exception("failed to fetch telegram updates")
        return []
