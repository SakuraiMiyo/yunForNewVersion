"""业务与安全边界：跨进程锁、单一使用令牌、闸门默认值。用独立 fixture 跑。"""
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import pytest

import main as M
from mcp_bridge import YunLocalBridge
from run_control import RegistryError, RunRegistry, file_mutex
from test_run_registry import run_fields


def make_project(path):
    (path / "runner").mkdir(parents=True, exist_ok=True)
    (path / "runner" / "main.py").write_text("# isolated placeholder")
    (path / "gps_track_lab" / "src").mkdir(parents=True, exist_ok=True)
    (path / "config.ini").write_text("[User]\ntoken = OFFLINE-ONLY\n")
    return path


def test_registry_denied_lock_does_not_redeem(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    registry = RunRegistry(path=tmp_path / "pending.json")
    token = registry.add(**run_fields(task)).token
    real_open = os.open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError("isolated lock failure")
        return real_open(path, *args, **kwargs)

    with patch("os.open", denied), pytest.raises(RegistryError):
        registry.pop(token)
    assert registry.get(token) is not None


def test_registry_failed_persistence_does_not_redeem(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    registry = RunRegistry(path=tmp_path / "pending.json")
    token = registry.add(**run_fields(task)).token
    with patch("run_control.os.replace", side_effect=PermissionError("isolated write failure")):
        with pytest.raises(RegistryError):
            registry.pop(token)
    assert registry.get(token) is not None


def test_stale_expiry_snapshot_cannot_erase_another_token(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    first = RunRegistry(path=tmp_path / "pending.json")
    old = first.add(**run_fields(task)).token
    first._runs[old].created_at = 0  # stale memory; authoritative disk is unchanged
    new = RunRegistry(path=first.path).add(**run_fields(task)).token
    assert new in {r.token for r in first.pending()}


def test_token_redemption_is_atomic_across_real_processes(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    registry = RunRegistry(path=tmp_path / "pending.json")
    token = registry.add(**run_fields(task)).token
    code = "from run_control import RunRegistry; import sys; print(int(RunRegistry(path=sys.argv[1]).pop(sys.argv[2]) is not None))"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(M.PROJECT_ROOT))
    processes = [subprocess.Popen([sys.executable, "-c", code, str(registry.path), token],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, err.decode(errors="replace")
        results.append(int(out.strip()))
    assert sum(results) == 1


def test_process_exit_releases_kernel_lock(tmp_path):
    lock = tmp_path / "worker.lock"
    code = "from pathlib import Path; from run_control import file_mutex; import os,sys; ctx=file_mutex(Path(sys.argv[1])); ctx.__enter__(); os._exit(0)"
    env = dict(os.environ, PYTHONPATH=str(Path(M.PROJECT_ROOT)))
    subprocess.run([sys.executable, "-c", code, str(lock)], env=env, check=True, timeout=15)
    with file_mutex(lock, timeout=0):
        pass


def test_bridge_refuses_stale_token_even_after_initial_lookup(tmp_path):
    project = tmp_path / "project"
    make_project(project)
    task = project / "tasks_sim" / "task.json"
    task.parent.mkdir()
    task.write_text("{}")
    bridge = YunLocalBridge(project, allow_submit=True, spawn_detached=False)
    run = bridge.runs.add(**run_fields(task))
    assert bridge.runs.pop(run.token) is not None
    bridge._execute_table = Mock(side_effect=AssertionError("must never execute"))
    result = bridge._run_guarded(run.token, run, dry_run=True, is_drift=False,
                                 timeout_s=10, progress_cb=None, abort_event=None)
    assert result["error"]["code"] == "unknown_token"
    bridge._execute_table.assert_not_called()


def test_shared_execution_lock_refuses_another_bridge(tmp_path):
    bridge = YunLocalBridge(make_project(tmp_path), allow_submit=True, spawn_detached=False)
    task = tmp_path / "task.json"
    task.write_text("{}")
    run = bridge.runs.add(**run_fields(task))
    bridge._run_claimed = Mock(side_effect=AssertionError("must never execute"))
    import hashlib
    key = hashlib.sha256(os.path.normcase(str(bridge.config_path.resolve())).encode()).hexdigest()[:24]
    with file_mutex(tmp_path / "tasks_sim" / f"execution.{key}.lock"):
        result = bridge._run_guarded(run.token, run, dry_run=True, is_drift=False,
                                     timeout_s=10, progress_cb=None, abort_event=None)
    assert result["error"]["code"] == "shared_execution_unavailable"
    bridge._run_claimed.assert_not_called()
    assert bridge.runs.get(run.token) is not None


def test_plugin_face_settings_survive_worker_environment(tmp_path):
    bridge = YunLocalBridge(make_project(tmp_path), allow_submit=True,
                            face_config={"photo": "photo.jpg", "detection": "det.json", "mirror": True})
    task = tmp_path / "task.json"
    task.write_text("{}")
    run = bridge.runs.add(**run_fields(task))
    worker = tmp_path.parent / "scripts" / "run_worker.py"
    worker.parent.mkdir(exist_ok=True)
    worker.write_text("# isolated stand-in")
    bridge._run_lock.acquire()
    state = {"token": run.token, "status": "running", "started_at": 0, "lines": []}
    with patch("subprocess.Popen") as spawn:
        spawn.return_value.pid = os.getpid()
        bridge._spawn_detached_run(run.token, run, state=state,
                                   run_lock=bridge._run_lock, state_lock=bridge._run_state_lock,
                                   dry_run=True, is_drift=False, timeout_s=10)
    env = spawn.call_args.kwargs["env"]
    assert env["YUN_FACE_PHOTO"] == str(tmp_path / "photo.jpg")
    assert env["YUN_FACE_DETECTION"] == str(tmp_path / "det.json")
    assert env["YUN_FACE_MIRROR"] == "1"


def test_timeout_keeps_mutex_until_upload_thread_stops(tmp_path):
    from test_background_run import ScriptedRunner
    import hashlib

    bridge = YunLocalBridge(make_project(tmp_path), allow_submit=True, spawn_detached=False)
    runner = ScriptedRunner(sleep_s=0.4, batches=4)
    bridge._runner_module = runner
    task = tmp_path / "task.json"
    task.write_text("{}")
    run = bridge.runs.add(**run_fields(task))
    result = bridge._run_guarded(run.token, run, dry_run=False, is_drift=False,
                                 timeout_s=0.02, progress_cb=None, abort_event=None)
    assert result["error"]["code"] == "timeout"
    key = hashlib.sha256(os.path.normcase(str(bridge.config_path.resolve())).encode()).hexdigest()[:24]
    lock = tmp_path / "tasks_sim" / f"execution.{key}.lock"
    with pytest.raises(RegistryError):
        with file_mutex(lock, timeout=0):
            pass
    bridge._execution_thread.join(timeout=3)
    assert not bridge._execution_thread.is_alive()
    # Reaper release can occur just after the worker exits.
    with file_mutex(lock, timeout=2):
        pass
    assert runner.sleep_calls < runner.batches


@pytest.mark.parametrize("status", ["N", "N0", "N1", "unexpected", None, []])
def test_face_admission_failure_never_sends_start(status):
    session = M.Yun_For_New.__new__(M.Yun_For_New)
    session.runFaceStatus, session.raRunArea = "Y", "TEST-AREA"
    session.client = Mock()
    session.client.post_json.return_value = {"code": 200, "data": {"runFaceStudentStatus": status}}
    with pytest.raises(M.FaceRequiredError):
        session.start()
    assert [call.args[0] for call in session.client.post_json.call_args_list] == ["/run/getRlStatus"]


@pytest.mark.parametrize("data", [None, [], {}, {"isStandard": "N", "list": []},
                                   {"isStandard": "Y", "isCheat": "Y", "list": []},
                                   {"isStandard": "Y", "list": False},
                                   {"isStandard": "Y", "list": [{"isStandard": "N"}]}])
def test_finish_refused_for_invalid_or_rejected_verdict(data):
    session = M.Yun_For_New.__new__(M.Yun_For_New)
    session._post_checked = Mock(return_value={"code": 200, "data": data})
    with pytest.raises(M.FaceRunStopError):
        session._finish_state_check({})
    assert session._post_checked.call_count == 1


def test_finish_state_url_is_display_only():
    session = M.Yun_For_New.__new__(M.Yun_For_New)
    session._post_checked = Mock(return_value={"code": 200, "data": {
        "isStandard": "Y", "url": "https://example.invalid/icon.png", "list": []}})
    assert session._finish_state_check({})["isStandard"] == "Y"


def test_console_rejects_cross_site_before_dispatch():
    from gps_track_lab import web

    class QuietHandler(web.TrackLabHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    origin = f"http://127.0.0.1:{server.server_port}"
    console = Mock()
    console.handle_post.return_value = (200, {"audit_only": True})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch.object(web, "console_instance", return_value=console):
        thread.start()
        try:
            for headers in [
                {"Host": "attacker.invalid", "Content-Type": "application/json"},
                {"Origin": "https://attacker.invalid", "Content-Type": "application/json"},
                {"Content-Type": "text/plain"},
                {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            ]:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("POST", "/api/console/run", body='{"confirmed":true}', headers=headers)
                response = connection.getresponse()
                assert response.status == 403
                response.read()
                connection.close()
            console.handle_post.assert_not_called()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/api/console/run", body='{"confirmed":true}',
                               headers={"Content-Type": "application/json", "Origin": origin})
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.close()
            console.handle_post.assert_called_once()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
