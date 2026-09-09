import json
import logging
from pathlib import Path

from src.core.config import FUTURES_SYMBOLS, TELEGRAM_POLL_INTERVAL_SECONDS
from src.core.signal_status import collect_conditions
from src.data.futures_exchange import (
    LiveKeysNotConfiguredError,
    get_futures_balance,
    get_futures_client,
    get_position,
)
from src.data.public_ip import get_public_ip
from src.execution import bot_process
from src.execution.futures_orders import get_bracket_prices
from src.execution.heartbeat import DEFAULT_PATH as HEARTBEAT_DEMO_PATH
from src.execution.heartbeat import LIVE_DEFAULT_PATH as HEARTBEAT_LIVE_PATH
from src.execution.heartbeat import read_heartbeat
from src.execution.journal import read_entries
from src.execution.telegram_client import get_updates, send_message
from src.futures_rule_bot import JOURNAL_PATH, LIVE_JOURNAL_PATH, reset_consecutive_losses

logger = logging.getLogger(__name__)

STATE_PATH = "state/telegram_bot_state.json"

# 진입/청산/서킷브레이커만 폰으로 쏜다 — no_signal/holding_position/skipped_max_positions는 애초에
# 저널에 안 남고(futures_rule_bot._SILENT_EVENTS), rejected_exchange_error 등은 저널엔 남지만
# 매 사이클 반복될 수 있어 알림으로는 노이즈라 제외한다.
_NOTIFY_EVENTS = ("entered", "closed", "circuit_breaker_blocked")

_REASON_LABELS = {
    "stop_loss": "손절", "take_profit": "익절", "manual": "수동청산",
    "breakeven_stop": "손익분기청산", "unknown": "기타",
}
_SIGNAL_LABELS = {"LONG": "롱", "SHORT": "숏"}

_HELP_TEXT = (
    "사용 가능한 명령:\n"
    "/status — 데모/실계좌 상태 조회\n"
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
    "/conditions": ("", "conditions"),
    "/help": ("", "help"),
}


def _journal_path(env: str) -> str:
    return LIVE_JOURNAL_PATH if env == "live" else JOURNAL_PATH


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
            f" · ADX {row['adx']:.1f}/{row['adx_threshold']:.0f} · RSI {row['rsi']:.1f}"
            f"\n  {' · '.join(row['blockers'])}"
        )

    rest = rows[limit:]
    if rest:
        lines.append("\n" + ", ".join(
            f"{r['symbol'].split('/')[0]} {r['proximity'] * 100:.0f}%" for r in rest))
    for err in payload.get("errors", []):
        lines.append(f"\n⚠️ {err['symbol'].split('/')[0]} 조회 실패: {err['message'][:60]}")
    return "\n".join(lines)


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

    ip_message = check_ip_change(state)
    if ip_message:
        send_message(ip_message, chat_id=chat_id, token=token)

    return state
