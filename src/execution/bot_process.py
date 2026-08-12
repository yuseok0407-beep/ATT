import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_SCRIPT = PROJECT_ROOT / "scripts" / "run_futures_bot.py"
PID_PATH = PROJECT_ROOT / "state" / "futures_bot.pid"

GRACEFUL_STOP_TIMEOUT_SECONDS = 15


def _read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid), encoding="utf-8")


def _clear_pid() -> None:
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass


def _is_our_bot_process(pid: int) -> bool:
    """PID가 살아있고 실제로 run_futures_bot.py를 실행 중인지 확인한다. 대시보드(Flask) 프로세스가
    재시작되면 메모리 상의 프로세스 핸들은 사라지지만 실제 봇 프로세스는 계속 떠 있을 수 있으므로,
    PID 파일 하나만 믿지 않고 그 PID가 다른 프로그램에 재사용된 건 아닌지 커맨드라인으로 검증한다."""
    try:
        proc = psutil.Process(pid)
        return "run_futures_bot.py" in " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def get_status() -> dict:
    pid = _read_pid()
    if pid is not None and _is_our_bot_process(pid):
        started_at = psutil.Process(pid).create_time()
        return {"running": True, "pid": pid, "started_at": started_at}
    if pid is not None:
        _clear_pid()  # 죽은 프로세스거나 다른 프로그램이 PID를 재사용한 경우 — 정리
    return {"running": False, "pid": None, "started_at": None}


def start() -> dict:
    status = get_status()
    if status["running"]:
        return status

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, str(BOT_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
    )
    _write_pid(proc.pid)
    return {"running": True, "pid": proc.pid, "started_at": time.time()}


def stop() -> dict:
    pid = _read_pid()
    if pid is None or not _is_our_bot_process(pid):
        _clear_pid()
        return {"running": False, "pid": None, "started_at": None}

    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # 봇의 KeyboardInterrupt 처리로 정상 종료 유도
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    deadline = time.time() + GRACEFUL_STOP_TIMEOUT_SECONDS
    while time.time() < deadline and _is_our_bot_process(pid):
        time.sleep(0.5)

    if _is_our_bot_process(pid):
        try:
            psutil.Process(pid).kill()  # 유예 시간 안에 정상 종료 안 되면 강제 종료
        except psutil.NoSuchProcess:
            pass

    _clear_pid()
    return {"running": False, "pid": None, "started_at": None}
