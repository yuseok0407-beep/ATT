import json
from datetime import datetime, timedelta, timezone

from src.core.state import compute_consecutive_losses, get_daily_pnl_pct


def test_first_call_today_sets_baseline_and_returns_zero(tmp_path):
    path = tmp_path / "daily_equity.json"
    pnl = get_daily_pnl_pct(10_000, path=str(path))
    assert pnl == 0.0
    saved = json.loads(path.read_text())
    assert saved["start_equity"] == 10_000


def test_same_day_computes_pnl_against_baseline(tmp_path):
    path = tmp_path / "daily_equity.json"
    get_daily_pnl_pct(10_000, path=str(path))
    pnl = get_daily_pnl_pct(9_500, path=str(path))
    assert pnl == -0.05


def test_new_day_resets_baseline(tmp_path):
    path = tmp_path / "daily_equity.json"
    path.write_text(json.dumps({"date": "2000-01-01", "start_equity": 5_000}))
    pnl = get_daily_pnl_pct(9_000, path=str(path))
    assert pnl == 0.0
    saved = json.loads(path.read_text())
    assert saved["start_equity"] == 9_000


def test_zero_baseline_does_not_divide_by_zero(tmp_path):
    path = tmp_path / "daily_equity.json"
    path.write_text(json.dumps({"date": "2099-01-01", "start_equity": 0}))
    import unittest.mock as mock

    with mock.patch("src.core.state.date") as mock_date:
        mock_date.today.return_value.isoformat.return_value = "2099-01-01"
        pnl = get_daily_pnl_pct(1000, path=str(path))
    assert pnl == 0.0


def _closed(realized_pnl):
    return {"event": "closed", "realized_pnl": realized_pnl}


class TestComputeConsecutiveLosses:
    def test_no_entries_returns_zero(self):
        assert compute_consecutive_losses([]) == 0

    def test_ignores_non_closed_events(self):
        entries = [{"event": "entered"}, _closed(-10.0), {"event": "holding_position"}]
        assert compute_consecutive_losses(entries) == 1

    def test_counts_trailing_losses_only(self):
        entries = [_closed(-5.0), _closed(20.0), _closed(-1.0), _closed(-2.0)]
        assert compute_consecutive_losses(entries) == 2

    def test_win_immediately_after_a_loss_resets_to_zero(self):
        entries = [_closed(-5.0), _closed(3.0)]
        assert compute_consecutive_losses(entries) == 0

    def test_zero_pnl_close_counts_as_a_stop_not_a_loss(self):
        entries = [_closed(-5.0), _closed(0.0)]
        assert compute_consecutive_losses(entries) == 0

    def test_all_losses_counts_every_one(self):
        entries = [_closed(-1.0), _closed(-2.0), _closed(-3.0)]
        assert compute_consecutive_losses(entries) == 3

    def test_manual_loss_is_skipped_and_does_not_break_the_streak(self):
        """실전 버그 재현: 사용자가 테스트로 넣은 수동 거래가 손실로 끝나면서 연속손실
        카운트를 오염시켰다 — reason=manual인 청산은 세지 않고 더 과거를 계속 봐야 한다."""
        manual_loss = {"event": "closed", "realized_pnl": -1.0, "reason": "manual"}
        entries = [_closed(-5.0), _closed(-6.0), manual_loss]
        assert compute_consecutive_losses(entries) == 2

    def test_manual_win_between_real_losses_does_not_reset_streak(self):
        manual_win = {"event": "closed", "realized_pnl": 100.0, "reason": "manual"}
        entries = [_closed(-5.0), manual_win, _closed(-6.0)]
        assert compute_consecutive_losses(entries) == 2

    def test_reset_event_stops_the_scan_before_older_losses(self):
        entries = [_closed(-5.0), _closed(-6.0), {"event": "consecutive_loss_reset"}, _closed(-1.0)]
        assert compute_consecutive_losses(entries) == 1

    def test_reset_event_with_no_losses_after_it_returns_zero(self):
        entries = [_closed(-5.0), _closed(-6.0), {"event": "consecutive_loss_reset"}]
        assert compute_consecutive_losses(entries) == 0

    def test_reset_event_with_a_win_immediately_after_still_returns_zero(self):
        entries = [_closed(-5.0), {"event": "consecutive_loss_reset"}, _closed(3.0)]
        assert compute_consecutive_losses(entries) == 0


