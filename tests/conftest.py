import pytest

from dashboard import app as dashboard_app
from src import futures_rule_bot
from src.execution import excursion, filter_stats

# futures_rule_bot의 상태 파일 경로는 모듈 전역이고 호출 시점에 읽히므로, 여기서 tmp_path로
# 갈아끼우면 테스트가 실제 파일을 절대 못 건드린다.
_ISOLATED_PATHS = [
    (futures_rule_bot, "FILTER_STATS_PATH", "filter_stats.json"),
    (futures_rule_bot, "LIVE_FILTER_STATS_PATH", "filter_stats.live.json"),
    (futures_rule_bot, "EXCURSION_PATH", "excursion.json"),
    (futures_rule_bot, "LIVE_EXCURSION_PATH", "excursion.live.json"),
    # 대시보드는 임포트 시점에 이름을 복사해 가므로 그쪽도 같이 갈아끼워야 한다.
    (dashboard_app, "FILTER_STATS_PATH", "filter_stats.json"),
    (dashboard_app, "LIVE_FILTER_STATS_PATH", "filter_stats.live.json"),
    (dashboard_app, "EXCURSION_PATH", "excursion.json"),
    (dashboard_app, "LIVE_EXCURSION_PATH", "excursion.live.json"),
    (filter_stats, "DEFAULT_PATH", "filter_stats.json"),
    (filter_stats, "LIVE_DEFAULT_PATH", "filter_stats.live.json"),
    (excursion, "DEFAULT_PATH", "excursion.json"),
    (excursion, "LIVE_DEFAULT_PATH", "excursion.live.json"),
]


@pytest.fixture(autouse=True)
def isolate_bot_state_files(tmp_path, monkeypatch):
    """테스트가 실거래 상태 파일(state/futures_rule_*.json)을 읽거나 쓰지 못하게 한다.

    차단 통계/최고점 추적은 저널과 달리 경로를 안 넘기면 모듈 기본값으로 조용히 떨어지는데,
    그중 excursion.pop은 **읽는 게 아니라 지우는** 동작이라 테스트를 한 번 돌리는 것만으로
    실제 보유 포지션의 최고점 기록이 날아갈 수 있다. 개별 테스트가 경로를 넘기는 걸 잊어도
    사고가 안 나도록 여기서 구조적으로 막는다(2026-09-09)."""
    for module, attr, filename in _ISOLATED_PATHS:
        monkeypatch.setattr(module, attr, str(tmp_path / filename))
