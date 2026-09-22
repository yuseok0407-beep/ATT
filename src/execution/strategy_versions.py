"""전략 버전 — "이 거래는 어떤 규칙으로 나왔나"에 번호를 붙인다(2026-09-22).

규칙이 바뀐 전후의 거래가 한 숫자로 섞이면 지금 규칙이 통하는지 판단할 수 없다. 저널의
`config_changed`가 그 경계를 이미 남기고 있지만(2026-09-12~) 번호가 없어서 "몇 번 바뀌었나",
"이 버전으로 몇 건/몇 R인가"를 사람이 셀 수 없었다. 여기서 두 출처를 한 타임라인으로 합친다:

1. **복원한 과거 버전**(`HISTORICAL_VERSIONS`) — `config_changed` 기록이 생기기 전의 규칙 변경.
   저널에 경계가 없으므로 UPDATE_LOG/git 이력과 저널 흔적(새 필드가 처음 나타난 시각)으로
   복원했다. 시각은 추정이라 화면에 그렇게 표시한다.
2. **저널의 `config_changed`** — 값이 실제로 바뀐 기록마다 새 버전 하나. 단, 다음 둘은 버전을
   올리지 않는다:
   - `first_record`(추적 시작 기록) — 규칙이 바뀐 게 아니라 그때부터 적기 시작한 것뿐이다.
   - 바뀐 항목이 전부 `from=None`인 기록 — `current_strategy_config()`에 **추적 항목을 새로
     추가**했을 때 생기는 기록이라 규칙 변경이 아니다.

거래는 **진입 시각**으로 버전에 배정한다 — 청산은 재시작 뒤에 일어날 수 있지만 그 거래를 만든
판단은 진입 때의 규칙이 내렸다.

데모와 실계좌는 저널이 따로라 버전도 저널별로 센다. 두 봇을 같은 설정으로 같이 재시작하는 한
번호가 같게 나오고, 어긋나면 그 자체가 "두 계좌가 다른 규칙으로 돌았다"는 신호다.
"""

from src.execution.performance import _net_r, resolve_closed_trades

# config_changed가 없던 시절의 규칙 변경. 시각은 UTC. 새 변경은 여기 적지 말 것 — 봇이 시작할 때
# 저널에 config_changed를 남기므로 자동으로 버전이 된다(코드 로직 변경은
# futures_strategy.STRATEGY_LOGIC_REVISION을 올리면 같은 경로를 탄다).
HISTORICAL_VERSIONS = [
    {"start": "2026-08-08T00:00:00+00:00", "title": "규칙 봇 개시",
     "detail": "ADX30·SMA10·손절 1.25%·손익비 2 — 신호를 아직 마감 안 된 진행 중인 봉으로 계산"},
    {"start": "2026-08-25T00:00:00+00:00", "title": "마감봉으로만 신호 계산",
     "detail": "진행 중인 봉을 버리고 마감된 봉으로만 신호/진입가 계산(커밋 aa099f4, 실거래 반영 08-25)"},
    {"start": "2026-09-08T18:00:00+00:00", "title": "재진입 잠금·괴리 검사·레짐 숏차단",
     "detail": "같은 신호봉 재진입 금지, 진입괴리 0.5R, SMA400 위 숏 차단, 거래당 리스크 2%→0.75%"
               " (첫 signal_bar_timestamp 기록 시각)"},
    {"start": "2026-09-10T07:00:00+00:00", "title": "저변동 필터·동시보유 8",
     "detail": "ATR/손절폭 0.64 미만 진입 차단, 동시보유 4→8 (첫 atr_to_stop_ratio 기록 시각)"},
]


def _is_rule_change(record: dict) -> bool:
    if record.get("first_record"):
        return False
    changes = record.get("changes") or {}
    if not isinstance(changes, dict):
        return bool(changes)   # 형식을 모르는 기록은 변경으로 본다(개정을 놓치는 쪽보다 낫다)
    return any(not isinstance(change, dict) or change.get("from") is not None
               for change in changes.values())


