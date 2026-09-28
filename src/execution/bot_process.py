import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_SCRIPT = PROJECT_ROOT / "scripts" / "run_futures_bot.py"
TELEGRAM_BOT_SCRIPT = PROJECT_ROOT / "scripts" / "run_telegram_bot.py"

# 데모/실계좌를 동시에 띄울 수 있게 key별로 완전히 분리된 PID 파일을 쓴다(2026-08-22).
# 데모 쪽 파일명은 기존 그대로 유지 — 이미 떠 있는 데모 봇의 PID 추적이 끊기지 않게.
# "telegram"은 env가 아니라 별개의 상시 알림/원격제어 프로세스지만(2026-08-22), 스폰/검증/graceful
# stop 로직이 완전히 동일해서 같은 PID_PATHS/키 체계에 얹었다 — 로직을 두 번 안 쓰기 위함.
PID_PATHS = {
    "demo": PROJECT_ROOT / "state" / "futures_bot.pid",
    "live": PROJECT_ROOT / "state" / "futures_bot.live.pid",
    "telegram": PROJECT_ROOT / "state" / "telegram_bot.pid",
}

# 정상 종료는 "종료 요청 파일"로 한다 — 봇이 사이클 사이 대기 중에 이 파일을 보고 스스로 끝낸다.
# 텔레그램 봇은 long-poll(최대 약 15초)을 마친 뒤에야 확인하므로 여유를 둔다.
GRACEFUL_STOP_TIMEOUT_SECONDS = 30
STDERR_LOG_DIR = PROJECT_ROOT / "logs"


def _pid_path(key: str) -> Path:
    return PID_PATHS.get(key, PID_PATHS["demo"])


def _stop_path(key: str) -> Path:
    return _pid_path(key).with_suffix(".stop")


def stop_requested(key: str) -> bool:
    """봇 루프가 대기 중에 부른다 — True면 지금 사이클을 끝으로 스스로 종료해야 한다."""
    return _stop_path(key).exists()


def _clear_stop_request(key: str) -> None:
    try:
        _stop_path(key).unlink()
    except FileNotFoundError:
        pass


def _launch_args(key: str) -> list[str]:
    """python 실행파일 뒤에 붙는 인자들(스크립트 경로 포함) — key마다 띄우는 스크립트/인자가 다름."""
    if key == "telegram":
        return [str(TELEGRAM_BOT_SCRIPT)]
    return [str(BOT_SCRIPT), "--env", key]


def _match_tokens(key: str) -> list[str]:
    """cmdline에 전부 포함돼야 "우리가 띄운 그 프로세스"로 인정한다. run_futures_bot.py는 데모/
    실계좌가 같은 스크립트를 --env만 다르게 받으므로 둘 다 확인해야 서로 뒤바뀐 걸로 오인하지
    않는다(2026-08-22). telegram은 그 자체로 유일한 스크립트라 이름만 확인하면 충분."""
    if key == "telegram":
        return ["run_telegram_bot.py"]
    return ["run_futures_bot.py", f"--env {key}"]


def _read_pid(key: str = "demo") -> int | None:
    pid_path = _pid_path(key)
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(key: str, pid: int) -> None:
    pid_path = _pid_path(key)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def _clear_pid(key: str = "demo") -> None:
    try:
        _pid_path(key).unlink()
    except FileNotFoundError:
        pass


def _is_our_bot_process(pid: int, key: str = "demo") -> bool:
    """PID가 살아있고 실제로 이 key용 스크립트를 실행 중인지 확인한다. 대시보드(Flask) 프로세스가
    재시작되면 메모리 상의 프로세스 핸들은 사라지지만 실제 봇 프로세스는 계속 떠 있을 수 있으므로,
    PID 파일 하나만 믿지 않고 그 PID가 다른 프로그램에 재사용된 건 아닌지 커맨드라인으로 검증한다."""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
        return all(token in cmdline for token in _match_tokens(key))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def get_status(key: str = "demo") -> dict:
    pid = _read_pid(key)
    if pid is not None and _is_our_bot_process(pid, key):
        started_at = psutil.Process(pid).create_time()
        return {"running": True, "pid": pid, "started_at": started_at}
    if pid is not None:
        _clear_pid(key)  # 죽은 프로세스거나 다른 프로그램이 PID를 재사용한 경우 — 정리
    return {"running": False, "pid": None, "started_at": None}


def start(key: str = "demo") -> dict:
    status = get_status(key)
    if status["running"]:
        return status

    _clear_stop_request(key)  # 지난번 종료 요청이 남아 있으면 켜자마자 꺼진다
    # 봇은 띄운 쪽(대시보드 콘솔 창, 텔레그램 봇)과 **분리된** 프로세스로 띄운다(2026-09-28).
    # 예전엔 콘솔을 물려받아서, 대시보드 창을 닫으면 Windows가 그 콘솔에 붙은 봇까지 같이 죽였다.
    # CREATE_NO_WINDOW = 창이 없는 **자기만의** 콘솔. DETACHED_PROCESS(콘솔 없음)는 쓰면 안 된다 —
    # venv의 python.exe는 진짜 인터프리터를 자식으로 다시 띄우는 실행기라, 콘솔 없는 실행기의
    # 자식이 새 콘솔 **창**을 만들어 봇마다 창이 하나씩 떴다(그 창을 닫으면 봇이 죽는다).
    # 화면 출력은 버리고(로그는 각 스크립트가 logs/*.log에 쓴다), 로깅 시작 전 오류만 파일에 남긴다.
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        popen_kwargs = {"creationflags": creationflags}
    else:
        popen_kwargs = {"start_new_session": True}
    STDERR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STDERR_LOG_DIR / f"{key}_process.err", "ab") as stderr_file:
        proc = subprocess.Popen(
            [sys.executable, *_launch_args(key)],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            **popen_kwargs,
        )
    _write_pid(key, proc.pid)
    return {"running": True, "pid": proc.pid, "started_at": time.time()}


def stop(key: str = "demo") -> dict:
    pid = _read_pid(key)
    if pid is None or not _is_our_bot_process(pid, key):
        _clear_pid(key)
        _clear_stop_request(key)
        return {"running": False, "pid": None, "started_at": None}

    # 분리된 프로세스에는 콘솔 신호(CTRL_BREAK)가 닿지 않는다 — 종료 요청 파일을 남기면 봇이 사이클
    # 사이에서 스스로 끝낸다(주문을 내는 도중에 끊기지 않게).
    _stop_path(key).parent.mkdir(parents=True, exist_ok=True)
    _stop_path(key).write_text(str(time.time()), encoding="utf-8")
    # Windows에서는 CTRL_BREAK를 보내지 않는다 — 콘솔을 공유하지 않는 프로세스에 보내면 보내는 쪽
    # 콘솔 전체(대시보드 자신)에 신호가 갈 수 있다. 유예 시간 안에 안 끝나면 아래에서 강제 종료한다.
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.time() + GRACEFUL_STOP_TIMEOUT_SECONDS
    while time.time() < deadline and _is_our_bot_process(pid, key):
        time.sleep(0.5)

    if _is_our_bot_process(pid, key):
        try:
            psutil.Process(pid).kill()  # 유예 시간 안에 정상 종료 안 되면 강제 종료
        except psutil.NoSuchProcess:
            pass

    _clear_pid(key)
    _clear_stop_request(key)
    return {"running": False, "pid": None, "started_at": None}
