import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from src.core.config import (
    BREAKER_HALT_ALERT_HOURS,
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    FUTURES_SYMBOLS,
    HEARTBEAT_STALE_SECONDS,
    TELEGRAM_DAILY_SUMMARY_HOUR,
    TELEGRAM_POLL_INTERVAL_SECONDS,
)
from src.core.signal_status import collect_conditions
from src.data.futures_exchange import (
    LiveKeysNotConfiguredError,
    get_futures_balance,
    get_futures_client,
    get_position,
)
from src.data.public_ip import get_public_ip
from src.execution import bot_process, excursion, filter_stats, strategy_versions
from src.execution.futures_orders import get_bracket_prices
from src.execution.heartbeat import DEFAULT_PATH as HEARTBEAT_DEMO_PATH
from src.execution.heartbeat import LIVE_DEFAULT_PATH as HEARTBEAT_LIVE_PATH
from src.execution.heartbeat import read_heartbeat
from src.execution.journal import read_entries
from src.execution import equity_log
from src.execution.performance import summarize_day
from src.execution.telegram_client import get_updates, send_message
from src.futures_rule_bot import (
    EXCURSION_PATH,
    FILTER_STATS_PATH,
    JOURNAL_PATH,
    LIVE_EXCURSION_PATH,
    LIVE_FILTER_STATS_PATH,
    LIVE_JOURNAL_PATH,
    reset_consecutive_losses,
)

logger = logging.getLogger(__name__)

STATE_PATH = "state/telegram_bot_state.json"

# 진입/청산/서킷브레이커만 폰으로 쏜다 — no_signal/holding_position/skipped_max_positions는 애초에
# 저널에 안 남고(futures_rule_bot._SILENT_EVENTS), rejected_exchange_error 등은 저널엔 남지만
# 매 사이클 반복될 수 있어 알림으로는 노이즈라 제외한다.
# unprotected_position/position_protected는 매 사이클 반복되지 않고(futures_rule_bot이 상태
# 전이에서만 남긴다) 손절 없는 레버리지 포지션은 즉시 알아야 하는 사고라 알림 대상이다.
_NOTIFY_EVENTS = ("entered", "closed", "circuit_breaker_blocked",
                  "unprotected_position", "position_protected", "config_changed")

_REASON_LABELS = {
    "stop_loss": "손절", "take_profit": "익절", "manual": "수동청산",
    "breakeven_stop": "손익분기청산", "unknown": "기타",
}
_SIGNAL_LABELS = {"LONG": "롱", "SHORT": "숏"}
# 설정 변경 알림에서 쓰는 이름 — 대시보드 index.html의 CONFIG_LABELS와 같은 항목을 한글로.
_CONFIG_LABELS = {
    "timeframe": "타임프레임", "adx_threshold": "ADX", "sma_period": "SMA기간",
    "regime_sma_period": "레짐SMA", "direction_filter": "방향확인",
    "min_atr_to_stop_ratio": "최소변동성",
    "max_entry_price_drift_r": "진입괴리한도", "stop_loss_pct": "손절폭", "take_profit_rr": "손익비",
    "leverage": "레버리지", "risk_per_trade": "거래당리스크",
    "max_concurrent_positions": "동시보유상한", "max_daily_loss_pct": "일일손실한도",
    "max_consecutive_losses": "연속손실한도", "consecutive_loss_cooldown_hours": "연속손실쿨다운",
    "symbols": "감시종목", "logic_revision": "규칙코드개정",
}

# 여기 목록과 _COMMANDS는 항상 같아야 한다 — 안 그러면 동작하는데 아무도 모르는 명령이 생긴다
# (실제로 /conditions가 그랬다). tests/test_telegram_bot.py가 둘이 어긋나면 실패시킨다.
_HELP_TEXT = (
    "사용 가능한 명령:\n"
    "/status — 데모/실계좌 상태 조회 (포지션·손절·익절·보유 중 최고점)\n"
    "/conditions — 종목별 진입 조건 근접도\n"
    "/summary — 어제 하루 성과 요약 (매일 자동으로도 발송)\n"
    "/start_demo, /stop_demo — 데모 봇 시작/중지\n"
    "/start_live, /stop_live — 실계좌 봇 시작/중지 (실제 자금에 영향)\n"
    "/reset_streak_demo, /reset_streak_live — 연속손실 카운트를 0으로 리셋\n"
    "/help — 이 메시지"
)

