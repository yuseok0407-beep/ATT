import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "journal/trades.jsonl"


def append_entry(entry: dict, path: str = DEFAULT_PATH) -> None:
    """매매 사이클 하나(시장상황·판단근거·주문결과)를 JSON Lines로 기록한다."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_entries(path: str = DEFAULT_PATH) -> list:
    file_path = Path(path)
    if not file_path.exists():
        return []
    with file_path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
