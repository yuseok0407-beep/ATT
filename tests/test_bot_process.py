from unittest.mock import MagicMock, patch

import psutil
import pytest

from src.execution import bot_process


@pytest.fixture(autouse=True)
def _pid_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_process, "PID_PATHS", {
        "demo": tmp_path / "futures_bot.pid",
        "live": tmp_path / "futures_bot.live.pid",
        "demo2": tmp_path / "futures_bot.demo2.pid",
        "telegram": tmp_path / "telegram_bot.pid",
    })
    yield


def _mock_live_process(cmdline_contains="run_futures_bot.py --env demo", create_time=1700000000.0):
    proc = MagicMock()
    # 실제 psutil처럼 인자 단위로 쪼개서 준다 — 판정이 인자 단위라서.
    proc.cmdline.return_value = ["python", *("scripts/" + cmdline_contains).split()]
    proc.create_time.return_value = create_time
    return proc


def test_get_status_no_pid_file_means_not_running_for_demo():
    status = bot_process.get_status("demo")
    assert status == {"running": False, "pid": None, "started_at": None}


def test_get_status_no_pid_file_means_not_running_for_live():
    status = bot_process.get_status("live")
    assert status == {"running": False, "pid": None, "started_at": None}


def test_get_status_defaults_to_demo():
    assert bot_process.get_status() == bot_process.get_status("demo")


def test_get_status_live_matching_process_reports_running():
    bot_process._write_pid("demo", 4242)
    with patch("src.execution.bot_process.psutil.Process", return_value=_mock_live_process()):
        status = bot_process.get_status("demo")
    assert status["running"] is True
    assert status["pid"] == 4242
    assert status["started_at"] == 1700000000.0


def test_get_status_dead_pid_clears_file():
    bot_process._write_pid("demo", 4242)
    with patch("src.execution.bot_process.psutil.Process", side_effect=psutil.NoSuchProcess(4242)):
        status = bot_process.get_status("demo")
    assert status["running"] is False
    assert not bot_process._pid_path("demo").exists()


def test_get_status_pid_reused_by_other_process_reports_not_running():
    bot_process._write_pid("demo", 4242)
    other_proc = _mock_live_process(cmdline_contains="some_other_program.py")
    with patch("src.execution.bot_process.psutil.Process", return_value=other_proc):
        status = bot_process.get_status("demo")
    assert status["running"] is False


def test_get_status_ignores_process_from_a_different_env():
    """데모 PID 파일이 가리키는 프로세스가 사실은 --env live로 뜬 프로세스라면(예: PID가 재사용된
    경우) get_status("demo")는 이걸 데모 봇으로 오인하면 안 된다 — 실계좌 프로세스를 데모로
    착각하거나 그 반대가 되는 사고를 막기 위한 핵심 안전장치(2026-08-22)."""
    bot_process._write_pid("demo", 4242)
    live_proc = _mock_live_process(cmdline_contains="run_futures_bot.py --env live")
    with patch("src.execution.bot_process.psutil.Process", return_value=live_proc):
        status = bot_process.get_status("demo")
    assert status["running"] is False
    assert not bot_process._pid_path("demo").exists()


def test_demo_and_live_pid_files_are_independent():
    bot_process._write_pid("demo", 111)
    bot_process._write_pid("live", 222)
    assert bot_process._read_pid("demo") == 111
    assert bot_process._read_pid("live") == 222