_COMMANDS = {
    "/start_demo": ("demo", "start"),
    "/stop_demo": ("demo", "stop"),
    "/start_live": ("live", "start"),
    "/stop_live": ("live", "stop"),
    "/reset_streak_demo": ("demo", "reset_streak"),
    "/reset_streak_live": ("live", "reset_streak"),
    "/status": ("", "status"),
    "/summary": ("", "summary"),
    "/conditions": ("", "conditions"),
    "/help": ("", "help"),
}


def _journal_path(env: str) -> str:
    return LIVE_JOURNAL_PATH if env == "live" else JOURNAL_PATH


def _filter_stats_path(env: str) -> str:
    return LIVE_FILTER_STATS_PATH if env == "live" else FILTER_STATS_PATH


def _excursion_path(env: str) -> str:
    return LIVE_EXCURSION_PATH if env == "live" else EXCURSION_PATH


def _equity_log_path(env: str) -> str:
    return equity_log.LIVE_DEFAULT_PATH if env == "live" else equity_log.DEFAULT_PATH


def _heartbeat_path(env: str) -> str:
    return HEARTBEAT_LIVE_PATH if env == "live" else HEARTBEAT_DEMO_PATH


def load_state(path: str = STATE_PATH) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def save_state(state: dict, path: str = STATE_PATH) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(state), encoding="utf-8")


def _fmt_price(value) -> str:
    """가격대가 종목마다 크게 다르다(BTC 수만 달러 vs XRP 1달러 미만) — 소수점 자리를 고정하면
    저가 종목은 손절/익절가가 진입가와 구분 안 될 정도로 뭉개진다. 대시보드 index.html의 fmtPrice
    와 동일한 자릿수 기준을 텍스트 메시지에도 그대로 적용."""
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    abs_value = abs(value)
    if abs_value >= 1000:
        digits = 1
    elif abs_value >= 100:
        digits = 2
    elif abs_value >= 1:
        digits = 4
    elif abs_value >= 0.01:
        digits = 6
    else:
        digits = 8
    return f"{value:,.{digits}f}"


def _fmt_config_change(change: dict) -> str:
    """설정 하나의 변경을 "이전→이후"로. 심볼 목록처럼 리스트인 값은 그대로 찍으면 12종목이
    한 줄을 다 먹으므로 추가/제거만 적는다."""
    before, after = change.get("from"), change.get("to")
    if isinstance(before, list) or isinstance(after, list):
        before_set, after_set = set(before or []), set(after or [])
        added = sorted(x.split("/")[0] for x in after_set - before_set)
        removed = sorted(x.split("/")[0] for x in before_set - after_set)
        parts = []
        if added:
            parts.append("추가 " + ",".join(added))
        if removed:
            parts.append("제외 " + ",".join(removed))
        return " / ".join(parts) or "변경"
    return f"{before}→{after}"