def _describe(changes: dict) -> str:
    if not isinstance(changes, dict):
        return ", ".join(map(str, changes))
    parts = []
    for key, change in changes.items():
        if not isinstance(change, dict):
            parts.append(f"{key} 변경")
            continue
        before, after = change.get("from"), change.get("to")
        if before is None:
            continue
        if isinstance(before, list) or isinstance(after, list):
            parts.append(f"{key} 변경")
        else:
            parts.append(f"{key} {before}→{after}")
    return ", ".join(parts)


def version_timeline(entries: list[dict]) -> list[dict]:
    """버전 목록을 시간순으로. 각 항목: version(1부터), label("v3"), start, title, detail,
    reconstructed(과거 복원 여부), changes, config(알면)."""
    timeline = [
        {**v, "version": i + 1, "label": f"v{i + 1}", "reconstructed": True, "changes": {}, "config": None}
        for i, v in enumerate(HISTORICAL_VERSIONS)
    ]
    for record in entries:
        if record.get("event") != "config_changed" or not record.get("timestamp"):
            continue
        if not _is_rule_change(record):
            # 규칙은 그대로다 — 직전 버전에 설정 스냅샷만 붙인다(복원 버전은 이걸로 설정값을 알게 된다).
            if timeline:
                timeline[-1]["config"] = record.get("config")
            continue
        n = len(timeline) + 1
        changes = record.get("changes") or {}
        changes = changes if isinstance(changes, dict) else {}
        timeline.append({
            "version": n, "label": f"v{n}", "start": record["timestamp"],
            "title": "설정 변경", "detail": _describe(changes), "reconstructed": False,
            "changes": changes, "config": record.get("config"),
        })
    return timeline


def version_at(timeline: list[dict], when: str | None) -> dict | None:
    """when(UTC ISO 타임스탬프) 시점에 유효했던 버전. 첫 버전보다 이르면 None."""
    if not when:
        return None
    active = None
    for version in timeline:
        if version["start"] <= when:
            active = version
        else:
            break
    return active


def _entry_time(trade: dict) -> str | None:
    return trade.get("entry_timestamp") or trade.get("timestamp")


def summarize_by_version(entries: list[dict]) -> dict:
    """버전별 성과. 거래는 진입 시각으로 배정하고, R은 수수료 차감 후(순R)를 앞에 둔다."""
    timeline = version_timeline(entries)
    buckets = {v["version"]: {"trades": 0, "trades_with_r": 0, "wins": 0, "total_r": 0.0,
                              "total_net_r": 0.0, "realized_pnl": 0.0} for v in timeline}
    unassigned = 0
    for trade in resolve_closed_trades(entries):
        version = version_at(timeline, _entry_time(trade))
        if version is None:
            unassigned += 1
            continue
        b = buckets[version["version"]]
        b["trades"] += 1
        b["realized_pnl"] += trade.get("realized_pnl") or 0.0
        if trade.get("realized_r") is None:
            continue
        b["trades_with_r"] += 1
        b["total_r"] += trade["realized_r"]
        b["wins"] += 1 if trade["realized_r"] > 0 else 0
        net, _ = _net_r(trade)
        if net is not None:
            b["total_net_r"] += net

    versions = []
    for v in timeline:
        b = buckets[v["version"]]
        n = b["trades_with_r"]
        versions.append({
            **{k: v[k] for k in ("version", "label", "start", "title", "detail", "reconstructed", "changes")},
            **b,
            "win_rate": b["wins"] / n if n else None,
            "avg_net_r": b["total_net_r"] / n if n else None,
        })
    current = timeline[-1] if timeline else None
    return {
        "current": current["label"] if current else None,
        "revisions": max(0, len(timeline) - 1),   # 첫 버전 이후 몇 번 바뀌었나
        "versions": versions,
        "unassigned_trades": unassigned,
    }