def test_start_spawns_subprocess_with_env_flag_and_writes_pid_when_not_running():
    with patch("src.execution.bot_process.get_status", return_value={"running": False, "pid": None, "started_at": None}), \
         patch("src.execution.bot_process.subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 9999
        result = bot_process.start("live")

    args, kwargs = mock_popen.call_args
    assert args[0][-2:] == ["--env", "live"]
    assert result["running"] is True
    assert result["pid"] == 9999
    assert bot_process._read_pid("live") == 9999


def test_start_does_not_spawn_a_second_process_when_already_running():
    already_running = {"running": True, "pid": 4242, "started_at": 1700000000.0}
    with patch("src.execution.bot_process.get_status", return_value=already_running), \
         patch("src.execution.bot_process.subprocess.Popen") as mock_popen:
        result = bot_process.start()

    mock_popen.assert_not_called()
    assert result == already_running


def test_stop_sends_graceful_signal_then_clears_pid_once_process_exits():
    bot_process._write_pid("demo", 4242)
    call_count = {"n": 0}

    def fake_is_our_bot_process(pid, env):
        call_count["n"] += 1
        return call_count["n"] < 3  # 두어 번 살아있다가 그 다음부터 죽었다고 응답

    with patch("src.execution.bot_process.os.kill") as mock_kill, \
         patch("src.execution.bot_process._is_our_bot_process", side_effect=fake_is_our_bot_process), \
         patch("src.execution.bot_process.time.sleep"), \
         patch("src.execution.bot_process.psutil.Process") as mock_process_cls:
        result = bot_process.stop()

    mock_kill.assert_called_once()
    mock_process_cls.return_value.kill.assert_not_called()  # 정상 종료됐으니 강제 kill까지는 안 감
    assert result == {"running": False, "pid": None, "started_at": None}
    assert not bot_process._pid_path("demo").exists()


def test_stop_escalates_to_force_kill_if_process_never_exits():
    bot_process._write_pid("demo", 4242)

    with patch("src.execution.bot_process.os.kill"), \
         patch("src.execution.bot_process._is_our_bot_process", return_value=True), \
         patch("src.execution.bot_process.time.sleep"), \
         patch("src.execution.bot_process.time.time", side_effect=[0, 0, 100]), \
         patch("src.execution.bot_process.psutil.Process") as mock_process_cls:
        bot_process.stop()

    mock_process_cls.return_value.kill.assert_called_once()
    assert not bot_process._pid_path("demo").exists()


def test_stop_when_not_running_is_a_noop():
    with patch("src.execution.bot_process.os.kill") as mock_kill:
        result = bot_process.stop()

    mock_kill.assert_not_called()
    assert result == {"running": False, "pid": None, "started_at": None}


def test_stop_only_affects_the_targeted_env():
    """live를 stop해도 demo 프로세스 PID 파일은 그대로 남아있어야 한다 — 두 봇이 서로 완전히
    독립적으로 제어돼야 한다는 이 구조의 핵심 보증(2026-08-22)."""
    bot_process._write_pid("demo", 111)
    bot_process._write_pid("live", 222)

    with patch("src.execution.bot_process.os.kill"), \
         patch("src.execution.bot_process._is_our_bot_process", return_value=False):
        bot_process.stop("live")

    assert not bot_process._pid_path("live").exists()
    assert bot_process._read_pid("demo") == 111


def test_telegram_start_spawns_run_telegram_bot_script_without_env_flag():
    """telegram은 env 구분이 없는 단일 스크립트라 --env가 붙으면 안 된다(2026-08-22)."""
    with patch("src.execution.bot_process.get_status", return_value={"running": False, "pid": None, "started_at": None}), \
         patch("src.execution.bot_process.subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 5555
        result = bot_process.start("telegram")

    args, kwargs = mock_popen.call_args
    assert args[0] == [bot_process.sys.executable, str(bot_process.TELEGRAM_BOT_SCRIPT)]
    assert result["running"] is True
    assert bot_process._read_pid("telegram") == 5555


def test_telegram_get_status_matches_only_its_own_script():
    bot_process._write_pid("telegram", 4242)
    telegram_proc = _mock_live_process(cmdline_contains="run_telegram_bot.py")
    with patch("src.execution.bot_process.psutil.Process", return_value=telegram_proc):
        status = bot_process.get_status("telegram")
    assert status["running"] is True


def test_telegram_get_status_does_not_match_futures_bot_process():
    """futures 봇(run_futures_bot.py --env demo)의 PID를 telegram 봇으로 착각하면 안 된다."""
    bot_process._write_pid("telegram", 4242)
    futures_proc = _mock_live_process(cmdline_contains="run_futures_bot.py --env demo")
    with patch("src.execution.bot_process.psutil.Process", return_value=futures_proc):
        status = bot_process.get_status("telegram")
    assert status["running"] is False


def test_telegram_stop_does_not_affect_futures_bot_pid_files():
    bot_process._write_pid("demo", 111)
    bot_process._write_pid("telegram", 333)

    with patch("src.execution.bot_process.os.kill"), \
         patch("src.execution.bot_process._is_our_bot_process", return_value=False):
        bot_process.stop("telegram")

    assert not bot_process._pid_path("telegram").exists()
    assert bot_process._read_pid("demo") == 111


# --- demo2 (2026-09-26) -------------------------------------------------------------
# "--env demo"는 "--env demo2"의 부분 문자열이다. 문자열 포함으로 판정하면 demo2 봇이 데모 봇으로
# 인정되어, 데모 PID 파일에 demo2의 PID가 남은 경우 "데모 중지"가 demo2 봇을 죽인다.

def test_demo2_process_is_not_mistaken_for_demo():
    bot_process._write_pid("demo", 4242)
    with patch("src.execution.bot_process.psutil.Process",
               return_value=_mock_live_process("run_futures_bot.py --env demo2")):
        assert bot_process.get_status("demo")["running"] is False


def test_demo2_process_is_recognized_as_demo2():
    bot_process._write_pid("demo2", 4343)
    with patch("src.execution.bot_process.psutil.Process",
               return_value=_mock_live_process("run_futures_bot.py --env demo2")):
        assert bot_process.get_status("demo2")["running"] is True


def test_demo2_launches_with_its_own_env_flag():
    assert bot_process._launch_args("demo2")[-2:] == ["--env", "demo2"]


def test_unknown_key_does_not_fall_back_to_the_demo_pid_file():
    with pytest.raises(KeyError):
        bot_process._pid_path("demo3")