# ------------------------- 연속손실 자동 쿨다운 (2026-09-22)
# 이 브레이커는 원래 자동 해제가 없는 "걸쇠"였다 — 한도에 닿으면 진입이 막히고, 막히면 새 청산이
# 안 생기므로 카운터가 저절로 안 내려간다. 포지션이 다 닫힌 뒤 걸리면 사람이 수동 리셋을 누를
# 때까지 영구 정지한다(2026-09-22에 데모 봇이 실제로 2일간 그 상태였다: 5/5, 마지막 거래 09-20).


class TestConsecutiveLossCooldown:
    NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    def _loss(self, hours_ago, pnl=-10.0):
        when = self.NOW - timedelta(hours=hours_ago)
        return {"timestamp": when.isoformat(), "event": "closed", "realized_pnl": pnl}

    def test_losses_older_than_the_cooldown_stop_counting(self):
        """마지막 손실 이후 쿨다운이 지났으면 카운트가 0으로 돌아간다 — 자동 해제."""
        entries = [self._loss(h) for h in (50, 48, 46, 44, 42)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 0

    def test_recent_losses_still_count(self):
        entries = [self._loss(h) for h in (5, 4, 3)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 3

    def test_window_is_anchored_on_the_most_recent_loss_not_the_oldest(self):
        """오래된 손실이 섞여 있어도 최근 손실이 창 안이면 그 연속은 유지된다 — 새 손실이
        닫히면 창이 다시 밀리는 동작(그래서 손실이 계속되는 동안에는 보호가 풀리지 않는다)."""
        entries = [self._loss(40), self._loss(2), self._loss(1)]

        # 40시간 전 것은 창 밖이라 거기서 멈추고, 최근 2건만 센다.
        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 2

    def test_a_streak_with_short_gaps_keeps_counting_past_the_24h_mark(self):
        """외부 검토 3.2(2026-10-03): 손실이 25·4·3·2·1시간 전이면 손실 사이 간격이 전부 24시간
        미만이라 연속 5다. 예전 이동 창 구현은 25시간 전 것을 빼고 4로 세서 정지가 안 걸렸다."""
        entries = [self._loss(h) for h in (25, 4, 3, 2, 1)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 5

    def test_cooldown_zero_keeps_the_old_latch_behaviour(self):
        """0이면 자동 해제 없음 — 옛 동작을 그대로 쓸 수 있어야 한다(되돌릴 수 있는 변경)."""
        entries = [self._loss(h) for h in (50, 48, 46, 44, 42)]

        assert compute_consecutive_losses(entries, cooldown_hours=0, now=self.NOW) == 5

    def test_a_win_inside_the_window_still_stops_the_count(self):
        """쿨다운은 기존 규칙을 대체하는 게 아니라 얹히는 것 — 이익 청산은 그대로 카운트를 끊는다."""
        entries = [self._loss(5), {"timestamp": (self.NOW - timedelta(hours=4)).isoformat(),
                                    "event": "closed", "realized_pnl": 20.0},
                    self._loss(3), self._loss(2)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 2

    def test_manual_reset_boundary_still_wins(self):
        entries = [self._loss(5), {"event": "consecutive_loss_reset"}, self._loss(2)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 1

    def test_records_without_a_timestamp_are_not_treated_as_expired(self):
        """시각을 모르는 기록을 "오래됐다"고 단정하면 보호가 조용히 풀린다 — 보수적으로 센다."""
        entries = [{"event": "closed", "realized_pnl": -10.0} for _ in range(5)]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 5

    def test_naive_timestamps_are_read_as_utc(self):
        """저널은 항상 tz를 붙이지만, 옛 기록이나 손으로 만든 기록이 naive일 수 있다 —
        비교 자체가 터지면 브레이커 계산이 예외로 죽는다."""
        naive = (self.NOW - timedelta(hours=50)).replace(tzinfo=None).isoformat()
        entries = [{"timestamp": naive, "event": "closed", "realized_pnl": -10.0}]

        assert compute_consecutive_losses(entries, cooldown_hours=24, now=self.NOW) == 0

    def test_defaults_to_the_configured_cooldown(self):
        from src.core import config

        entries = [self._loss(config.CONSECUTIVE_LOSS_COOLDOWN_HOURS + 1)]

        assert compute_consecutive_losses(entries, now=self.NOW) == 0
