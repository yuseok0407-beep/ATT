import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "state/futures_rule_filter_stats.json"
LIVE_DEFAULT_PATH = "state/futures_rule_filter_stats.live.json"  # 실계좌 봇 전용

# 저널에 안 남는(_SILENT_EVENTS) 진입 차단 사유들의 일별 집계. 저널에 남기면 안 되는 이유는
# 그대로다 — 신호가 살아있는 동안 POLL_INTERVAL_SECONDS마다 계속 재발생해서 저널이 터진다.
# 하지만 "오늘 저변동으로 몇 번 걸렀나"는 방금 넣은 필터들이 백테스트대로 도는지 확인할 유일한
# 수단이라(2026-09-09), 저널 대신 카운터 파일에 누적한다.
TRACKED_EVENTS = ("skipped_regime", "skipped_htf", "skipped_low_volatility",
                  "skipped_same_signal_bar", "skipped_price_drift")

EVENT_LABELS = {
    "skipped_regime": "레짐숏차단",
    "skipped_htf": "상위봉역행",
    "skipped_low_volatility": "저변동",
    "skipped_same_signal_bar": "같은봉",
    "skipped_price_drift": "가격이탈",
}

RETENTION_DAYS = 14  # 이보다 오래된 날짜는 버린다 — 추세만 보면 되는 지표라 무한히 쌓을 이유가 없다


def _load(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 통계 파일이 깨졌다고 감시 루프를 멈출 이유는 없다 — 빈 상태로 다시 시작한다.
        logger.warning("filter stats file %s is unreadable — starting over", path)
        return {}


def _save(data: dict, path: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def record(event: str, symbol: str, signal_bar_timestamp: str | None,
           path: str = DEFAULT_PATH, today: str = None) -> bool:
    """차단 1건을 집계한다. 실제로 카운트가 올라갔으면 True.

    **같은 (심볼, 신호 봉)은 한 번만 센다.** 30초마다 도는 감시 루프에서 매 사이클을 그대로
    세면 1시간봉 하나가 최대 120으로 잡혀서 "저변동 차단 2400회" 같은 무의미한 숫자가 된다 —
    백테스트가 봉당 한 번만 판단하는 것과 의미를 맞춰서 "차단된 신호 개수"를 센다.

    signal_bar_timestamp가 없으면(중복 판정 기준이 없으면) 세지 않는다 — 부풀린 숫자보다
    누락이 낫다."""
    if event not in TRACKED_EVENTS or not signal_bar_timestamp:
        return False

    today = today or date.today().isoformat()
    data = _load(path)
    day = data.setdefault(today, {"counts": {}, "seen": {}})
    seen_key = f"{event}|{symbol}"
    if day["seen"].get(seen_key) == signal_bar_timestamp:
        return False

    day["seen"][seen_key] = signal_bar_timestamp
    day["counts"][event] = day["counts"].get(event, 0) + 1

    for old_day in sorted(data)[:-RETENTION_DAYS]:
        del data[old_day]
    _save(data, path)
    return True


def read_counts(path: str = DEFAULT_PATH, days: int = 2) -> dict:
    """최근 며칠치 집계를 {날짜: {이벤트: 건수}}로 돌려준다(최신 날짜부터). seen(중복판정용
    내부 상태)은 빼고 카운트만 — 대시보드/텔레그램이 그대로 렌더링할 수 있게."""
    data = _load(path)
    return {day: data[day].get("counts", {}) for day in sorted(data, reverse=True)[:days]}


def format_counts(counts: dict) -> str:
    """{이벤트: 건수}를 "저변동 12 · 같은봉 3" 한 줄로. 0건인 항목은 빼고, 전부 0이면 빈 문자열."""
    parts = [f"{EVENT_LABELS.get(event, event)} {counts[event]}"
             for event in TRACKED_EVENTS if counts.get(event)]
    return " · ".join(parts)
