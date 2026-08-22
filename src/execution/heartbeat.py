import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "state/futures_rule_heartbeat.json"
LIVE_DEFAULT_PATH = "state/futures_rule_heartbeat.live.json"  # 실계좌 봇 전용(2026-08-22)


def write_heartbeat(cycle_count: int, open_positions: int, margin_equity: float, path: str = DEFAULT_PATH) -> None:
    """감시 루프가 사이클을 돌 때마다(매 POLL_INTERVAL_SECONDS) 호출 — 로그 파일과 별개로,
    대시보드가 "봇이 실제로 살아서 사이클을 돌고 있는지"를 파일 하나만 읽어서 바로 확인할 수 있게
    한다. 로그의 하트비트(10분 주기)보다 훨씬 촘촘해서, 봇이 멈춘 지 몇십 초 만에도 감지 가능."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle_count": cycle_count, "open_positions": open_positions, "margin_equity": margin_equity,
    }
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(record), encoding="utf-8")


def read_heartbeat(path: str = DEFAULT_PATH) -> dict | None:
    """마지막으로 기록된 하트비트에 age_seconds(지금으로부터 몇 초 전인지)를 더해서 돌려준다.
    파일이 없으면(봇을 한 번도 안 돌렸으면) None."""
    file_path = Path(path)
    if not file_path.exists():
        return None

    record = json.loads(file_path.read_text(encoding="utf-8"))
    last_ts = datetime.fromisoformat(record["timestamp"])
    age_seconds = (datetime.now(timezone.utc) - last_ts).total_seconds()
    return {**record, "age_seconds": age_seconds}
