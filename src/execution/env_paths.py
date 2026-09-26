"""env → 그 계좌의 파일 경로 전부. 봇·대시보드·텔레그램·내보내기가 모두 여기서 받는다.

경로를 만드는 규칙 자체는 `core.envs.env_path` 한 곳이고, 이 모듈은 "어떤 파일들이 계좌별로
갈라지는가"의 목록이다. 새 계좌별 파일을 만들면 여기에 한 줄 더하면 된다.

거래소 클라이언트를 끌고 오지 않는다(파일 경로만 다룬다) — 내보내기처럼 거래소 연결 없이
도는 코드도 이걸 쓸 수 있어야 해서.
"""

from src.core.envs import env_path
from src.execution import equity_log, excursion, filter_stats, heartbeat

# 데모 경로. 데모 파일명은 절대 바꾸지 않는다 — 기존 이력이 끊긴다.
DEMO_PATHS = {
    "journal": "journal/futures_rule_trades.jsonl",
    "state": "state/futures_rule_daily_equity.json",
    "last_trade": "state/futures_rule_last_trade.json",
    "filter_stats": filter_stats.DEFAULT_PATH,
    "excursion": excursion.DEFAULT_PATH,
    "heartbeat": heartbeat.DEFAULT_PATH,
    "equity_log": equity_log.DEFAULT_PATH,
    "log": "logs/futures_rule_bot.log",
}


def paths_for(env: str) -> dict[str, str]:
    """모르는 env면 `UnknownEnvError` — 데모 경로로 폴백하지 않는다."""
    return {key: env_path(path, env) for key, path in DEMO_PATHS.items()}
