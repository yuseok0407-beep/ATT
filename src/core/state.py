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
    추적이 없어 불가능하다고 여겨졌던 부분).

    reason=="manual"인 청산(사용자가 대시보드/거래소에서 직접 넣고 직접 닫은 거래, 테스트
    주문 포함)은 건너뛰고 계속 더 과거를 본다 — 서킷브레이커는 "규칙 기반 신호가 계속
    틀리고 있다"는 걸 감지하려는 건데, 봇이 판단조차 안 한 수동 거래가 그 카운트에 섞이면
    안 된다(2026-08-14, 사용자가 테스트 삼아 넣은 주문이 연속손실 카운트를 오염시킨 사고로
    실제 확인됨).

    event=="consecutive_loss_reset" 기록을 만나면 그 즉시 멈춘다(과거 손실은 더 이상 안 셈) —
    이 이벤트는 별도 상태 파일 없이 저널 자체에 "여기부터 다시 센다"는 경계선만 남기는 용도다
    (2026-08-23, `futures_rule_bot.reset_consecutive_losses` 참고). 실현손익 기록을 건드리는
    게 아니라서 승률/총손익 같은 다른 통계에는 영향이 없다."""
    count = 0
    for entry in reversed(entries):
        if entry.get("event") == "consecutive_loss_reset":
            break
        if entry.get("event") != "closed":
            continue
        if entry.get("reason") == "manual":
            continue
        realized_pnl = entry.get("realized_pnl")
        if realized_pnl is None:
            continue
        if realized_pnl < 0:
            count += 1
        else:
            break
    return count
