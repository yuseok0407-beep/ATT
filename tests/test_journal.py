import json

from src.execution.journal import append_entry, read_entries


def test_append_and_read_entries(tmp_path):
    path = tmp_path / "journal" / "trades.jsonl"

    append_entry({"decision": "BUY", "symbol": "BTC/USDT"}, path=str(path))
    append_entry({"decision": "NO_TRADE", "symbol": "ETH/USDT"}, path=str(path))

    entries = read_entries(path=str(path))
    assert len(entries) == 2
    assert entries[0]["decision"] == "BUY"
    assert "timestamp" in entries[0]


def test_read_entries_missing_file_returns_empty_list(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert read_entries(path=str(path)) == []


def test_read_entries_reuses_the_parse_when_the_file_is_unchanged(tmp_path, monkeypatch):
    """같은 사이클 안에서 저널을 10~20번 다시 읽는 구조라, 파일이 그대로면 다시 파싱하지
    않는다 — 저널이 커질수록 이 비용만 계속 늘기 때문."""
    path = tmp_path / "trades.jsonl"
    append_entry({"event": "entered", "symbol": "BTC/USDT:USDT"}, path=str(path))

    parses = []
    real_loads = json.loads
    monkeypatch.setattr(json, "loads", lambda s: parses.append(s) or real_loads(s))

    first = read_entries(path=str(path))
    second = read_entries(path=str(path))

    assert first == second
    assert len(parses) == 1  # 두 번째 호출은 파일을 다시 파싱하지 않는다


def test_read_entries_sees_an_append_from_another_process(tmp_path):
    """대시보드/텔레그램은 봇이 쓰는 저널을 읽기만 하는 별도 프로세스다 — 캐시가 파일 상태
    (mtime/크기)로 무효화되지 않으면 화면이 낡은 저널을 계속 보여주게 된다."""
    path = tmp_path / "trades.jsonl"
    append_entry({"event": "entered", "symbol": "BTC/USDT:USDT"}, path=str(path))
    assert len(read_entries(path=str(path))) == 1

    # 다른 프로세스가 쓴 것처럼 파일에 직접 덧붙인다(append_entry의 캐시 무효화를 거치지 않음).
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": "2026-09-12T00:00:00+00:00", "event": "closed"}) + "\n")

    entries = read_entries(path=str(path))
    assert len(entries) == 2
    assert entries[-1]["event"] == "closed"


def test_read_entries_drops_the_cache_when_the_file_disappears(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_entry({"event": "entered"}, path=str(path))
    assert len(read_entries(path=str(path))) == 1

    path.unlink()
    assert read_entries(path=str(path)) == []
