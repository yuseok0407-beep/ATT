import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "journal/trades.jsonl"

# 파일경로 -> ((mtime_ns, size), 파싱된 기록 리스트).
#
# 저널은 한 사이클 안에서 같은 파일이 10~20번 다시 읽힌다(심볼마다 마지막 진입 기록/마지막
# 이벤트를 찾고, 연속손실을 세고, 청산을 감지한다 — 전부 "전체를 훑어 뒤에서부터 찾는" 형태라
# 매번 파일 전체를 다시 파싱했다). 2026-09-12 측정으로 데모 저널 415KB(382건)가 1회 약 5~8ms라
# 사이클당 60~100ms이고, 저널은 계속 자라기만 하므로 이 비용도 계속 는다.
#
# 읽기 쪽 한 곳에서만 막는다 — 호출자마다 캐시를 들고 다니게 고치면(사이클 캐시를 인자로
# 넘기는 식) 봇/대시보드/텔레그램 세 프로세스의 모든 경로를 다 고쳐야 하고, 어느 하나가 낡은
# 리스트를 들고 있으면 그게 곧 오래된 저널을 보고 판단하는 버그가 된다.
#
# 무효화는 파일의 (mtime_ns, size)로 한다 — 저널은 append 전용이라 새 기록이 생기면 크기가
# 반드시 커지고, 다른 프로세스(봇)가 쓴 것도 이 방식이면 그대로 감지된다(프로세스 간 공유
# 캐시가 아니라 각자 파일 상태를 보는 것이라 대시보드도 항상 최신을 본다).
#
# 돌려주는 리스트는 캐시와 같은 객체다 — 호출자는 읽기만 할 것(현재 모든 호출자가 그렇다.
# 값을 바꿔야 하면 performance.resolve_closed_trades처럼 dict(entry)로 복사해서 쓴다).
_cache: dict[str, tuple[tuple[int, int], list]] = {}


def append_entry(entry: dict, path: str = DEFAULT_PATH) -> None:
    """매매 사이클 하나(시장상황·판단근거·주문결과)를 JSON Lines로 기록한다."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # 방금 쓴 내용은 크기가 커져서 어차피 캐시가 무효화되지만, 같은 사이클 안에서 기록 직후
    # 바로 다시 읽는 경로가 있으므로(예: 진입 기록 후 같은 심볼 재평가) 명시적으로 버린다.
    _cache.pop(str(file_path), None)


def read_entries(path: str = DEFAULT_PATH) -> list:
    """저널 전체를 리스트로 읽는다. 파일이 바뀌지 않았으면 직전에 파싱한 결과를 그대로 준다."""
    file_path = Path(path)
    key = str(file_path)
    try:
        stat = file_path.stat()
    except (FileNotFoundError, NotADirectoryError):
        _cache.pop(key, None)
        return []

    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    with file_path.open("r", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    _cache[key] = (stamp, entries)
    return entries
