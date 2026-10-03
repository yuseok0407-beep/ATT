"""날짜별 마진 자산(시작/종료)을 남긴다 — "그날 자산이 실제로 얼마에서 얼마가 됐나".

왜 필요한가(2026-09-24): 일일 요약이 `realized_pnl`만 말해서 "수익 +5 USDT"라고 알리는데
계좌 총자산은 그대로이거나 오히려 줄어 있는 일이 있었다. 이유가 두 개다.

1. **거래소의 `realizedPnl`에는 수수료가 안 들어있다**(`commission`이 별개 필드). 이 전략은
   건당 기대값과 수수료가 같은 크기라 부호가 뒤집힐 수 있다.
2. 실현손익은 **그날 청산된 거래**만 센다. 아직 들고 있는 포지션의 평가손익 변화, 펀딩비,
   입출금은 안 들어간다 — 그런데 사용자가 보는 "총 자산"에는 전부 들어있다.

그래서 R이나 실현손익 대신 **자산 그 자체의 변화**를 따로 기록한다. 이것만이 계좌 화면의
숫자와 직접 맞춰볼 수 있는 값이다.

`state/futures_rule_daily_equity*.json`(서킷브레이커용)과 파일을 나눈 이유: 그쪽은 **오늘의
시작 자산 한 줄**만 들고 날짜가 바뀌면 덮어쓴다(일일 손실 한도를 재는 것이 목적이라 과거가
필요 없다). 거기에 이력을 얹으면 손실 한도 로직이 읽는 파일의 형식이 바뀌어, 한도가 잘못
계산되면 실거래 리스크가 된다.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_PATH = "state/futures_rule_equity_log.json"
LIVE_DEFAULT_PATH = "state/futures_rule_equity_log.live.json"  # 실계좌 봇 전용

# 일일 요약이 전날 하루만 보므로 길게 들 이유는 없지만, 주/월 단위로 되돌아볼 수 있을 만큼은
# 남긴다. 하루 한 줄이라 120일이어도 파일이 몇 KB다.
KEEP_DAYS = 120


def _load(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def record(equity: float, path: str = DEFAULT_PATH, now: datetime = None) -> dict:
    """이번 사이클의 자산을 오늘 날짜에 기록한다. 하트비트와 같은 자리에서 매 사이클 불린다.

    그날 **처음** 본 값이 `start`, **마지막으로** 본 값이 `end`가 된다. 봇이 꺼져 있던 시간은
    알 수 없으므로 `start`는 "봇이 그날 처음 확인한 자산"이라는 뜻이고, 그 이상을 주장하지
    않는다(밤새 꺼져 있었으면 전날 `end`와 벌어질 수 있다 — 요약이 그 간극을 같이 보여준다).
    """
    if equity is None or equity <= 0:
        return _load(path)

    now = now or datetime.now().astimezone()
    today = now.date().isoformat()
    data = _load(path)

    day = data.get(today)
    if not isinstance(day, dict) or "start" not in day:
        # 입출금 기록(record_transfers)이 먼저 이 날을 만들었을 수 있다 — 그건 지우지 않는다.
        day = {**(day if isinstance(day, dict) else {}), "start": float(equity), "start_at": now.isoformat()}
    day["end"] = float(equity)
    day["end_at"] = now.isoformat()
    data[today] = day

    cutoff = (date.fromisoformat(today).toordinal() - KEEP_DAYS)
    data = {d: v for d, v in data.items()
            if _ordinal(d) is not None and _ordinal(d) >= cutoff}

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return data


def record_transfers(rows: list[dict], path: str = DEFAULT_PATH) -> int:
    """거래소 입출금(수입 내역의 TRANSFER 행)을 그 날짜에 붙인다. 새로 붙인 건수를 돌려준다.

    외부 검토 3.3(2026-10-03): 자산 수익률이 `(끝-처음)/처음`이라 **입금이 수익으로** 계산됐다.
    입출금을 따로 남겨 `period_return`/`day_change`가 빼게 한다. 같은 내역을 여러 번 받아도 되도록
    거래소 `tranId`로 중복을 거른다. 날짜는 봇 PC의 로컬 날짜(record와 같은 기준)."""
    data = _load(path)
    seen = {t.get("id") for day in data.values() if isinstance(day, dict)
            for t in day.get("transfers", [])}
    added = 0
    for row in rows:
        tran_id = str(row.get("tranId"))
        if tran_id in seen or row.get("asset") != "USDT":
            continue
        at = datetime.fromtimestamp(int(row["time"]) / 1000, timezone.utc).astimezone()
        day = data.setdefault(at.date().isoformat(), {})
        day.setdefault("transfers", []).append(
            {"id": tran_id, "at": at.isoformat(), "amount": float(row["income"])})
        seen.add(tran_id)
        added += 1
    if added:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return added


def _transfers_between(data: dict, start_at: str | None, end_at: str | None) -> float:
    """(start_at, end_at] 사이 입출금 합계. 시각을 모르면 그 끝은 열어 둔다."""
    lo = datetime.fromisoformat(start_at) if start_at else None
    hi = datetime.fromisoformat(end_at) if end_at else None
    total = 0.0
    for day in data.values():
        if not isinstance(day, dict):
            continue
        for t in day.get("transfers", []):
            at = datetime.fromisoformat(t["at"])
            if (lo is None or at > lo) and (hi is None or at <= hi):
                total += float(t["amount"])
    return total


def _ordinal(day: str) -> int | None:
    try:
        return date.fromisoformat(day).toordinal()
    except (TypeError, ValueError):
        return None


def day_change(day: str, path: str = DEFAULT_PATH) -> dict | None:
    """그날의 자산 변화. 기록이 없으면 None(요약은 그 줄을 빼고 나간다).

    `prev_end`는 전날 마지막으로 본 자산이다. 그날 `start`와 다르면 봇이 꺼져 있는 동안 자산이
    움직였다는 뜻이라(미결제 포지션의 평가손익, 펀딩비, 입출금), 요약이 그걸 숨기지 않는다.
    """
    data = _load(path)
    record_ = data.get(day)
    if not isinstance(record_, dict) or "start" not in record_ or "end" not in record_:
        return None

    start, end = float(record_["start"]), float(record_["end"])
    transfers = _transfers_between(data, record_.get("start_at"), record_.get("end_at"))
    previous = [d for d in data if _ordinal(d) is not None and d < day]
    prev_end = None
    if previous:
        last = max(previous)
        value = data[last].get("end")
        prev_end = float(value) if isinstance(value, (int, float)) else None

    return {
        "day": day,
        "start": start,
        "end": end,
        "change": end - start,
        "change_pct": ((end - start) / start) if start > 0 else None,
        # 그날 입출금과, 그것을 뺀 거래로 인한 변화(2026-10-03)
        "transfers": transfers,
        "trading_change": end - start - transfers,
        "prev_end": prev_end,
        "overnight_change": (start - prev_end) if prev_end is not None else None,
    }


def read_days(path: str = DEFAULT_PATH, days: int = KEEP_DAYS) -> dict:
    """최근 N일치 기록. 월 수익률처럼 여러 날을 묶어 볼 때 쓴다."""
    # 입출금만 있고 자산 기록이 없는 날(봇이 꺼져 있던 날)은 자산 경로가 아니다 — 뺀다.
    data = {d: v for d, v in _load(path).items() if isinstance(v, dict) and "start" in v}
    if not data:
        return {}
    cutoff = max(_ordinal(d) or 0 for d in data) - days + 1
    return {d: v for d, v in sorted(data.items())
            if _ordinal(d) is not None and _ordinal(d) >= cutoff}


def period_return(path: str = DEFAULT_PATH, days: int = 30) -> dict | None:
    """최근 N일 자산 수익률(입출금 제외) — 목표(월 +5~10%)와 직접 비교할 수 있는 유일한 값.

    R이나 실현손익 합계로는 이 값을 못 낸다: 사이징이 바뀌면 같은 R이 다른 금액이 되고,
    미실현 변동·펀딩비·입출금이 빠져 있다.
    """
    window = read_days(path, days)
    if len(window) < 2:
        return None
    first, last = min(window), max(window)
    start, end = window[first].get("start"), window[last].get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start <= 0:
        return None
    # 입금은 수익이 아니다(외부 검토 3.3) — 기간 안의 입출금을 빼고 잰다. 입금이 기간 중간에
    # 들어오면 그 돈은 기간 일부만 일했지만, 단순하게 끝 자산에서만 뺀다(분모는 시작 자산).
    transfers = _transfers_between(_load(path), window[first].get("start_at"), window[last].get("end_at"))
    return {
        "from": first, "to": last, "days": len(window),
        "start": float(start), "end": float(end), "transfers": transfers,
        "return_pct": (float(end) - transfers - float(start)) / float(start),
    }
