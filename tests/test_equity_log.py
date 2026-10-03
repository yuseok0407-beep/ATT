"""날짜별 자산 기록 — 일일 요약이 계좌 총자산과 맞아떨어지게 하는 근거 데이터.

여기서 지키는 것: 그날 처음/마지막 값이 정확히 잡히는가, 봇이 꺼져 있던 간극을 숨기지 않는가,
그리고 파일이 깨져도 봇이 죽지 않는가(감시 루프 안에서 매 사이클 불리므로).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.execution import equity_log
from src.execution.equity_log import day_change, period_return, read_days, record, record_transfers


def _at(day: str, hour: int = 9) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").astimezone()


def test_first_value_of_the_day_becomes_start_and_the_last_becomes_end(tmp_path):
    path = str(tmp_path / "equity.json")
    equity_log.record(100.0, path=path, now=_at("2026-09-20", 1))
    equity_log.record(120.0, path=path, now=_at("2026-09-20", 12))
    equity_log.record(110.0, path=path, now=_at("2026-09-20", 23))

    change = equity_log.day_change("2026-09-20", path=path)
    assert change["start"] == 100.0
    assert change["end"] == 110.0
    assert change["change"] == pytest.approx(10.0)
    assert change["change_pct"] == pytest.approx(0.10)


def test_days_are_kept_separate(tmp_path):
    path = str(tmp_path / "equity.json")
    equity_log.record(100.0, path=path, now=_at("2026-09-20"))
    equity_log.record(105.0, path=path, now=_at("2026-09-21"))

    assert equity_log.day_change("2026-09-20", path=path)["end"] == 100.0
    assert equity_log.day_change("2026-09-21", path=path)["start"] == 105.0


def test_overnight_gap_is_reported_not_hidden(tmp_path):
    """봇이 꺼져 있는 동안에도 자산은 움직인다(평가손익·펀딩비·입출금).

    그 차이를 안 보여주면 "어제 100으로 끝났는데 오늘 98에서 시작"이 설명 없이 남아서,
    사용자가 또 요약과 계좌 화면을 못 맞추게 된다."""
    path = str(tmp_path / "equity.json")
    equity_log.record(100.0, path=path, now=_at("2026-09-20"))
    equity_log.record(98.0, path=path, now=_at("2026-09-21"))

    change = equity_log.day_change("2026-09-21", path=path)
    assert change["prev_end"] == 100.0
    assert change["overnight_change"] == pytest.approx(-2.0)


def test_day_change_is_none_before_the_bot_has_recorded_anything(tmp_path):
    """새 코드로 봇을 재시작하기 전에는 기록이 없다 — 요약은 그 줄을 빼고 나가야 한다."""
    assert equity_log.day_change("2026-09-20", path=str(tmp_path / "missing.json")) is None


def test_a_corrupt_file_does_not_crash_the_watch_loop(tmp_path):
    """이 함수는 감시 루프 안에서 매 사이클 불린다. 여기서 예외가 나면 봇이 멈춘다."""
    path = tmp_path / "equity.json"
    path.write_text("{ 깨진 json", encoding="utf-8")
    equity_log.record(100.0, path=str(path), now=_at("2026-09-20"))
    assert equity_log.day_change("2026-09-20", path=str(path))["start"] == 100.0


def test_non_positive_equity_is_ignored(tmp_path):
    """거래소 조회가 실패해 0이 넘어오는 경우가 있다. 그걸 시작 자산으로 박으면
    수익률이 무한대가 된다."""
    path = str(tmp_path / "equity.json")
    equity_log.record(100.0, path=path, now=_at("2026-09-20"))
    equity_log.record(0.0, path=path, now=_at("2026-09-20", 20))
    assert equity_log.day_change("2026-09-20", path=path)["end"] == 100.0


def test_old_days_are_pruned(tmp_path):
    path = tmp_path / "equity.json"
    old = (_at("2026-09-20").date() - timedelta(days=equity_log.KEEP_DAYS + 10)).isoformat()
    path.write_text(json.dumps({old: {"start": 1.0, "end": 1.0}}), encoding="utf-8")
    equity_log.record(100.0, path=str(path), now=_at("2026-09-20"))
    assert old not in json.loads(path.read_text(encoding="utf-8"))


# --- 목표 대비 수익률 ------------------------------------------------------

def test_period_return_measures_equity_not_r(tmp_path):
    """월 +1% 목표는 금액 기준이다 — R 합계로는 답할 수 없다(사이징이 바뀌면 같은 R이
    다른 금액이고, 미실현 변동·펀딩비가 빠진다)."""
    path = str(tmp_path / "equity.json")
    for i, value in enumerate([1000.0, 1050.0, 1100.0]):
        equity_log.record(value, path=path, now=_at("2026-09-20") + timedelta(days=i))

    period = equity_log.period_return(path=path, days=30)
    assert period["start"] == 1000.0
    assert period["end"] == 1100.0
    assert period["return_pct"] == pytest.approx(0.10)
    assert period["days"] == 3


def test_period_return_needs_at_least_two_days(tmp_path):
    path = str(tmp_path / "equity.json")
    equity_log.record(1000.0, path=path, now=_at("2026-09-20"))
    assert equity_log.period_return(path=path, days=30) is None


# ---------------------------------------------- 입출금 (2026-10-03, 외부 검토 3.3)

def _transfer(tran_id, when, amount):
    return {"incomeType": "TRANSFER", "tranId": tran_id, "asset": "USDT", "income": str(amount),
            "time": int(when.timestamp() * 1000)}


def test_a_deposit_is_not_counted_as_return(tmp_path):
    """입금이 수익률에 들어가면 안 된다 — 1000에서 시작해 500을 입금하고 끝이 1510이면 +1%다."""
    path = str(tmp_path / "eq.json")
    kst = timezone(timedelta(hours=9))
    record(1000.0, path=path, now=datetime(2026, 9, 1, 9, 0, tzinfo=kst))
    record(1510.0, path=path, now=datetime(2026, 9, 2, 22, 0, tzinfo=kst))
    record_transfers([_transfer(1, datetime(2026, 9, 2, 12, 0, tzinfo=kst), 500.0)], path=path)

    period = period_return(path=path, days=30)

    assert period["transfers"] == pytest.approx(500.0)
    assert period["return_pct"] == pytest.approx(0.01)


def test_the_same_transfer_is_recorded_once(tmp_path):
    path = str(tmp_path / "eq.json")
    row = _transfer(7, datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc), 100.0)

    assert record_transfers([row], path=path) == 1
    assert record_transfers([row], path=path) == 0


def test_day_change_separates_transfers_from_trading(tmp_path):
    path = str(tmp_path / "eq.json")
    kst = timezone(timedelta(hours=9))
    record(1000.0, path=path, now=datetime(2026, 9, 2, 0, 5, tzinfo=kst))
    record(1090.0, path=path, now=datetime(2026, 9, 2, 23, 0, tzinfo=kst))
    record_transfers([_transfer(9, datetime(2026, 9, 2, 12, 0, tzinfo=kst), 100.0)], path=path)

    change = day_change("2026-09-02", path=path)

    assert change["transfers"] == pytest.approx(100.0)
    assert change["trading_change"] == pytest.approx(-10.0)


def test_a_transfer_only_day_stays_out_of_the_equity_path(tmp_path):
    """봇이 꺼져 있던 날 입금이 들어와도 자산 경로(차트)에 빈 날이 생기면 안 된다."""
    path = str(tmp_path / "eq.json")
    record(1000.0, path=path, now=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
    record_transfers([_transfer(3, datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), 50.0)], path=path)

    assert list(read_days(path=path)) == ["2026-09-01"]
