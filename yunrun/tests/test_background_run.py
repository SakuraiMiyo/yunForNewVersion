"""Tests for detached execution: start, observe, abort.

The behaviour under test exists because hosts cancel long tool calls. A common timeout kills a
local tool after 60 seconds (``tool yun_confirm_run execution timeout after 60 seconds``),
and a real record takes 16-25 minutes, so:

* a run must survive the call that started it, on its own thread;
* its progress must be readable while it is still going;
* it must be stoppable mid-flight, because "取消" arriving after the chain started is
  otherwise unanswerable;
* starting a second run must be refused with a reason, and must not deadlock -- the
  detached path takes the execution lock itself, so the worker must not try to take it
  again (that mistake reported "busy" for a run nobody else was doing).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "gps_track_lab" / "src"))

from mcp_bridge import RunAborted, YunLocalBridge  # noqa: E402


class FakeClient:
    """Counts the runner's waits; the abort check rides on exactly these calls."""

    def __init__(self) -> None:
        self.sleeps = 0

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        time.sleep(float(seconds))


class ScriptedRunner:
    """A stand-in for the runner module: reports a fixed tasklist, then simulates it."""

    def __init__(self, *, sleep_s: float = 0.05, batches: int = 4) -> None:
        self.sleep_s = sleep_s
        self.batches = batches
        self.sleep_calls = 0
        self.client = FakeClient()

    def set_args(self, path: str) -> None:
        pass

    def Yun_For_New(self, **kwargs):
        """Mimics the runner's session factory (the bridge calls it by name)."""
        return self._Session(self, kwargs.get("client"))

    def default_client(self):
        return self.client

    class _Session:
        def __init__(self, outer, client=None) -> None:
            self.outer = outer
            self.crsRunRecordId = 4242
            self._last_confirmed_mileage_m = 0.0
            self.client = client or outer.client

        # -- the three phases the bridge drives -------------------------------------
        def start(self) -> None:
            print("云运动任务创建成功！")

        def do_by_points_map(self, **kwargs) -> None:
            for index in range(self.outer.batches):
                if self.client is not None:
                    self.client.sleep(self.outer.sleep_s)
                self.outer.sleep_calls += 1
                print(f"已上传第 {index + 1} 批")

        def finish_by_points_map(self) -> None:
            print("[finish] 服务端已受理本次结束请求（code=200）。")


class BackgroundRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # A minimal project skeleton so the bridge's containment checks pass.
        (self.root / "gps_track_lab" / "src").mkdir(parents=True)
        (self.root / "runner").mkdir()
        (self.root / "runner" / "main.py").write_text("# placeholder", encoding="utf-8")
        (self.root / "config.ini").write_text("[User]\ntoken = t\n", encoding="utf-8")
        self.bridge = YunLocalBridge(self.root, allow_submit=True, spawn_detached=False)
        self.runner = ScriptedRunner()
        # _runner() imports the real module (yun_runner) and caches it; seed the cache so
        # the chain is driven by the scripted double instead of the network runner.
        self.bridge._runner_module = self.runner

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _prepare(self, **overrides):
        from run_control import PreparedRun

        task = self.root / "tasks_sim" / "tasklist_sim_test.json"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text("{}", encoding="utf-8")
        fields = dict(campus="tasks_else", task_file=str(task), card_png="",
                      distance_km=3.1, duration_s=1200.0, pace_min_per_km=6.4,
                      cadence=160, stride_m=1.0, point_count=500, qualified=True,
                      seed=1, created_at=time.time())
        fields.update(overrides)
        return self.bridge.runs.add(**fields)

    def _execute(self, token, **kwargs):
        """Drive _run_guarded directly with the scripted runner and the lock held."""
        run = self.bridge.runs.get(token)
        self.bridge._run_lock.acquire()
        try:
            return self.bridge._run_guarded(token, run, dry_run=False, is_drift=False,
                                            timeout_s=60.0,
                                            progress_cb=kwargs.pop("progress_cb", None),
                                            abort_event=kwargs.pop("abort_event", None))
        finally:
            self.bridge._run_lock.release()

    # ---------------------------------------------------- abort plumbing (跨进程)
    # 这两条钉住 detached 路径的可中止性：abort_run() 在别的进程里写
    # abort.<worker_pid>.flag，而 execute_run 正是 worker 里跑的入口。少了下面这条
    # 透传，abortable_sleep 看到 event 与 flag 都为 None 就退回裸 time.sleep，
    # 「中止」变成静默空操作——20 分钟的上传链照样跑到 finish。

    def test_execute_run_hands_the_abort_flag_to_the_chain(self) -> None:
        run = self._prepare()
        seen: dict = {}

        def fake(token, run_obj, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

        with mock.patch.object(self.bridge, "_run_guarded", side_effect=fake):
            result = self.bridge.execute_run(run.token, confirmed=True, publish_state=True)

        self.assertTrue(result["ok"])
        self.assertEqual(seen["abort_flag_path"], self.bridge._abort_flag_for())
        self.assertIs(seen["publish_state"], True)
        # 跑完必须清掉 flag，否则下一次运行会被上一次残留的中止信号直接打断
        self.assertFalse(seen["abort_flag_path"].exists())

    def test_execute_run_honours_a_flag_from_another_process(self) -> None:
        run = self._prepare()
        flag = self.bridge._abort_flag_for()
        original = self.bridge._run_guarded
        progress: list[str] = []

        def write_then_run(token, run_obj, **kwargs):
            # 模拟「别的进程在我已经开跑之后点了中止」。execute_run 起手会预清理 flag，
            # 所以只有在这一刻写才等价于跨进程中止（也顺带钉住预清理的语义）。
            flag.write_text(str(time.time()), encoding="utf-8")
            return original(token, run_obj, **kwargs)

        with mock.patch.object(self.bridge, "_run_guarded", side_effect=write_then_run):
            result = self.bridge.execute_run(run.token, confirmed=True,
                                             progress_cb=progress.append)

        self.assertFalse(result["ok"])
        self.assertIn("RunAborted",
                      json.dumps(result.get("error") or {}, ensure_ascii=False))
        # 停在批次之间：四批最多跑一两批，绝不能走到 finish
        self.assertLess(self.runner.sleep_calls, 4)
        self.assertEqual([line for line in progress if "finish" in line.lower()], [])

    # ------------------------------------------------------------------ gates

    def test_confirmation_is_required(self) -> None:
        run = self._prepare()
        result = self.bridge.start_background_run(run.token, confirmed=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "confirmation_required")

    def test_unknown_token_is_refused(self) -> None:
        result = self.bridge.start_background_run("nope", confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_token")

    def test_submit_disabled_is_refused(self) -> None:
        bridge = YunLocalBridge(self.root, allow_submit=False, spawn_detached=False)
        # The staged table has to exist: the registry forgets tokens whose table is gone,
        # which would otherwise surface as unknown_token instead of the gate under test.
        table = self.root / "t.json"
        table.write_text("{}", encoding="utf-8")
        run = bridge.runs.add(campus="tasks_else", task_file=str(table),
                             card_png="", distance_km=3.1, duration_s=1200.0,
                             pace_min_per_km=6.4, cadence=160, stride_m=1.0,
                             point_count=10, qualified=True, seed=1, created_at=time.time())
        result = bridge.start_background_run(run.token, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "submit_disabled")

    # ------------------------------------------------------------------ execution

    def test_run_completes_and_reports_batched_mode(self) -> None:
        run = self._prepare()
        result = self._execute(run.token)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "batched-table")
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["record_id"], 4242)
        self.assertTrue(any("打表" in line for line in result["progress"]))
        self.assertEqual(self.runner.sleep_calls, 4)   # every batch was uploaded
        # Token is single-use: it left the registry when the work started.
        self.assertIsNone(self.bridge.runs.get(run.token))

    def test_progress_callback_sees_each_batch(self) -> None:
        seen: list[str] = []
        run = self._prepare()
        self._execute(run.token, progress_cb=seen.append)
        self.assertGreaterEqual(len(seen), 6)
        self.assertTrue(any("已上传第 4 批" in line for line in seen))

    def test_abort_stops_before_finish(self) -> None:
        abort = threading.Event()
        seen: list[str] = []
        run = self._prepare()

        def watch(line: str) -> None:
            seen.append(line)
            if "已上传第 2 批" in line:
                abort.set()      # stop mid-flight, exactly like the console button

        result = self._execute(run.token, progress_cb=watch, abort_event=abort)
        self.assertFalse(result["ok"])
        self.assertIn("RunAborted", str(result["error"]["message"]))
        # Batch 2 was uploaded before the abort was requested; the next wait refuses to
        # wait, so batches 3-4 never go out and finish is never reached.
        self.assertEqual(self.runner.sleep_calls, 2)
        self.assertFalse(any("finish" in line for line in seen))

    def test_abort_before_start_never_uploads(self) -> None:
        abort = threading.Event()
        abort.set()
        run = self._prepare()
        result = self._execute(run.token, abort_event=abort)
        self.assertFalse(result["ok"])
        self.assertEqual(self.runner.sleep_calls, 0)


class RunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "gps_track_lab" / "src").mkdir(parents=True)
        (self.root / "runner").mkdir()
        (self.root / "runner" / "main.py").write_text("# placeholder", encoding="utf-8")
        (self.root / "config.ini").write_text("[User]\ntoken = t\n", encoding="utf-8")
        self.bridge = YunLocalBridge(self.root, allow_submit=True, spawn_detached=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_run_reports_inactive(self) -> None:
        activity = self.bridge.run_activity()
        self.assertTrue(activity["ok"])
        self.assertFalse(activity["active"])
        self.assertIsNone(self.bridge.run_state())

    def test_activity_is_json_safe(self) -> None:
        """Tool results must serialize: the live state holds a callable, the view must not."""
        import json

        with self.bridge._run_state_lock:
            self.bridge._run_state = {
                "status": "running", "token": "abc", "dry_run": True,
                "started_at": time.time(), "lines": ["a", "b"], "result": None,
            }
        activity = self.bridge.run_activity()
        json.dumps(activity)     # must not raise
        self.assertTrue(activity["active"])
        self.assertEqual(activity["recent_lines"], ["a", "b"])

    def test_second_run_is_refused_while_one_is_running(self) -> None:
        task = self.root / "t.json"
        task.write_text("{}", encoding="utf-8")
        first = self.bridge.runs.add(campus="tasks_else", task_file=str(task), card_png="",
                                     distance_km=3.1, duration_s=60.0, pace_min_per_km=6.0,
                                     cadence=160, stride_m=1.0, point_count=5,
                                     qualified=True, seed=1, created_at=time.time())
        # Pretend a run is in flight without starting a real thread.
        self.bridge._run_lock.acquire()
        try:
            self.bridge._run_state = {"status": "running", "token": first.token,
                                      "dry_run": False, "started_at": time.time(),
                                      "lines": [], "result": None}
            second = self.bridge.runs.add(campus="tasks_else", task_file=str(task), card_png="",
                                          distance_km=3.1, duration_s=60.0,
                                          pace_min_per_km=6.0, cadence=160, stride_m=1.0,
                                          point_count=5, qualified=True, seed=2,
                                          created_at=time.time())
            result = self.bridge.start_background_run(second.token, confirmed=True)
        finally:
            self.bridge._run_lock.release()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "already_running")
        self.assertIn(first.token, result["error"]["message"])

    def test_abort_without_a_run_is_a_no_op(self) -> None:
        result = self.bridge.abort_run()
        self.assertTrue(result["ok"])
        self.assertFalse(result["aborted"])

    def test_abort_sets_the_event_and_can_cancel_a_token(self) -> None:
        task = self.root / "t.json"
        task.write_text("{}", encoding="utf-8")
        pending = self.bridge.runs.add(campus="tasks_else", task_file=str(task), card_png="",
                                       distance_km=3.1, duration_s=60.0, pace_min_per_km=6.0,
                                       cadence=160, stride_m=1.0, point_count=5,
                                       qualified=True, seed=1, created_at=time.time())
        self.bridge._run_state = {"status": "running", "token": "live", "dry_run": False,
                                  "started_at": time.time(), "lines": [], "result": None}
        result = self.bridge.abort_run(pending.token)
        self.assertTrue(result["aborted"])
        self.assertTrue(self.bridge._abort_event.is_set())
        self.assertEqual(result["cancelled"], [pending.token])
        self.assertIsNone(self.bridge.runs.get(pending.token))


