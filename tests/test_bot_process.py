from unittest.mock import MagicMock, patch

import psutil
import pytest

from src.execution import bot_process


@pytest.fixture(autouse=True)
def _pid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_process, "PID_PATH", tmp_path / "futures_bot.pid")
    yield


def _mock_live_process(cmdline_contains="run_futures_bot.py", create_time=1700000000.0):
    proc = MagicMock()
    proc.cmdline.return_value = ["python", "scripts/" + cmdline_contains]
    proc.create_time.return_value = create_time
    return proc


def test_get_status_no_pid_file_means_not_running():
    status = bot_process.get_status()
    assert status == {"running": False, "pid": None, "started_at": None}


def test_get_status_live_matching_process_reports_running():
    bot_process._write_pid(4242)
    with patch("src.execution.bot_process.psutil.Process", return_value=_mock_live_process()):
        status = bot_process.get_status()
    assert status["running"] is True
    assert status["pid"] == 4242
    assert status["started_at"] == 1700000000.0


def test_get_status_dead_pid_clears_file():
    bot_process._write_pid(4242)
    with patch("src.execution.bot_process.psutil.Process", side_effect=psutil.NoSuchProcess(4242)):
        status = bot_process.get_status()
    assert status["running"] is False
    assert not bot_process.PID_PATH.exists()


def test_get_status_pid_reused_by_other_process_reports_not_running():
    bot_process._write_pid(4242)
    other_proc = _mock_live_process(cmdline_contains="some_other_program.py")
    with patch("src.execution.bot_process.psutil.Process", return_value=other_proc):
        status = bot_process.get_status()
    assert status["running"] is False


def test_start_spawns_subprocess_and_writes_pid_when_not_running():
    with patch("src.execution.bot_process.get_status", return_value={"running": False, "pid": None, "started_at": None}), \
         patch("src.execution.bot_process.subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 9999
        result = bot_process.start()

    mock_popen.assert_called_once()
    assert result["running"] is True
    assert result["pid"] == 9999
    assert bot_process._read_pid() == 9999


def test_start_does_not_spawn_a_second_process_when_already_running():
    already_running = {"running": True, "pid": 4242, "started_at": 1700000000.0}
    with patch("src.execution.bot_process.get_status", return_value=already_running), \
         patch("src.execution.bot_process.subprocess.Popen") as mock_popen:
        result = bot_process.start()

    mock_popen.assert_not_called()
    assert result == already_running


def test_stop_sends_graceful_signal_then_clears_pid_once_process_exits():
    bot_process._write_pid(4242)
    call_count = {"n": 0}

    def fake_is_our_bot_process(pid):
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
    assert not bot_process.PID_PATH.exists()


def test_stop_escalates_to_force_kill_if_process_never_exits():
    bot_process._write_pid(4242)

    with patch("src.execution.bot_process.os.kill"), \
         patch("src.execution.bot_process._is_our_bot_process", return_value=True), \
         patch("src.execution.bot_process.time.sleep"), \
         patch("src.execution.bot_process.time.time", side_effect=[0, 0, 100]), \
         patch("src.execution.bot_process.psutil.Process") as mock_process_cls:
        bot_process.stop()

    mock_process_cls.return_value.kill.assert_called_once()
    assert not bot_process.PID_PATH.exists()


def test_stop_when_not_running_is_a_noop():
    with patch("src.execution.bot_process.os.kill") as mock_kill:
        result = bot_process.stop()

    mock_kill.assert_not_called()
    assert result == {"running": False, "pid": None, "started_at": None}