def _format_entry(env: str, entry: dict) -> str | None:
    label = "LIVE" if env == "live" else "DEMO"
    symbol = entry.get("symbol")
    short_symbol = symbol.split("/")[0] if symbol else "?"
    event = entry.get("event")

    if event == "entered":
        side = _SIGNAL_LABELS.get(entry.get("signal"), entry.get("signal") or "")
        return (f"🟢 [{label}] {short_symbol} {side} 진입 @ {entry.get('entry_price')}"
                f" (손절 {entry.get('stop_loss_price')} · 익절 {entry.get('take_profit_price')})")

    if event == "closed":
        reason = _REASON_LABELS.get(entry.get("reason"), entry.get("reason") or "기타")
        pnl = entry.get("realized_pnl")
        pnl_str = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else str(pnl)
        emoji = "🔴" if isinstance(pnl, (int, float)) and pnl < 0 else "🟢"
        return f"{emoji} [{label}] {short_symbol} {reason} 청산 @ {entry.get('exit_price')} · 실현손익 {pnl_str} USDT"

    if event == "circuit_breaker_blocked":
        return f"⛔ [{label}] 서킷브레이커 발동 — {entry.get('reason')}"

    if event == "unprotected_position":
        return (f"🚨 [{label}] {short_symbol} 손절 주문이 없습니다 — 레버리지 포지션이 무방비 상태입니다."
                f" 대시보드에서 확인하거나 즉시 청산하세요.")

    if event == "position_protected":
        return (f"🛡 [{label}] {short_symbol} 손절 주문이 복구됐습니다"
                f" (손절 {_fmt_price(entry.get('stop_loss_price'))}).")

    if event == "config_changed":
        # 설정을 바꾸고 봇을 다시 띄웠을 때 "실제로 그 값으로 떴는지"를 폰에서 바로 확인할 수
        # 있게 한다 — .env를 고쳤는데 재시작을 안 했거나 엉뚱한 env를 재시작한 경우가 이 한 통으로
        # 드러난다(값이 그대로면 봇이 아예 이 기록을 안 남기므로 재시작만으로는 알림이 안 온다).
        changes = entry.get("changes") or {}
        if entry.get("first_record"):
            return f"⚙️ [{label}] 봇이 현재 설정을 저널에 기록했습니다 (설정 변경 추적 시작)."
        summary = ", ".join(f"{_CONFIG_LABELS.get(k, k)} {_fmt_config_change(v)}"
                            for k, v in list(changes.items())[:6])
        more = f" 외 {len(changes) - 6}개" if len(changes) > 6 else ""
        if not any((v or {}).get("from") is not None for v in changes.values()):
            # 추적 항목을 새로 추가한 기록 — 규칙이 바뀐 게 아니라 전략 버전도 안 올라간다.
            return f"⚙️ [{label}] 설정 기록 항목이 추가됐습니다(규칙 변경 아님) — {summary}{more}"
        version = strategy_versions.version_at(
            strategy_versions.version_timeline(read_entries(path=_journal_path(env))), entry.get("timestamp"))
        tag = f" → 전략 {version['label']}" if version else ""
        return f"⚙️ [{label}] 설정이 바뀐 채로 봇이 시작됐습니다{tag} — {summary}{more}"

    return None


def check_new_journal_entries(env: str, state: dict) -> list[str]:
    """저널에 새로 쌓인 항목 중 알림 가치가 있는 것만(entered/closed/circuit_breaker_blocked)
    메시지로 포맷해 돌려준다. 최초 호출(state에 이 env의 기록이 없음)은 과거 이력을 한꺼번에
    쏘지 않도록 지금 시점을 워터마크로만 기록하고 빈 리스트를 돌려준다."""
    entries = read_entries(path=_journal_path(env))
    env_state = state.setdefault(env, {})
    last_ts = env_state.get("last_entry_ts")

    if last_ts is None:
        if entries:
            env_state["last_entry_ts"] = entries[-1].get("timestamp")
        return []

    new_entries = [e for e in entries if e.get("timestamp") and e["timestamp"] > last_ts]
    if not new_entries:
        return []
    env_state["last_entry_ts"] = max(e["timestamp"] for e in new_entries)

    messages = []
    for entry in new_entries:
        if entry.get("event") not in _NOTIFY_EVENTS:
            continue
        message = _format_entry(env, entry)
        if message:
            messages.append(message)
    return messages


def check_bot_status_change(env: str, state: dict) -> str | None:
    """봇 프로세스가 켜짐<->꺼짐으로 전이할 때만 알린다(매번 알리면 스팸). 최초 호출은 현재 상태만
    기록."""
    label = "LIVE" if env == "live" else "DEMO"
    running = bot_process.get_status(env)["running"]
    env_state = state.setdefault(env, {})
    was_running = env_state.get("was_running")
    env_state["was_running"] = running

    if was_running is None or running == was_running:
        return None
    return f"✅ [{label}] 감시 봇이 시작됐습니다." if running else f"⚠️ [{label}] 감시 봇이 꺼졌습니다."


def check_heartbeat_stall(env: str, state: dict) -> str | None:
    """봇 프로세스는 살아있는데 사이클이 멈춘 경우를 알린다(2026-09-09 추가).

    check_bot_status_change는 프로세스의 생사만 본다 — 프로세스가 떠 있는 채로 거래소 응답을
    기다리며 멈추거나 예외 루프에 빠지면 알림이 한 건도 안 가고, /status를 직접 쳐보기 전엔 알
    방법이 없었다. 봇이 꺼져 있을 때는 아무 말도 안 한다(하트비트가 낡은 게 당연하고,
    check_bot_status_change가 이미 알렸다). 상태가 바뀔 때만 한 번씩 보낸다."""
    label = "LIVE" if env == "live" else "DEMO"
    env_state = state.setdefault(env, {})
    was_stalled = env_state.get("heartbeat_stalled", False)

    if not bot_process.get_status(env)["running"]:
        env_state["heartbeat_stalled"] = False
        return None

    heartbeat = read_heartbeat(path=_heartbeat_path(env))
    age = heartbeat.get("age_seconds") if heartbeat else None
    stalled = age is not None and age > HEARTBEAT_STALE_SECONDS
    env_state["heartbeat_stalled"] = stalled

    if stalled and not was_stalled:
        return (f"⚠️ [{label}] 프로세스는 살아있는데 마지막 사이클이 {age / 60:.0f}분 전입니다"
                f" — 거래소 응답 대기나 오류 루프일 수 있습니다.")
    if was_stalled and not stalled:
        return f"✅ [{label}] 사이클이 다시 정상적으로 돌고 있습니다."
    return None


