import json
from datetime import date
from pathlib import Path

STATE_PATH = "state/daily_equity.json"


def get_daily_pnl_pct(current_equity: float, path: str = STATE_PATH) -> float:
    """당일 시작 자산 대비 현재 손익률을 계산한다. 날짜가 바뀌면 오늘의 시작 자산으로 재설정한다."""
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


def compute_consecutive_losses(entries: list[dict]) -> int:
    """저널의 청산 기록(event=="closed")을 최신순으로 훑어 realized_pnl이 음수인 게 몇 번
    연속으로 이어지는지 센다. 손실이 아닌 청산(승리 또는 손익 0)을 만나는 순간 멈춘다 —
    거래당 실현손익이 저널에 남기 시작한 뒤로 실제로 계산 가능해졌다(이전엔 랏 단위 원가
    추적이 없어 불가능하다고 여겨졌던 부분)."""
    count = 0
    for entry in reversed(entries):
        if entry.get("event") != "closed":
            continue
        realized_pnl = entry.get("realized_pnl")
        if realized_pnl is None:
            continue
        if realized_pnl < 0:
            count += 1
        else:
            break
    return count