class SharedRunStateTests(unittest.TestCase):
    """Execution state must be visible to processes that did not start the run.

    Observed live: the bot's MCP server started a run, the plugin answered
    ``yun_run_status`` with ``active: false`` -- because the state lived in the MCP
    process's memory. The answer was honest and useless.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "gps_track_lab" / "src").mkdir(parents=True)
        (self.root / "runner").mkdir()
        (self.root / "runner" / "main.py").write_text("# placeholder", encoding="utf-8")
        (self.root / "config.ini").write_text("[User]\ntoken = t\n", encoding="utf-8")
        self.bridge = YunLocalBridge(self.root, allow_submit=True, spawn_detached=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _other(self) -> YunLocalBridge:
        """A second bridge = a second process asking about the same run."""
        return YunLocalBridge(self.root, allow_submit=True, spawn_detached=False)

    def test_state_written_by_one_bridge_is_readable_by_another(self) -> None:
        with self.bridge._run_state_lock:
            self.bridge._run_state = {
                "status": "running", "token": "abc", "dry_run": False,
                "started_at": time.time(), "lines": ["第一批已上传"], "result": None,
            }
            self.bridge._write_run_state_locked()

        other = self._other()
        activity = other.run_activity()
        self.assertTrue(activity["active"])
        self.assertEqual(activity["token"], "abc")
        self.assertTrue(activity["shared"])
        self.assertIn("第一批已上传", activity["recent_lines"])

    def test_dead_owner_is_not_reported_as_running(self) -> None:
        # A run whose process is gone must not claim progress: the user would wait forever.
        state_path = self.bridge.run_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "version": 1, "status": "running", "token": "abc", "dry_run": False,
            "started_at": time.time(), "pid": 999_999_999, "lines": [],
        }), encoding="utf-8")

        activity = self._other().run_activity()
        self.assertFalse(activity["active"])
        self.assertEqual(activity["status"], "lost")
        self.assertIn("发起进程已退出", activity["note"])

    def test_a_recorded_result_wins_over_the_liveness_probe(self) -> None:
        """Finished with a verdict, but its process is already gone -> done, not lost.

        The worker writes the result and exits in the same instant, so the pid probe can see a
        dead process while the file already says how it went.
        """
        state_path = self.bridge.run_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "version": 1, "status": "running", "token": "abc", "dry_run": True,
            "started_at": time.time(), "pid": 999_999_999,
            "finished_at": time.time(),
            "lines": ["done"],
            "result": {"ok": True, "record_id": 4242, "mode": "batched-table"},
        }), encoding="utf-8")

        activity = self._other().run_activity()
        self.assertEqual(activity["status"], "done")
        self.assertFalse(activity["active"])
        self.assertEqual((activity.get("result") or {}).get("record_id"), 4242)

    def test_finished_but_failing_result_is_failed_not_lost(self) -> None:
        state_path = self.bridge.run_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "version": 1, "status": "running", "token": "abc", "dry_run": False,
            "started_at": time.time(), "pid": 999_999_999,
            "lines": [],
            "result": {"ok": False, "error": {"code": "runner_failed", "message": "boom"}},
        }), encoding="utf-8")
        activity = self._other().run_activity()
        self.assertEqual(activity["status"], "failed")
        self.assertEqual((activity.get("result") or {}).get("error", {}).get("code"),
                         "runner_failed")

    def test_unfinished_run_with_a_dead_process_is_still_lost(self) -> None:
        """The ``lost`` verdict must survive the fix: no result and no process is not "running"."""
        state_path = self.bridge.run_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "version": 1, "status": "running", "token": "abc", "dry_run": False,
            "started_at": time.time(), "pid": 999_999_999, "lines": ["batch 3"],
        }), encoding="utf-8")
        activity = self._other().run_activity()
        self.assertEqual(activity["status"], "lost")
        self.assertFalse(activity["active"])
        self.assertIn("发起进程已退出", activity["note"])

    def test_abort_reaches_a_run_owned_by_another_process(self) -> None:
        state_path = self.bridge.run_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        owner_pid = 424242
        state_path.write_text(json.dumps({
            "version": 1, "status": "running", "token": "abc", "dry_run": False,
            "started_at": time.time(), "pid": owner_pid, "lines": [],
        }), encoding="utf-8")

        result = self._other().abort_run()
        self.assertTrue(result["ok"])
        self.assertTrue(result["aborted"])
        self.assertEqual(result["where"], f"pid:{owner_pid}")
        self.assertTrue(self.bridge._abort_flag_for(owner_pid).exists())

    def test_abort_flag_stops_the_sleep_loop(self) -> None:
        from mcp_bridge import RunAborted, _check_abort

        flag = self.bridge._abort_flag_for()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("stop", encoding="utf-8")
        with self.assertRaises(RunAborted):
            _check_abort(None, flag)

    def test_no_shared_state_means_inactive(self) -> None:
        activity = self._other().run_activity()
        self.assertFalse(activity["active"])
        self.assertIsNone(activity.get("status"))


class _FaceAwareRunner:
    """替身 runner：只回答"人脸接线"这一问题，其余走最小实现。

    记录 build_face_runner 的调用次数与收到的参数，返回一个带 SENTINEL 的对象，
    这样测试可以断言"builder 被调用了""参数就是配置里的路径"。
    ``builder`` 为 None 时返回默认哨兵；传自定义函数可模拟预检抛错。
    """

    def __init__(self, builder=None) -> None:
        self._builder = builder
        self.build_calls = 0
        self.last_face_args = None
        self.client = FakeClient()

    def set_args(self, path: str) -> None:
        pass

    def build_face_runner(self, args):
        self.build_calls += 1
        self.last_face_args = args
        if self._builder is not None:
            return self._builder(args)
        sentinel = type("BuiltFaceRunner", (), {})()
        sentinel.SENTINEL = True
        return sentinel

    def default_client(self):
        return self.client

    def Yun_For_New(self, **kwargs):
        return ScriptedRunner._Session(self, kwargs.get("client"))


class FaceSourceWiringTests(unittest.TestCase):
    """桥接的人脸源接线。

    为什么这层必须存在：任务 runFaceStatus=Y 时 runner 会要求 face_runner，拿不到就
    **直接停止**（runner/main.py:706-730）。本账号三个任务全是 Y，所以"通过宿主 /
    控制台真跑"这条路必须把照片源接进来，否则根本跑不了——而在此之前，
    `_execute_table` 建 session 时压根没传 face_runner。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "gps_track_lab" / "src").mkdir(parents=True)
        (self.root / "runner").mkdir()
        (self.root / "runner" / "main.py").write_text("# placeholder", encoding="utf-8")
        # 真实 runner 的 set_args 要读 [Yun] 段；这一组的人脸接线必须用真实 runner，
        # 所以配置得是最小可用集，不能只有一行 token。
        (self.root / "config.ini").write_text(
            "[Yun]\nyun_host = https://example.invalid\napp_edition = 3.6.6\n"
            "[User]\ntoken = t\nsys_edition = 16\n", encoding="utf-8")
        self._bridges: list[YunLocalBridge] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bridge(self, face_config=None, builder=None) -> YunLocalBridge:
        bridge = YunLocalBridge(self.root, allow_submit=True, spawn_detached=False,
                                face_config=face_config)
        # 用**能记录调用的替身**测接线，而不是真实 runner：真实 runner 会要求一整份
        # 最小可用的 config.ini（实测要 Yun/User 十几个键），而这里要验证的只有一件事——
        # build_face_runner 的结果有没有被交给 session。真实预检本身已由
        # scripts/retina_detect.py 与 tests/test_yun_face.py 覆盖。
        bridge._runner_module = _FaceAwareRunner(builder)
        self._bridges.append(bridge)
        return bridge

    def _face_args(self):
        """替身 builder 不读内容，只记录参数——路径存在即可。"""
        photo = self.root / "face.png"
        photo.write_bytes(b"placeholder")
        det = self.root / "face.json"
        det.write_text("{}", encoding="utf-8")
        return str(photo), str(det)

    def test_unconfigured_reports_not_ready(self) -> None:
        bridge = self._bridge(None)
        self.assertFalse(bridge.face_ready)
        status = bridge.face_status()
        self.assertFalse(status["configured"])
        self.assertIn("未配置", status["note"])

    def test_partial_config_is_not_ready(self) -> None:
        """只填照片不填标注 = 没配好：缺标注时检测结果无从谈起，不能放行。"""
        photo, _ = self._face_args()
        bridge = self._bridge({"photo": photo, "detection": ""})
        self.assertFalse(bridge.face_ready)
        self.assertIsNone(bridge._build_face_runner(bridge._runner(), bridge.face_args()))

    def test_complete_config_calls_the_builder(self) -> None:
        photo, det = self._face_args()
        bridge = self._bridge({"photo": photo, "detection": det})
        self.assertTrue(bridge.face_ready)
        self.assertTrue(bridge.face_status()["configured"])
        runner = bridge._runner()
        built = bridge._build_face_runner(runner, bridge.face_args())
        self.assertTrue(getattr(built, "SENTINEL", False), "builder 没有被调用")
        self.assertEqual(runner.build_calls, 1)

    def test_builder_argument_carries_the_configured_paths(self) -> None:
        """给 builder 的必须就是配置里的路径（否则会去比对另一张照片）。

        比较前用 os.path.realpath：Windows 会把临时目录里的用户名写成 8.3 短名
        `USERNA~1`，两种写法 `Path` 也不相等——实测踩过，realpath 才能展开成同一长名。
        """
        photo, det = self._face_args()
        bridge = self._bridge({"photo": photo, "detection": det, "mirror": True})
        runner = bridge._runner()
        bridge._build_face_runner(runner, bridge.face_args())
        args = runner.last_face_args
        self.assertEqual(os.path.realpath(args.face_photo), os.path.realpath(photo))
        self.assertEqual(os.path.realpath(args.face_detection), os.path.realpath(det))
        self.assertTrue(args.face_mirror)
        self.assertIsNone(args.face_video)

    def test_builder_failure_propagates_with_reason(self) -> None:
        """预检失败必须原样冒泡（带原因），不能被吞成"没有能力"。"""
        photo, det = self._face_args()

        def boom(_args):
            raise RuntimeError("照片源预检失败：xxx —— 未发出任何请求")

        bridge = self._bridge({"photo": photo, "detection": det}, builder=boom)
        with self.assertRaises(RuntimeError) as ctx:
            bridge._build_face_runner(bridge._runner(), bridge.face_args())
        self.assertIn("未发出任何请求", str(ctx.exception))

    def test_runner_without_the_capability_returns_none(self) -> None:
        """旧版/替身 runner 没有 build_face_runner 时按"无该能力"处理，不抛异常。"""
        photo, det = self._face_args()
        bridge = self._bridge({"photo": photo, "detection": det})
        bridge._runner_module = ScriptedRunner()
        self.assertIsNone(bridge._build_face_runner(bridge._runner(), bridge.face_args()))

    def test_env_variables_are_honoured(self) -> None:
        """没走插件配置时，环境变量也要能供上（MCP server 那条路）。"""
        photo, det = self._face_args()
        old = {k: os.environ.get(k) for k in ("YUN_FACE_PHOTO", "YUN_FACE_DETECTION")}
        os.environ["YUN_FACE_PHOTO"] = photo
        os.environ["YUN_FACE_DETECTION"] = det
        try:
            bridge = self._bridge(None)
            self.assertTrue(bridge.face_ready)
            # realpath：Windows 8.3 短名（USERNA~1）与长名是同一文件但字符串不等
            self.assertEqual(os.path.realpath(bridge.face_args()["photo"]),
                             os.path.realpath(photo))
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