def check_breaker_halt(env: str, state: dict, now: datetime = None) -> str | None:
    """서킷브레이커 정지가 BREAKER_HALT_ALERT_HOURS 넘게 계속되면 한 번 알리고, 풀리면 한 번 더
    알린다(2026-09-22 추가, check_heartbeat_stall과 같은 상태 전이 패턴).

    저널의 circuit_breaker_blocked 알림은 "막히기 시작했다"는 한 통뿐이라 "아직도 막혀 있다"로
    읽히지 않았다 — 데모 봇이 연속손실 5/5로 2일간 멈춰 있는 걸 아무도 몰랐다. "지금도 막혀
    있는가"는 저널에 없고 봇이 매 사이클 쓰는 하트비트에만 있다.

    정지 시작 시각은 이 프로세스가 처음 목격한 시각이다 — 텔레그램 봇을 재시작하면 다시 센다
    (늦게 알리는 쪽으로만 틀린다). 봇이 꺼졌거나 하트비트가 낡았으면 판단하지 않는다(다른 알림이
    이미 맡는다). 하트비트에 필드가 없으면(재시작 전 옛 봇 코드) 알 수 없으므로 아무것도 안 바꾼다."""
    if BREAKER_HALT_ALERT_HOURS <= 0:
        return None
    label = "LIVE" if env == "live" else "DEMO"
    env_state = state.setdefault(env, {})
    now = now or datetime.now().astimezone()

    if not bot_process.get_status(env)["running"]:
        env_state.pop("breaker_blocked_since", None)
        env_state["breaker_halt_alerted"] = False
        return None

    heartbeat = read_heartbeat(path=_heartbeat_path(env))
    if (not heartbeat or "circuit_breaker_blocked" not in heartbeat
            or heartbeat.get("age_seconds", 0) > HEARTBEAT_STALE_SECONDS):
        return None

    if not heartbeat["circuit_breaker_blocked"]:
        was_alerted = env_state.get("breaker_halt_alerted", False)
        env_state.pop("breaker_blocked_since", None)
        env_state["breaker_halt_alerted"] = False
        return f"✅ [{label}] 서킷브레이커 정지가 풀려 신규 진입을 다시 평가합니다." if was_alerted else None

    since_raw = env_state.get("breaker_blocked_since")
    if since_raw is None:
        env_state["breaker_blocked_since"] = now.isoformat()
        return None
    hours = (now - datetime.fromisoformat(since_raw)).total_seconds() / 3600
    if hours < BREAKER_HALT_ALERT_HOURS or env_state.get("breaker_halt_alerted"):
        return None

    env_state["breaker_halt_alerted"] = True
    return (f"⏸ [{label}] 서킷브레이커로 {hours:.0f}시간째 신규 진입이 멈춰 있습니다"
            f" — {heartbeat.get('breaker_reason')}.\n연속손실은 마지막 손실 {CONSECUTIVE_LOSS_COOLDOWN_HOURS:g}시간 뒤,"
            f" 일일손실은 날짜가 바뀌면 자동 재개됩니다. 연속손실을 바로 풀려면 /reset_streak_{env}")


def check_ip_change(state: dict) -> str | None:
    """공인 IP가 바뀌면 알린다 — 바이낸스 API 키의 IP 화이트리스트를 갱신해야 할 수 있다는 신호."""
    ip = get_public_ip()
    if ip is None:
        return None  # 조회 자체가 실패한 것뿐이므로 "바뀜"으로 오인하지 않는다
    last_ip = state.get("last_ip")
    state["last_ip"] = ip

    if last_ip is None or ip == last_ip:
        return None
    return f"🌐 공인 IP가 바뀌었습니다: {last_ip} → {ip} — 바이낸스 API 키의 IP 화이트리스트를 갱신하세요."


