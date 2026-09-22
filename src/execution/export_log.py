"""저널(JSON Lines)을 분석 도구가 바로 읽는 표 형태로 내보낸다.

저널은 append 전용 이벤트 스트림이라 "이 거래가 어떤 신호에서 나와서 어떻게 끝났는지"가 여러
줄에 흩어져 있다(`entered`가 신호·ATR비율·수량을, `closed`가 청산가·R을 들고 있고, 그때 어떤
설정이었는지는 그 앞의 `config_changed`에 있다). 엑셀/판다스로 뭔가 세어보려면 매번 이 결합을
손으로 다시 해야 하고, 그 과정에서 R을 믿을 수 있는지 판정하는 규칙(부호 모순 시 폐기)을
빼먹으면 화면의 숫자와 어긋난다.

그래서 결합은 **실거래·대시보드가 쓰는 `performance.resolve_closed_trades`를 그대로 불러서**
한다 — 여기 조건을 따로 적으면 내보낸 CSV와 대시보드가 서로 다른 말을 하게 된다.

내보내는 것(env별):
- `trades_{env}.csv`  — 청산된 거래 한 건이 한 줄(진입 맥락 결합, 보유시간·R신뢰여부 포함)
- `events_{env}.csv`  — 저널 전체 이벤트를 납작하게(차단/거부/서킷브레이커까지 세는 용도)
- `configs_{env}.csv` — 설정 경계(이 줄 이후 거래는 이 설정으로 나온 것)
- `raw/{원본파일명}`   — 원본 JSONL 그대로 복사(손실 없는 백업이자 재생성 원본)
"""

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.execution import journal
from src.execution.performance import _net_r, _price_r, resolve_closed_trades

DEFAULT_OUT_DIR = "exports"

# env -> 저널 경로 / 필터 카운터 경로. futures_rule_bot의 상수를 그대로 쓰지 않는 이유는 이
# 모듈이 봇 모듈(거래소 클라이언트까지 끌고 온다)에 의존할 필요가 없기 때문 — 내보내기는
# 파일만 읽으므로 거래소 연결 없이 돌아야 한다.
JOURNALS = {
    "demo": "journal/futures_rule_trades.jsonl",
    "live": "journal/futures_rule_trades.live.jsonl",
}
FILTER_STATS = {
    "demo": "state/futures_rule_filter_stats.json",
    "live": "state/futures_rule_filter_stats.live.json",
}

TRADE_COLUMNS = [
    "env", "symbol", "side", "reason",
    "entry_timestamp", "exit_timestamp", "holding_minutes",
    "entry_price", "exit_price", "stop_loss_price", "take_profit_price",
    "realized_pnl", "net_realized_pnl", "realized_r", "net_realized_r", "r_reliable",
    "total_fee", "entry_fee", "exit_fee", "fee_r", "fee_estimated", "actual_entry_price",
    "entry_slippage_r", "max_favorable_r", "max_adverse_r",
    "signal", "signal_bar_timestamp", "atr_to_stop_ratio",
    "quantity", "notional", "stop_loss_pct", "config_timestamp",
]

EVENT_COLUMNS = [
    "env", "timestamp", "event", "symbol", "reason",
    "signal", "entered", "has_position",
    "entry_price", "exit_price", "stop_loss_price", "take_profit_price",
    "realized_pnl", "realized_r", "total_fee", "fee_r", "net_realized_r",
    "atr_to_stop_ratio", "signal_bar_timestamp",
]


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (ValueError, TypeError):
        return None
    return round(delta.total_seconds() / 60.0, 2)


