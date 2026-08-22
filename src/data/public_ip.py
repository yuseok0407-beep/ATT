import logging
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SECONDS = 120  # 외부 IP 조회 서비스를 매 폴링(5초)마다 두드리지 않기 위한 캐시

_cache = {"ip": None, "fetched_at": 0.0}


def get_public_ip(cache_seconds: int = DEFAULT_CACHE_SECONDS) -> str | None:
    """이 프로세스가 실행 중인 머신의 실제 공인 IP. 바이낸스 API 키의 IP 화이트리스트에
    등록해야 하는 값이 바로 이거다 — 사설 IP(예: 192.168.x.x)를 등록하면 절대 매칭될 수 없어서
    -2015 "Invalid API-key, IP, or permissions" 오류가 난다(2026-08-22 실전 확인). 대시보드와
    텔레그램 알림 봇이 공유해서 쓴다(dashboard/app.py, src/telegram_bot.py). 조회 실패(네트워크
    문제 등)는 이전에 성공한 값이라도 있으면 그걸, 없으면 None을 돌려주고 호출자를 막지 않는다."""
    now = time.monotonic()
    if _cache["ip"] is not None and now - _cache["fetched_at"] < cache_seconds:
        return _cache["ip"]
    try:
        resp = requests.get("https://api.ipify.org", timeout=3)
        resp.raise_for_status()
        ip = resp.text.strip()
    except Exception:
        logger.exception("failed to fetch public IP")
        return _cache["ip"]
    _cache["ip"] = ip
    _cache["fetched_at"] = now
    return ip
