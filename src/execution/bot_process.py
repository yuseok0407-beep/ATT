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
    "demo2": PROJECT_ROOT / "state" / "futures_bot.demo2.pid",  # 두 번째 데모 계좌(2026-09-26)
    "telegram": PROJECT_ROOT / "state" / "telegram_bot.pid",
}

GRACEFUL_STOP_TIMEOUT_SECONDS = 15


def _pid_path(key: str) -> Path:
    # 모르는 key를 데모 PID 파일로 폴백하면 "demo2 중지"가 데모 봇을 죽인다 — KeyError로 막는다.
    return PID_PATHS[key]


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
        args = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return _cmdline_matches(args, key)


def _cmdline_matches(args: list[str], key: str) -> bool:
    """문자열 포함이 아니라 **인자 단위로** 비교한다(2026-09-26) — "--env demo"는 "--env demo2"의
    부분 문자열이라, 이어붙인 커맨드라인에서 찾으면 demo2 프로세스가 데모 봇으로 인정된다."""
    joined = " ".join(args)
    tokens = _match_tokens(key)
    if not all(token in joined for token in tokens if not token.startswith("--env ")):
        return False
    env_tokens = [token.split(" ", 1)[1] for token in tokens if token.startswith("--env ")]
    if not env_tokens:
        return True
    pairs = {args[i + 1] for i in range(len(args) - 1) if args[i] == "--env"}
    return all(env in pairs for env in env_tokens)


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

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, *_launch_args(key)],
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
    )
    _write_pid(key, proc.pid)
    return {"running": True, "pid": proc.pid, "started_at": time.time()}


def stop(key: str = "demo") -> dict:
    pid = _read_pid(key)
    if pid is None or not _is_our_bot_process(pid, key):
        _clear_pid(key)
        return {"running": False, "pid": None, "started_at": None}

    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # 봇의 KeyboardInterrupt 처리로 정상 종료 유도
        else:
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
    return {"running": False, "pid": None, "started_at": None}