def _round(value, digits: int = 8):
    """부동소수 잡음(1.8666722999153968)을 표에서 읽을 수 있는 자리수로 줄인다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return round(value, digits)


def _config_timeline(entries: list[dict]) -> list[dict]:
    """config_changed 기록을 시간순으로. 거래마다 "그때 어떤 설정이었나"를 붙이는 데 쓴다."""
    return [e for e in entries if e.get("event") == "config_changed" and e.get("timestamp")]


def _config_at(timeline: list[dict], when: str | None) -> str | None:
    """when 시점에 유효했던 설정 경계의 타임스탬프. 그 앞에 아무 기록도 없으면 None —
    저널이 config_changed를 남기기 시작한 2026-09-12 이전 거래가 전부 여기 해당하고, 그 빈 칸이
    곧 "이 거래는 어떤 규칙으로 나왔는지 저널만으로는 모른다"는 뜻이다."""
    if not when:
        return None
    active = None
    for record in timeline:
        if record["timestamp"] <= when:
            active = record["timestamp"]
        else:
            break
    return active


def build_trade_rows(entries: list[dict], env: str) -> list[dict]:
    """청산 거래 한 건 = 한 줄. 진입 기록에만 있는 맥락(신호/ATR비율/수량)을 같이 싣는다.

    `resolve_closed_trades`가 이미 방향·손절가·R을 복원해 주므로 여기서는 그 결과에 진입 쪽
    맥락과 파생값(보유시간·명목가·손절폭%)만 덧붙인다. 그 함수가 R을 버린 거래는 `realized_r`가
    비고 `r_reliable`이 no가 된다 — 왜 비었는지가 표에서 드러나야 걸러낼 수 있다."""
    resolved = resolve_closed_trades(entries)

    # 심볼별 마지막 진입 기록을 시간순으로 따라간다(resolve_closed_trades와 같은 방식이라
    # 결과 리스트와 순서가 정확히 일치한다 — 그래서 인덱스로 짝지을 수 있다).
    last_entry: dict[str, dict] = {}
    context: list[dict] = []
    for entry in entries:
        event, symbol = entry.get("event"), entry.get("symbol")
        if event == "entered" and symbol:
            last_entry[symbol] = entry
        elif event == "closed" and entry.get("reason") != "manual":
            context.append(last_entry.get(symbol, {}))

    timeline = _config_timeline(entries)
    rows = []
    for i, trade in enumerate(resolved):
        source = context[i] if i < len(context) else {}
        execution = source.get("execution") or {}
        quantity = execution.get("quantity")
        entry_price = trade.get("entry_price")
        stop_loss_price = trade.get("stop_loss_price")

        notional = None
        if isinstance(quantity, (int, float)) and isinstance(entry_price, (int, float)):
            notional = quantity * entry_price
        stop_loss_pct = None
        if (isinstance(entry_price, (int, float)) and entry_price
                and isinstance(stop_loss_price, (int, float))):
            stop_loss_pct = abs(entry_price - stop_loss_price) / entry_price

        # R이 폐기된 거래인지를 표에 남긴다 — 빈 칸만 보면 "옛 기록이라 원래 없는 것"과
        # "계산은 되지만 실현손익과 부호가 모순돼서 버린 것"이 구분되지 않는다.
        if trade.get("realized_r") is not None:
            r_reliable = "yes"
        elif _price_r(trade.get("exit_price"), entry_price,
                      stop_loss_price, trade.get("side")) is not None:
            r_reliable = "no"
        else:
            r_reliable = "unknown"

        # 순R과 수수료 — 2026-09-22 이후 청산은 실측을 들고 있고, 그 이전은 설정 수수료율로
        # 추정한다. 어느 쪽인지 `fee_estimated`로 밝혀서 표에서 섞이지 않게 한다.
        net_r, fee_estimated = _net_r(trade)

        # 실제 진입 체결가 - 신호 봉 종가 = 진입 슬리피지. R로 재면 사이징과 무관하다.
        # **이게 저널의 realized_r에 빠져 있는 또 하나의 비용**이다(R은 신호 봉 종가를 진입가로
        # 쓴다) — 값이 있는 거래만 채워지고, 옛 기록은 빈다.
        actual_entry = trade.get("actual_entry_price")
        slippage_r = None
        if (isinstance(actual_entry, (int, float)) and isinstance(entry_price, (int, float))
                and stop_loss_price is not None):
            risk = abs(float(entry_price) - float(stop_loss_price))
            if risk > 0:
                drift = (actual_entry - entry_price) / risk
                slippage_r = drift if trade.get("side") == "long" else -drift

        entry_timestamp = trade.get("entry_timestamp") or source.get("timestamp")
        rows.append({
            "env": env,
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "reason": trade.get("reason"),
            "entry_timestamp": entry_timestamp,
            "exit_timestamp": trade.get("timestamp"),
            "holding_minutes": _minutes_between(entry_timestamp, trade.get("timestamp")),
            "entry_price": _round(entry_price),
            "exit_price": _round(trade.get("exit_price")),
            "stop_loss_price": _round(stop_loss_price),
            "take_profit_price": _round(trade.get("take_profit_price")),
            "realized_pnl": _round(trade.get("realized_pnl"), 4),
            "net_realized_pnl": _round(trade.get("net_realized_pnl"), 4),
            "realized_r": _round(trade.get("realized_r"), 4),
            "net_realized_r": _round(net_r, 4),
            "r_reliable": r_reliable,
            "total_fee": _round(trade.get("total_fee"), 6),
            "entry_fee": _round(trade.get("entry_fee"), 6),
            "exit_fee": _round(trade.get("exit_fee"), 6),
            "fee_r": _round(trade.get("fee_r"), 4),
            "fee_estimated": "yes" if (net_r is not None and fee_estimated) else "no",
            "actual_entry_price": _round(actual_entry),
            "entry_slippage_r": _round(slippage_r, 4),
            "max_favorable_r": _round(trade.get("max_favorable_r"), 4),
            "max_adverse_r": _round(trade.get("max_adverse_r"), 4),
            "signal": source.get("signal"),
            "signal_bar_timestamp": source.get("signal_bar_timestamp"),
            "atr_to_stop_ratio": _round(source.get("atr_to_stop_ratio"), 4),
            "quantity": _round(quantity),
            "notional": _round(notional, 4),
            "stop_loss_pct": _round(stop_loss_pct, 6),
            "config_timestamp": _config_at(timeline, trade.get("timestamp")),
        })
    return rows


def build_event_rows(entries: list[dict], env: str) -> list[dict]:
    """저널 전체를 납작한 표로. 거래가 안 된 이벤트(거래소 거부/서킷브레이커/무보호)도 세야
    하므로 `closed`만 보는 trades와 따로 낸다. `execution`처럼 중첩된 값은 원본 JSONL 쪽에
    그대로 있으니 여기서는 스칼라 칼럼만 싣는다."""
    rows = []
    for entry in entries:
        row = {"env": env}
        for column in EVENT_COLUMNS[1:]:
            row[column] = _round(entry.get(column))
        rows.append(row)
    return rows


def build_config_rows(entries: list[dict], env: str) -> list[dict]:
    """설정 경계 한 줄 = 그 시점 이후 거래가 어떤 규칙으로 나왔는지. 전략 파라미터는 앞으로
    늘어나므로 칼럼을 고정하지 않고 저널에서 발견된 키를 그대로 칼럼으로 쓴다
    (`current_strategy_config()`에 새 파라미터를 넣으면 여기도 자동으로 따라온다)."""
    rows = []
    for entry in _config_timeline(entries):
        config = entry.get("config") or {}
        row = {"env": env, "timestamp": entry["timestamp"],
               "first_record": bool(entry.get("first_record")),
               "changed_keys": ",".join(sorted((entry.get("changes") or {}).keys()))}
        for key, value in config.items():
            row[key] = ",".join(map(str, value)) if isinstance(value, list) else value
        rows.append(row)
    return rows


def write_csv(rows: list[dict], path: str | Path, columns: list[str] | None = None) -> Path:
    """행이 없어도 헤더만 있는 파일을 남긴다 — 파일이 아예 없으면 "내보내기가 실패한 것"과
    "그 env에 아직 거래가 없는 것"이 구분되지 않는다. Excel이 한글을 깨지 않고 열도록
    UTF-8 BOM으로 쓴다."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(dict.fromkeys(key for row in rows for key in row))
    with file_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return file_path