def is_authorized(update: dict, chat_id: str) -> bool:
    """설정된 TELEGRAM_CHAT_ID와 발신자가 일치하는지 확인한다 — 토큰이 유출돼도 다른 사람이
    원격으로 봇을 켜고 끄지 못하게 하는 최소한의 접근 제어."""
    message = update.get("message") or update.get("edited_message") or {}
    sender_chat_id = (message.get("chat") or {}).get("id")
    return sender_chat_id is not None and str(sender_chat_id) == str(chat_id)


def dispatch_command(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    command = text.strip().split()[0].split("@")[0]  # 그룹챗의 "/cmd@봇이름" 형태도 처리
    return _COMMANDS.get(command)


def _proximity_bar(value: float, width: int = 10) -> str:
    """0~1을 텍스트 게이지로. 텔레그램은 HTML/마크다운 파싱 없이도 읽히게 블록 문자만 쓴다."""
    filled = max(0, min(width, round(value * width)))
    return "█" * filled + "░" * (width - filled)


def format_conditions(limit: int = 6) -> str:
    """진입 조건에 가까운 순으로 종목을 보여준다 — "왜 안 들어가는지"를 폰에서 바로 보기 위한 것.

    계산은 대시보드 /api/conditions와 완전히 같은 함수(core.signal_status.collect_conditions)를
    쓴다. 시세 자체는 공개 데이터라 계좌 키가 없어도 되지만 "어떤 심볼을 볼지"는 계좌 마켓에 따라
    달라서(SOXL은 실계좌에만 있음) 데모 기준 목록을 쓰고, 연결이 안 되면 설정값 전체로 폴백한다.

    limit: 상위 몇 종목까지 자세히 보여줄지 — 12종목을 전부 풀어 쓰면 한 메시지가 너무 길어져서
    나머지는 근접도만 한 줄로 접는다."""
    try:
        client = get_futures_client("demo")
        client.load_markets()
        symbols = [s for s in FUTURES_SYMBOLS if s in client.markets]
    except Exception:
        symbols = list(FUTURES_SYMBOLS)

    try:
        payload = collect_conditions(symbols)
    except Exception as exc:
        return f"조건 조회 실패: {exc}"

    rows = payload["symbols"]
    if not rows:
        return "조회 가능한 종목이 없습니다."

    lines = [f"[진입 조건] {payload['timeframe']} · ADX≥{payload['adx_threshold']:.0f} · "
             f"SMA{payload['sma_period']} 돌파 · 레짐 SMA{payload['regime_sma_period']}"]

    for row in rows[:limit]:
        name = row["symbol"].split("/")[0]
        if row["ready"]:
            signal = _SIGNAL_LABELS.get(row["signal"], row["signal"])
            lines.append(f"\n🔥 {name} — 지금 {signal} 신호!")
            continue
        if row["candidate_side"] is None:
            lines.append(f"\n▸ {name} — {', '.join(row['blockers']) or '계산 불가'}")
            continue
        side = _SIGNAL_LABELS.get(row["candidate_side"], row["candidate_side"])
        lines.append(
            f"\n▸ {name} {side} 대기  {_proximity_bar(row['proximity'])} {row['proximity'] * 100:.0f}%"
            f"\n  SMA{row['sma_period']}까지 {row['distance_pct']:+.2f}%"
            f" · ADX {row['adx']:.1f}/{row['adx_threshold']:.0f}"
            f" · {row.get('direction_label') or ''}"
            f"\n  {' · '.join(row['blockers'])}"
        )

    rest = rows[limit:]
    if rest:
        lines.append("\n" + ", ".join(
            f"{r['symbol'].split('/')[0]} {r['proximity'] * 100:.0f}%" for r in rest))
    for err in payload.get("errors", []):
        lines.append(f"\n⚠️ {err['symbol'].split('/')[0]} 조회 실패: {err['message'][:60]}")
    return "\n".join(lines)


def _format_equity_line(env: str, day: str) -> str:
    """그날 자산이 얼마에서 얼마가 됐는지. 기록이 없으면 빈 문자열(줄을 빼고 나간다).

    실현손익·R과 달리 이 값만이 **계좌 화면의 총자산과 직접 맞춰볼 수 있다** — 미실현 변동,
    펀딩비, 입출금이 전부 반영돼 있기 때문이다. 사용자가 "수익이라는데 총자산은 그대로"라고
    한 것이 정확히 이 간극이었다(2026-09-24).
    """
    change = equity_log.day_change(day, path=_equity_log_path(env))
    if not change:
        return ""

    pct = f", {change['change_pct'] * 100:+.2f}%" if change["change_pct"] is not None else ""
    line = (f"  자산 {change['start']:.2f} → {change['end']:.2f} "
            f"({change['change']:+.2f}{pct})")

    # 봇이 꺼져 있던 동안의 변화는 그날 거래로 설명되지 않는다 — 숨기면 또 안 맞는다.
    overnight = change.get("overnight_change")
    if overnight is not None and abs(overnight) >= 0.01:
        line += (f"\n  (전날 마감 {change['prev_end']:.2f} → 당일 시작 사이 "
                 f"{overnight:+.2f})")
    return line


def _format_day_line(env: str, day: str) -> str:
    """한 계좌의 하루 성과를 두어 줄로. 거래가 없었으면 그렇다고만 말한다."""
    label = "LIVE" if env == "live" else "DEMO"
    stats = summarize_day(read_entries(path=_journal_path(env)), day)

    if not stats["trades"]:
        extras = []
        if stats["rejections"]:
            extras.append(f"거부 {stats['rejections']}건")
        if stats["circuit_breakers"]:
            extras.append(f"서킷브레이커 {stats['circuit_breakers']}회")
        tail = f" ({' · '.join(extras)})" if extras else ""
        head = f"[{label}] 청산된 거래 없음{tail}"
        # 거래가 없어도 자산은 움직인다(보유 포지션 평가손익, 펀딩비). 그 줄이 빠지면
        # "거래 없음 = 변화 없음"으로 읽혀 계좌 화면과 또 어긋난다.
        return "\n".join([head] + [line for line in [_format_equity_line(env, day)] if line])

    # **수수료 뺀 값을 앞에 놓는다.** 거래소의 realizedPnl에는 수수료가 안 들어있어서, 그걸
    # 그대로 알리면 "+5 USDT 수익"이라고 해놓고 계좌 총자산은 줄어 있는 일이 생긴다
    # (2026-09-24 사용자 보고). 이 전략은 건당 기대값과 수수료가 같은 크기라 부호까지 뒤집힌다.
    net_r = stats.get("net_total_r")
    r_text = f"{net_r:+.2f}R" if net_r is not None else "R 측정 불가"
    net_pnl = stats.get("net_realized_pnl")
    pnl_text = (f"{net_pnl:+.2f} USDT" if net_pnl is not None
                else f"{stats['realized_pnl']:+.2f} USDT")
    lines = [f"[{label}] {stats['trades']}건 · 승 {stats['wins']} / 패 {stats['losses']}"
             f" · {r_text} · {pnl_text}"]

    fees = stats.get("fees")
    if fees:
        tail = " 추정" if stats.get("fees_estimated") else ""
        lines.append(f"  수수료 {fees:.2f} 차감{tail} (차감 전 {stats['realized_pnl']:+.2f})")

    lines.append(_format_equity_line(env, day))
    lines = [line for line in lines if line]

    blocked = filter_stats.format_counts(
        filter_stats.read_counts(path=_filter_stats_path(env), days=14).get(day, {}))
    if blocked:
        lines.append(f"  차단: {blocked}")
    if stats["rejections"] or stats["circuit_breakers"]:
        lines.append(f"  거부 {stats['rejections']}건 · 서킷브레이커 {stats['circuit_breakers']}회")
    return "\n".join(lines)


def _format_goal_line(env: str) -> str:
    """최근 30일 **자산** 수익률과 목표(월 +10%)의 거리. 기록이 모자라면 빈 문자열.

    R 합계로는 이 줄을 만들 수 없다 — 사이징이 바뀌면 같은 R이 다른 금액이 되고, 미실현
    변동과 펀딩비가 빠진다. 목표가 금액 기준이므로 비교도 금액 기준이어야 한다
    (`docs/OBJECTIVE.md`).
    """
    period = equity_log.period_return(path=_equity_log_path(env), days=30)
    if not period:
        return ""
    label = "LIVE" if env == "live" else "DEMO"
    pct = period["return_pct"] * 100
    return (f"[{label}] 최근 {period['days']}일 자산 {period['start']:.2f} → "
            f"{period['end']:.2f} ({pct:+.2f}%) · 목표 월 +10%")


def build_daily_summary(day: str) -> str:
    """전날 성과 한 통. 데모/실계좌를 한 메시지에 담는다 — 두 통으로 나누면 폰에서 비교가 안 된다.

    금액은 전부 **수수료를 뺀 값**이고, 자산 줄은 계좌 총자산과 직접 맞춰볼 수 있는 값이다
    (2026-09-24). 그 전에는 수수료가 빠진 실현손익만 알려서 "수익이라는데 총자산은 그대로"가
    반복됐다 — 요약이 계좌 화면과 어긋나면 요약 자체가 쓸모없다.
    """
    lines = [f"📊 일일 요약 · {day}",
             _format_day_line("demo", day),
             _format_day_line("live", day)]
    goals = [line for line in (_format_goal_line("demo"), _format_goal_line("live")) if line]
    if goals:
        lines.append("— 목표 진행 —")
        lines.extend(goals)
    return "\n".join(lines)


def check_daily_summary(state: dict, now: datetime = None) -> str | None:
    """하루에 한 번, 설정한 시각(로컬)을 지나면 전날 요약을 돌려준다. 이미 보낸 날이면 None.

    "지금까지의 오늘"이 아니라 **전날 하루치**를 보내는 이유는 그래야 완결된 하루이기 때문이다 —
    일일 손실 한도가 리셋되는 경계와 같은 기준(로컬 날짜)을 쓴다.

    다른 알림들과 달리 최초 실행에서도 건너뛰지 않는다. 과거 이력을 한꺼번에 쏘는 문제(그래서
    check_new_journal_entries는 워터마크만 잡고 넘어간다)가 여기엔 없다 — 어차피 하루에 한 통이라
    첫 실행에 한 통 나가는 게 오히려 "기능이 살아있다"는 확인이 된다."""
    if TELEGRAM_DAILY_SUMMARY_HOUR < 0:
        return None
    now = now or datetime.now().astimezone()
    if now.hour < TELEGRAM_DAILY_SUMMARY_HOUR:
        return None

    target = (now.date() - timedelta(days=1)).isoformat()
    if state.get("last_summary_date") == target:
        return None
    state["last_summary_date"] = target
    return build_daily_summary(target)


def _format_excursion(record: dict | None) -> str:
    """보유 중 최고/최저 지점을 R배수로 한 줄 덧붙인다. "지금 +0.4R인데 아까 +1.6R까지 갔었다"를
    바로 보여주려는 것 — 이 체감("양전했다가 익절 못 닿고 흘러내려 손절")이 전략을 다시 들여다본
    출발점이었는데, 지금까지는 5분봉으로 경로를 재구성해야만 확인할 수 있었다(2026-09-09)."""
    if not record:
        return ""
    mfe, mae = record.get("max_favorable_r"), record.get("max_adverse_r")
    if mfe is None or mae is None:
        return ""
    return f"\n  최고 {mfe:+.2f}R · 최저 {mae:+.2f}R"


def format_status(env: str) -> str:
    """[env] 실행 여부/하트비트 + 마진 자산 + 보유 포지션별 진입가·현재가·미실현손익·손절가·익절가.
    계좌 연결 자체가 실패해도(라이브 키 미설정 등) 실행 여부/하트비트는 이미 계산해둔 걸 그대로
    보여주고 그 아래에만 실패 사유를 붙인다 — 대시보드 api_status의 부분 실패 격리와 같은 철학."""
    label = "LIVE" if env == "live" else "DEMO"
    status = bot_process.get_status(env)
    heartbeat = read_heartbeat(path=_heartbeat_path(env))

    lines = [f"[{label}] {'실행 중' if status['running'] else '중지됨'}"]
    if heartbeat:
        lines.append(f"마지막 사이클: {heartbeat.get('age_seconds', 0):.0f}초 전 (#{heartbeat.get('cycle_count')})")
    else:
        lines.append("사이클 기록 없음")

    try:
        client = get_futures_client(env)
        client.load_markets()
        balance = get_futures_balance(client)
        margin_equity = (balance.get("USDT") or {}).get("total") or 0.0
        lines.append(f"마진 자산: ${margin_equity:,.2f}")

        # 오늘 필터가 몇 번 진입을 걸렀는지 — 저널에 안 남는 이벤트라 이 집계 말고는 볼 방법이 없다.
        today_counts = next(iter(filter_stats.read_counts(path=_filter_stats_path(env), days=1).values()), {})
        blocked_line = filter_stats.format_counts(today_counts)
        if blocked_line:
            lines.append(f"오늘 차단: {blocked_line}")

        excursions = excursion.read_all(path=_excursion_path(env))
        available_symbols = [s for s in FUTURES_SYMBOLS if s in client.markets]
        for symbol in available_symbols:
            try:
                position = get_position(client, symbol)
            except Exception:
                continue
            if position is None:
                continue

            short_symbol = symbol.split("/")[0]
            side = "롱" if position.get("side") == "long" else "숏"
            pnl = position.get("unrealizedPnl")
            pnl_str = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "-"
            try:
                stop_loss_price, take_profit_price = get_bracket_prices(client, symbol)
            except Exception:
                stop_loss_price, take_profit_price = None, None

            lines.append(
                f"\n▸ {short_symbol} {side}\n"
                f"  진입 {_fmt_price(position.get('entryPrice'))} · 현재가 {_fmt_price(position.get('markPrice'))}"
                f" · 손익 {pnl_str} USDT\n"
                f"  손절 {_fmt_price(stop_loss_price)} · 익절 {_fmt_price(take_profit_price)}"
                + _format_excursion(excursions.get(symbol))
            )
    except LiveKeysNotConfiguredError:
        lines.append("계좌 연결: 라이브 키 미설정")
    except Exception as exc:
        lines.append(f"계좌 연결 실패: {exc}")

    return "\n".join(lines)


def run_once(state: dict, chat_id: str, token: str) -> dict:
    """감시 루프 한 사이클: 새 명령 처리 + 새 저널 이벤트/봇 상태 전이/IP 변경 알림. 갱신된 state를
    돌려준다(호출자가 저장)."""
    updates = get_updates(offset=state.get("update_offset"), timeout=TELEGRAM_POLL_INTERVAL_SECONDS,
                           token=token)

    max_update_id = None
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

        if not is_authorized(update, chat_id):
            logger.warning("ignoring telegram update from unauthorized chat: %s",
                            (update.get("message") or {}).get("chat"))
            continue

        text = ((update.get("message") or {}).get("text") or "").strip()
        parsed = dispatch_command(text)
        if parsed is None:
            send_message("알 수 없는 명령입니다. /help를 참고하세요.", chat_id=chat_id, token=token)
            continue

        env, action = parsed
        if action == "help":
            send_message(_HELP_TEXT, chat_id=chat_id, token=token)
        elif action == "conditions":
            send_message(format_conditions(), chat_id=chat_id, token=token)
        elif action == "summary":
            yesterday = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
            send_message(build_daily_summary(yesterday), chat_id=chat_id, token=token)
        elif action == "status":
            send_message(format_status("demo") + "\n\n" + format_status("live"), chat_id=chat_id, token=token)
        elif action == "start":
            result = bot_process.start(env)
            send_message(f"✅ [{env.upper()}] 봇 시작 요청됨 (pid={result.get('pid')})", chat_id=chat_id, token=token)
        elif action == "stop":
            bot_process.stop(env)
            send_message(f"🛑 [{env.upper()}] 봇을 중지했습니다.", chat_id=chat_id, token=token)
        elif action == "reset_streak":
            reset_consecutive_losses(journal_path=_journal_path(env))
            send_message(f"🔄 [{env.upper()}] 연속손실 카운트를 0으로 리셋했습니다.", chat_id=chat_id, token=token)

    if max_update_id is not None:
        state["update_offset"] = max_update_id + 1

    for env in ("demo", "live"):
        for message in check_new_journal_entries(env, state):
            send_message(message, chat_id=chat_id, token=token)
        status_message = check_bot_status_change(env, state)
        if status_message:
            send_message(status_message, chat_id=chat_id, token=token)
        stall_message = check_heartbeat_stall(env, state)
        if stall_message:
            send_message(stall_message, chat_id=chat_id, token=token)
        halt_message = check_breaker_halt(env, state)
        if halt_message:
            send_message(halt_message, chat_id=chat_id, token=token)

    summary_message = check_daily_summary(state)
    if summary_message:
        send_message(summary_message, chat_id=chat_id, token=token)

    ip_message = check_ip_change(state)
    if ip_message:
        send_message(ip_message, chat_id=chat_id, token=token)

    return state
