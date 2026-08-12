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