def _copy_into(source_path: str | Path, raw_dir: Path) -> str | None:
    source = Path(source_path)
    if not source.name or not source.exists():
        return None
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / source.name
    shutil.copy2(source, target)
    return str(target)


def export_env(env: str, out_dir: str | Path = DEFAULT_OUT_DIR,
               journal_path: str | None = None) -> dict:
    """한 계좌(env)의 저널을 CSV 3종 + 원본 복사로 내보내고 요약을 돌려준다."""
    journal_path = journal_path or JOURNALS[env]
    out = Path(out_dir)
    entries = journal.read_entries(journal_path)

    trades = build_trade_rows(entries, env)
    files = {
        "trades": str(write_csv(trades, out / f"trades_{env}.csv", TRADE_COLUMNS)),
        "events": str(write_csv(build_event_rows(entries, env),
                                out / f"events_{env}.csv", EVENT_COLUMNS)),
        "configs": str(write_csv(build_config_rows(entries, env), out / f"configs_{env}.csv")),
    }
    for key, path in (("raw", journal_path), ("filter_stats", FILTER_STATS.get(env, ""))):
        copied = _copy_into(path, out / "raw")
        if copied:
            files[key] = copied

    with_r = [t for t in trades if t.get("realized_r") is not None]
    with_net = [t for t in trades if t.get("net_realized_r") is not None]
    return {
        "env": env,
        "journal": journal_path,
        "events": len(entries),
        "trades": len(trades),
        "trades_with_r": len(with_r),
        "total_r": round(sum(t["realized_r"] for t in with_r), 4) if with_r else None,
        # 수수료 차감 후 — 비교에 써야 하는 쪽. total_r은 수수료 이전 값이다.
        "total_net_r": round(sum(t["net_realized_r"] for t in with_net), 4) if with_net else None,
        "realized_pnl": round(sum(t.get("realized_pnl") or 0.0 for t in trades), 4),
        "first_trade": trades[0]["exit_timestamp"] if trades else None,
        "last_trade": trades[-1]["exit_timestamp"] if trades else None,
        "files": files,
    }


def export_all(out_dir: str | Path = DEFAULT_OUT_DIR, envs=("demo", "live")) -> dict:
    """모든 env를 내보내고, 두 계좌를 합친 `trades_all.csv`와 `manifest.json`까지 남긴다.

    합친 파일을 따로 두는 이유: 분석 질문 대부분이 "심볼별/시간대별/필터별"이라 env로 갈라져
    있으면 매번 두 파일을 붙여야 한다. env 칼럼이 있으니 합쳐도 계좌 구분은 유지된다."""
    out = Path(out_dir)
    summaries = [export_env(env, out) for env in envs]

    combined = []
    for env in envs:
        combined += build_trade_rows(journal.read_entries(JOURNALS[env]), env)
    combined.sort(key=lambda row: row.get("exit_timestamp") or "")
    combined_path = write_csv(combined, out / "trades_all.csv", TRADE_COLUMNS)

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(out),
        "combined": str(combined_path),
        "envs": summaries,
        "trade_columns": TRADE_COLUMNS,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
