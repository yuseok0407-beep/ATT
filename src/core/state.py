import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.core.config import CONSECUTIVE_LOSS_COOLDOWN_HOURS

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


def compute_consecutive_losses(entries: list[dict], cooldown_hours: float = None,
                                now: datetime = None) -> int:
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
    게 아니라서 승률/총손익 같은 다른 통계에는 영향이 없다.

    cooldown_hours (2026-09-22): 이 시간보다 오래된 청산은 세지 않는다 — **자동 해제**다.
    None이면 config의 `CONSECUTIVE_LOSS_COOLDOWN_HOURS`, 0이면 자동 해제 없음(옛 동작).

    이게 없을 때 이 함수는 "걸쇠"였다. 한도에 닿으면 신규 진입이 막히고, 막히면 새 청산이
    안 생기므로 카운터가 저절로 안 내려간다 — 포지션이 다 닫힌 뒤에 걸리면 사람이 수동 리셋을
    누를 때까지 영구 정지한다(2026-09-22에 데모 봇이 실제로 2일간 그 상태였다). 시간 창을 두면
    "마지막 손실로부터 N시간이 지나면 다시 센다"가 되어, 정지가 최대 N시간으로 묶인다.

    해제 규칙: **손실 뒤 N시간 동안 새 손실이 없으면 그 앞의 연속은 끊긴다.** 그래서 지금부터
    거꾸로 훑으며 "직전에 센 손실(처음엔 지금)과의 간격"이 N시간 이상이면 멈춘다 — 마지막 손실이
    N시간을 넘겼으면 0이고, 연속 중간에 N시간 넘는 공백이 있으면 그 앞은 세지 않는다.

    2026-10-03 정정(외부 검토 3.2): 예전 구현은 "지금으로부터 N시간 안의 손실만" 세는 이동
    창이었다. 손실이 25·4·3·2·1시간 전이면 마지막 손실이 1시간 전인데도 4로 세서 5연패 정지가
    안 걸렸다 — 위 설명(그리고 CLAUDE.md)과 다른 동작이다. 손실 사이 간격은 21시간뿐이라 규칙대로면
    연속 5다."""
    if cooldown_hours is None:
        cooldown_hours = CONSECUTIVE_LOSS_COOLDOWN_HOURS
    gap = timedelta(hours=cooldown_hours) if cooldown_hours and cooldown_hours > 0 else None
    anchor = (now or datetime.now(timezone.utc)) if gap is not None else None

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
        closed_at = _closed_at(entry) if gap is not None else None
        if closed_at is not None and anchor - closed_at >= gap:
            # 이 청산 뒤로 N시간 넘게 손실이 없었다 — 여기서(와 그 이전은) 연속이 끊겼다.
            break
        if realized_pnl < 0:
            count += 1
            if closed_at is not None:
                anchor = closed_at
        else:
            break
    return count


def _closed_at(entry: dict) -> datetime | None:
    """청산 기록의 시각(UTC). 타임스탬프가 없거나 못 읽으면 None — 시각을 모르는 기록을
    "오래됐다"고 단정해서 보호를 조용히 푸는 쪽으로 틀리지 않게, 호출자는 None을 "창 안"으로 본다."""
    timestamp = entry.get("timestamp")
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when
