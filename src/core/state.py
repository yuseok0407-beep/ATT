import json
from datetime import date
from pathlib import Path

STATE_PATH = "state/daily_equity.json"


def get_daily_pnl_pct(current_equity: float, path: str = STATE_PATH) -> float:
    """당일 시작 자산 대비 현재 손익률을 계산한다. 날짜가 바뀌면 오늘의 시작 자산으로 재설정한다.

    포지션별 손익 귀속(랏 단위 원가 추적)은 하지 않으므로 연속 손실 횟수(consecutive_losses)는
    이 상태만으로는 계산할 수 없다 — 향후 거래별 실현손익 기록이 필요한 별도 작업.
    """
    file_path = Path(path)
    today = date.today().isoformat()

    state = None
    if file_path.exists():
        state = json.loads(file_path.read_text(encoding="utf-8"))

    if state is None or state.get("date") != today:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps({"date": today, "start_equity": current_equity}), encoding="utf-8"
        )
        return 0.0

    start_equity = state.get("start_equity", 0.0)
    if start_equity <= 0:
        return 0.0
    return (current_equity - start_equity) / start_equity
