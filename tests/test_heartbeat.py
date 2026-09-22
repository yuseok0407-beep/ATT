import json
from datetime import datetime, timedelta, timezone

from src.execution.heartbeat import read_heartbeat, write_heartbeat


def test_read_heartbeat_missing_file_returns_none(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert read_heartbeat(path=str(path)) is None


def test_write_then_read_heartbeat_roundtrips_fields(tmp_path):
    path = tmp_path / "state" / "heartbeat.json"
    write_heartbeat(cycle_count=5, open_positions=2, margin_equity=1234.56, path=str(path))

    result = read_heartbeat(path=str(path))
    assert result["cycle_count"] == 5
    assert result["open_positions"] == 2
    assert result["margin_equity"] == 1234.56
    assert "timestamp" in result


def test_read_heartbeat_computes_small_age_for_fresh_write(tmp_path):
    path = tmp_path / "heartbeat.json"
    write_heartbeat(cycle_count=1, open_positions=0, margin_equity=100.0, path=str(path))

    result = read_heartbeat(path=str(path))
    assert 0 <= result["age_seconds"] < 5


def test_read_heartbeat_computes_large_age_for_stale_record(tmp_path):
    path = tmp_path / "heartbeat.json"
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    path.write_text(json.dumps({
        "timestamp": old_ts, "cycle_count": 1, "open_positions": 0, "margin_equity": 100.0,
    }), encoding="utf-8")

    result = read_heartbeat(path=str(path))
    assert result["age_seconds"] >= 30 * 60 - 2


def test_heartbeat_records_whether_the_circuit_breaker_blocked_this_cycle(tmp_path):
    """저널의 circuit_breaker_blocked는 시작 한 줄뿐이라 '지금도 막혀 있는가'는 여기서만 알 수 있다."""
    path = tmp_path / "heartbeat.json"
    write_heartbeat(cycle_count=1, open_positions=0, margin_equity=100.0, path=str(path),
                    breaker_reason="연속 손실 5회로 임계치 도달")
    blocked = read_heartbeat(path=str(path))
    assert blocked["circuit_breaker_blocked"] is True
    assert blocked["breaker_reason"] == "연속 손실 5회로 임계치 도달"

    write_heartbeat(cycle_count=2, open_positions=0, margin_equity=100.0, path=str(path))
    assert read_heartbeat(path=str(path))["circuit_breaker_blocked"] is False
