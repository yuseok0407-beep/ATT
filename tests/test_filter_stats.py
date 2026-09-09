import json

from src.execution import filter_stats


def _path(tmp_path):
    return str(tmp_path / "stats.json")


def test_record_counts_a_blocked_signal(tmp_path):
    path = _path(tmp_path)
    assert filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", "2026-09-09 01:00:00",
                                path=path, today="2026-09-09") is True
    assert filter_stats.read_counts(path=path) == {"2026-09-09": {"skipped_low_volatility": 1}}


def test_record_counts_the_same_signal_bar_only_once(tmp_path):
    """감시 루프는 30초마다 도는데 1시간봉 신호는 그동안 고정이라, 매 사이클을 그대로 세면
    한 봉이 최대 120으로 잡혀서 숫자 자체가 무의미해진다 — 백테스트가 봉당 한 번만 판단하는
    것과 의미를 맞춘다."""
    path = _path(tmp_path)
    for _ in range(120):
        filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", "2026-09-09 01:00:00",
                            path=path, today="2026-09-09")

    assert filter_stats.read_counts(path=path)["2026-09-09"]["skipped_low_volatility"] == 1


def test_record_counts_again_when_the_signal_bar_advances(tmp_path):
    path = _path(tmp_path)
    filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", "2026-09-09 01:00:00",
                        path=path, today="2026-09-09")
    filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", "2026-09-09 02:00:00",
                        path=path, today="2026-09-09")

    assert filter_stats.read_counts(path=path)["2026-09-09"]["skipped_low_volatility"] == 2


def test_record_counts_each_symbol_separately(tmp_path):
    path = _path(tmp_path)
    for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        filter_stats.record("skipped_regime", symbol, "2026-09-09 01:00:00",
                            path=path, today="2026-09-09")

    assert filter_stats.read_counts(path=path)["2026-09-09"]["skipped_regime"] == 2


def test_record_ignores_events_it_does_not_track(tmp_path):
    path = _path(tmp_path)
    assert filter_stats.record("entered", "BTC/USDT:USDT", "2026-09-09 01:00:00",
                                path=path, today="2026-09-09") is False
    assert filter_stats.read_counts(path=path) == {}


def test_record_ignores_a_missing_signal_bar(tmp_path):
    """중복 판정 기준이 없으면 세지 않는다 — 부풀린 숫자보다 누락이 낫다."""
    path = _path(tmp_path)
    assert filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", None,
                                path=path, today="2026-09-09") is False
    assert filter_stats.read_counts(path=path) == {}


def test_read_counts_returns_the_most_recent_days_first(tmp_path):
    path = _path(tmp_path)
    filter_stats.record("skipped_regime", "BTC/USDT:USDT", "bar1", path=path, today="2026-09-07")
    filter_stats.record("skipped_regime", "BTC/USDT:USDT", "bar2", path=path, today="2026-09-09")

    assert list(filter_stats.read_counts(path=path, days=2)) == ["2026-09-09", "2026-09-07"]
    assert list(filter_stats.read_counts(path=path, days=1)) == ["2026-09-09"]


def test_old_days_are_dropped(tmp_path):
    path = _path(tmp_path)
    for day in range(1, filter_stats.RETENTION_DAYS + 5):
        filter_stats.record("skipped_regime", "BTC/USDT:USDT", "bar",
                            path=path, today=f"2026-09-{day:02d}")

    stored = json.loads(open(path, encoding="utf-8").read())
    assert len(stored) == filter_stats.RETENTION_DAYS
    assert "2026-09-01" not in stored


def test_read_counts_survives_a_corrupted_file(tmp_path):
    """통계 파일이 깨졌다고 감시 루프가 멈추면 안 된다."""
    path = _path(tmp_path)
    open(path, "w", encoding="utf-8").write("{not json")

    assert filter_stats.read_counts(path=path) == {}
    assert filter_stats.record("skipped_regime", "BTC/USDT:USDT", "bar",
                                path=path, today="2026-09-09") is True


def test_format_counts_skips_zero_and_unknown_entries():
    text = filter_stats.format_counts({"skipped_low_volatility": 12, "skipped_regime": 0})
    assert text == "저변동 12"


def test_format_counts_is_empty_when_nothing_was_blocked():
    assert filter_stats.format_counts({}) == ""
