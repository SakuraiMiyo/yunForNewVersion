"""共享业务层：网页控制台、MCP server 与其它宿主共用同一份实现。

Two capability tiers, separated deliberately:

* **Local/simulation** -- dataset inspection, trajectory simulation, card rendering and
  the staged-run handshake (``prepare_run``). None of this touches the network.
* **Submission** -- ``execute_run`` imports the runner (``main.py``) and uploads a
  prepared table in batched mode. It is unreachable without a token minted by
  ``prepare_run`` *and* an explicit ``confirmed=True``, so no automated caller can
  submit a record on its own.
"""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import math
import os
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable

from run_control import DEFAULT_TTL_SECONDS, PreparedRun, RunRegistry

class RunAborted(Exception):    """Raised inside the runner's sleep when an abort was requested.

    Aborting has to interrupt a chain that spends most of its life sleeping between the
    batches of a 16-25 minute record, so the abort check rides on the sleep callable the
    runtime already threads through the client.
    """


class RunAlreadyActive(Exception):
    """Raised when a second run is started while one is still in flight.

    Distinct from a plain "busy" error because it carries the token of the run that holds
    the lock: an abandoned thread left over from a cancelled tool call is otherwise
    indistinguishable from a run the user actually asked for.
    """


CAMPUS_DIRS = {
    "fch": "tasks_fch",
    "txl": "tasks_txl",
    "xc": "tasks_xc",
    "else": "tasks_else",
    "tasks_fch": "tasks_fch",
    "tasks_txl": "tasks_txl",
    "tasks_xc": "tasks_xc",
    "tasks_else": "tasks_else",
}


def _pid_alive(pid: Any) -> bool:
    """True when a process with that id is still running.

    Used to tell "this run is still uploading" apart from "the process that owned it is
    gone" -- the second case must never be reported as progress.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so a run result can be written to the shared state file."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _check_abort(event: threading.Event | None, flag_path: Path | None) -> None:
    """Raise :class:`RunAborted` when either abort channel says to stop."""
    if event is not None and event.is_set():
        raise RunAborted("已按请求中止（未执行 finish，记录不会提交完整成绩）")
    if flag_path is not None and flag_path.exists():
        raise RunAborted("已按请求中止（另一个进程请求停止；未执行 finish）")

# Table holding generated runs, kept apart from the real capture directories.
STAGING_DIR = "tasks_sim"
# How close a generated trajectory has to pass to each of the task's checkpoints. On the
# historical same-field records every checkpoint sits 0.4-4.5 m from the actual path, so 25 m leaves
# generous room for a differently-shaped loop while still rejecting another stadium (nearest
# other field is ~950 m away).
CHECKPOINT_TOLERANCE_M = 25.0
# Shared token store, written next to the staged tables so every entry point (plugin, MCP
# server, web console) can redeem a token minted by any of the others.
REGISTRY_FILE = "pending_runs.json"
# Live state of the run in flight. Also shared, for the same reason: the process that
# starts a run is often not the process that later asks about it (the bot's MCP server
# starts it, the plugin answers yun_run_status), and a run that cannot be observed is a run
# nobody can stop or report on.
RUN_STATE_FILE = "active_run.json"
# Per-run stdout log, so a long run can be tailable instead of being trapped in a buffer
# that only the owning process can read.
RUN_LOG_PREFIX = "run_"
# Per-run sidecar naming, written next to the card for the web server to read back.
CARD_PLAN_FILE = "card_run_plan.json"
# Retention: how many generated artefacts to keep. Every entry point prunes before it
# writes, so a long-running bot cannot fill the disk with old trajectories.
DEFAULT_KEEP_RUNS = 10
DEFAULT_KEEP_TASKLISTS = 10

# 卡片底图策略：'full' 带高德底图，'none' 只有渐变背景 + 轨迹线。
# 为什么要有这个开关：同一个账号会出现在不同群里，底图会把田径场的位置画出来，
# 所以只有指定群与私聊才发带底图的卡片，其余群发"空底图"版本（轨迹照画）。
BASEMAP_MODES = ("full", "none")


class EmptyBasemap:
    """不取任何瓦片的底图：让卡片的 `.map` 只剩自身的深色渐变。

    接口与 ``track_card.basemap.Basemap`` 的 ``mosaic`` 保持兼容即可——卡片模板对每个
    返回 ``None`` 的格子渲染一个透明占位块，于是底图消失，而轨迹 SVG、起终点标记、
    顶部标题和信息面板都不受影响。
    """

    provider = "none"
    downloads = 0
    cache_hits = 0

    def __init__(self) -> None:
        self.failures: list[str] = []

    def mosaic(self, tile_range: Any) -> list[list[tuple[None, None]]]:
        columns = tile_range.x_max - tile_range.x_min + 1
        rows = tile_range.y_max - tile_range.y_min + 1
        return [[(None, None) for _ in range(columns)] for _ in range(rows)]



class _TeeLog(io.StringIO):
    """A stdout sink that keeps the full text *and* forwards lines as they are written.

    The runner reports progress by printing (one line per uploaded batch, plus the
    status and face notes), and the whole chain used to be captured in a StringIO that
    nobody could read until the process finished -- which is useless for following a
    run that takes twenty minutes. Wrapping the sink lets a live UI stream those lines
    while the buffered copy is still returned at the end.
    """

    def __init__(self, on_line: "Callable[[str], None] | None" = None) -> None:
        super().__init__()
        self._on_line = on_line
        self._partial = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:  # type: ignore[override]
        written = super().write(text)
        if self._on_line is None or not text:
            return written
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                line = line.rstrip()
                if line:
                    try:
                        self._on_line(line)
                    except Exception:  # noqa: BLE001 - an observer must never break a run
                        pass
        return written

    def flush(self) -> None:  # type: ignore[override]
        super().flush()
        # A trailing line without a newline would otherwise be dropped until the end.
        with self._lock:
            if self._partial.strip() and self._on_line is not None:
                line, self._partial = self._partial.strip(), ""
                try:
                    self._on_line(line)
                except Exception:  # noqa: BLE001
                    pass


class YunLocalBridge:
    """Dataset inspection, trajectory simulation, staged-run handshake and submission.

    ``allow_submit`` defaults to False so an integration that only wants the read-only /
    simulation surface cannot accidentally reach the upload path. Host integrations pass
    True explicitly.
    """

    def __init__(
        self,
        project_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        simulation_root: str | Path | None = None,
        *,
        allow_submit: bool = False,
        run_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        keep_runs: int = DEFAULT_KEEP_RUNS,
        keep_tasklists: int = DEFAULT_KEEP_TASKLISTS,
        allow_external_simulation_root: bool = False,
        registry_file: str | Path | None = None,
        spawn_detached: bool = True,
        face_config: dict[str, Any] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir or Path(__file__).parent).resolve()
        # The runner lives in runner/ after the layout cleanup; accept either place so an
        # older checkout still loads.
        self.runner_dir = self.project_dir / "runner"
        if not (self.runner_dir / "main.py").is_file():
            if (self.project_dir / "main.py").is_file():
                self.runner_dir = self.project_dir
            else:
                raise FileNotFoundError(
                    f"project root does not contain the runner (runner/main.py): {self.project_dir}")
        self.config_path = self._contained(config_path or self.project_dir / "config.ini", must_exist=True)
        self.lab_root = self._contained(self.project_dir / "gps_track_lab", must_exist=True)
        # The simulation root is the one path allowed outside the project, and only on an
        # explicit opt-in: a project kept inside OneDrive dehydrates generated files to
        # the cloud, after which the card renderer can no longer read them back.
        self.external_simulation_root = bool(allow_external_simulation_root)
        root = Path(simulation_root) if simulation_root else self.lab_root / "runs"
        self.simulation_root = (root.resolve() if self.external_simulation_root
                                else self._contained(root, must_exist=False))
        self.staging_root = self._contained(self.project_dir / STAGING_DIR, must_exist=False)
        lab_source = str(self.lab_root / "src")
        if lab_source not in sys.path:
            sys.path.insert(0, lab_source)
        # Generator tooling lives at the project root after the layout cleanup; keep the
        # old in-lab location on the path too so an older checkout still works.
        for tools_path in (str(self.project_dir / "tools"), str(self.lab_root / "tools")):
            if os.path.isdir(tools_path) and tools_path not in sys.path:
                sys.path.insert(0, tools_path)
        self._generation_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.allow_submit = bool(allow_submit)
        # Shared token store: the plugin, the MCP server and the web console are separate
        # processes, and a token minted by one has to be redeemable by the others. Keeping
        # it beside the staged tables also means a plugin reload no longer discards it.
        registry_path = Path(registry_file) if registry_file else self.staging_root / REGISTRY_FILE
        self.registry_path = registry_path
        self.run_state_path = self.staging_root / RUN_STATE_FILE
        # A run takes 16-25 minutes and its host (a robot framework, or the agent) can restart inside
        # that window; a thread would die with the host and leave a half-submitted record.
        # Detached means "own process, own lifetime".
        self.spawn_detached = bool(spawn_detached)
        # ------------------------------------------------------------------ 人脸源
        # 为什么必须在这里接：任务 runFaceStatus=Y 时 runner 会要求 face_runner，拿不到
        # 就**直接停止**（runner/main.py:706-730）。所以"通过宿主/控制台真跑"这条路
        # 必须把照片源接进来，否则本账号（三个任务全是 Y）根本跑不了。
        # 取值优先级：显式传入 > 环境变量 YUN_FACE_PHOTO / YUN_FACE_DETECTION / YUN_FACE_MIRROR。
        env_photo = (os.environ.get("YUN_FACE_PHOTO") or "").strip()
        env_det = (os.environ.get("YUN_FACE_DETECTION") or "").strip()
        env_mirror = (os.environ.get("YUN_FACE_MIRROR") or "").strip().lower() in (
            "1", "true", "yes", "y", "on")
        cfg = dict(face_config or {})
        def face_path(value):
            value = str(value or "").strip()
            if not value:
                return ""
            path = Path(value).expanduser()
            return str((path if path.is_absolute() else self.project_dir / path).resolve())

        self.face_photo = face_path(cfg.get("photo") or env_photo)
        self.face_detection = face_path(cfg.get("detection") or env_det)
        self.face_mirror = bool(cfg.get("mirror", env_mirror))
        # 只做"配置齐全性"检查，不做预检：预检要读文件、跑质量门，放在真正执行前的
        # worker 里做（失败会被明确报出，且那时才需要付出代价）。
        self.face_ready = bool(self.face_photo and self.face_detection)
        self.runs = RunRegistry(ttl_seconds=run_ttl_seconds, path=registry_path)
        self._runner_module = None
        if int(keep_runs) < 0 or int(keep_tasklists) < 0:
            raise ValueError("retention limits must be >= 0")
        self.keep_runs = int(keep_runs)
        self.keep_tasklists = int(keep_tasklists)
        # State of the run currently executing, if any. A run outlives the call that
        # started it (the host may cancel a tool call while the chain keeps going), so the
        # progress has to live somewhere the caller can come back and ask.
        self._run_state: dict[str, Any] | None = None
        self._run_state_lock = threading.Lock()
        self._abort_event = threading.Event()

    # ------------------------------------------------------------------ retention

    def prune(self, keep_runs: int | None = None,
              keep_tasklists: int | None = None) -> dict[str, Any]:
        """Drop everything but the newest N runs and staged tables.

        Called before every generation so the caches stay bounded rather than being
        trimmed only when they are already over the limit.
        """
        from gps_track_lab.retention import prune_run_logs, prune_runs, prune_tasklists

        limit_tasks = self.keep_tasklists if keep_tasklists is None else int(keep_tasklists)
        runs_report = prune_runs(self.simulation_root,
                                 self.keep_runs if keep_runs is None else int(keep_runs))
        tasks_report = prune_tasklists(self.staging_root, limit_tasks)
        logs_report = prune_run_logs(self.staging_root, limit_tasks)
        return {
            "runs": runs_report.as_dict(),
            "tasklists": tasks_report.as_dict(),
            "logs": logs_report.as_dict(),
        }

    def retention_status(self) -> dict[str, Any]:
        from gps_track_lab.retention import leftover_count, run_count, tasklist_count

        return {
            "keep_runs": self.keep_runs,
            "keep_tasklists": self.keep_tasklists,
            "runs_now": run_count(self.simulation_root),
            "tasklists_now": tasklist_count(self.staging_root),
            # Directories left behind by OneDrive hold no run and cannot be deleted
            # locally; reporting them separately keeps "10/10" honest.
            "leftover_dirs": leftover_count(self.simulation_root),
        }


    def _contained(self, path: str | Path, *, must_exist: bool) -> Path:
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(self.project_dir)
        except ValueError as exc:
            raise ValueError(f"path must stay inside project root: {candidate}") from exc
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    @staticmethod
    def _ok(**data: Any) -> dict[str, Any]:
        return {"ok": True, **data}

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    @staticmethod
    def _finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a number")
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return number

    def _config(self) -> configparser.ConfigParser:
        config = configparser.ConfigParser(interpolation=None)
        if not config.read(self.config_path, encoding="utf-8-sig"):
            raise FileNotFoundError(self.config_path)
        return config

    def _campus_path(self, campus: str) -> tuple[str, Path]:
        key = str(campus or "else").strip().lower()
        directory = CAMPUS_DIRS.get(key)
        if directory is None:
            raise ValueError(f"unsupported campus: {campus}")
        path = self._contained(self.project_dir / directory, must_exist=True)
        if not path.is_dir():
            raise NotADirectoryError(path)
        return directory, path

    def status(self) -> dict[str, Any]:
        try:
            config = self._config()
            token = config.get("User", "token", fallback="").strip()
            device_id = config.get("User", "device_id", fallback="").strip()
            return self._ok(
                mode="local-only" if not self.allow_submit else "staged-submit",
                network_submission_available=self.allow_submit,
                allow_submit=self.allow_submit,
                project_dir=str(self.project_dir),
                config_path=str(self.config_path),
                registry_path=str(self.registry_path),
                school_host=config.get("Yun", "school_host", fallback=""),
                app_edition=config.get("Yun", "app_edition", fallback=""),
                platform=config.get("Yun", "platform", fallback=""),
                has_token=bool(token),
                token_length=len(token),
                has_device_id=bool(device_id),
                device_name=config.get("User", "device_name", fallback=""),
                min_distance_km=config.getfloat("Run", "min_distance", fallback=0.0),
                simulation_root=str(self.simulation_root),
                retention=self.retention_status(),
                face=self.face_status(),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error("status_failed", str(exc))

    def list_tasks(self, campus: str = "else") -> dict[str, Any]:
        try:
            directory, path = self._campus_path(campus)
            tasks = []
            for source in sorted(path.glob("*.json")):
                try:
                    with source.open("r", encoding="utf-8") as handle:
                        data = json.load(handle).get("data", {})
                    tasks.append({
                        "file": source.name,
                        "record_mileage_km": data.get("recordMileage"),
                        "duration_seconds": data.get("duration"),
                        "pace_min_per_km": data.get("recodePace"),
                        "cadence": data.get("recodeCadence"),
                        "qualified": data.get("isQualified"),
                        "points": len(data.get("pointsList", [])),
                    })
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    tasks.append({"file": source.name, "error": str(exc)})
            return self._ok(campus=directory, count=len(tasks), tasks=tasks)
        except Exception as exc:  # noqa: BLE001
            return self._error("list_tasks_failed", str(exc))

    def analyze_tasks(
        self,
        campus: str = "else",
        rule_min_km: float = 3.0,
        rule_max_minutes: float = 30.0,
    ) -> dict[str, Any]:
        try:
            minimum = self._finite_number(rule_min_km, "rule_min_km", 0.0, 100.0)
            maximum = self._finite_number(rule_max_minutes, "rule_max_minutes", 0.1, 600.0)
            directory, path = self._campus_path(campus)
            from gps_track_lab.dataset import analyze_task_exports

            report = analyze_task_exports([path], minimum, maximum)
            return self._ok(campus=directory, report=report)
        except Exception as exc:  # noqa: BLE001
            return self._error("analyze_tasks_failed", str(exc))

    def calibration_status(self) -> dict[str, Any]:
        try:
            from gps_track_lab.calibration import load_calibration

            calibration_path = self._contained(self.lab_root / "config" / "track.json", must_exist=True)
            calibration = load_calibration(calibration_path)
            return self._ok(calibration_path=str(calibration_path), calibration=calibration.to_dict())
        except Exception as exc:  # noqa: BLE001
            return self._error("calibration_failed", str(exc))

    def generate_track(
        self,
        distance_km: float = 3.05,
        pace_min_per_km: float = 6.0,
        candidates: int = 8,
        seed: int | None = None,
    ) -> dict[str, Any]:
        if not self._generation_lock.acquire(blocking=False):
            return self._error("busy", "another local simulation is already running")
        try:
            distance = self._finite_number(distance_km, "distance_km", 0.1, 50.0)
            pace = self._finite_number(pace_min_per_km, "pace_min_per_km", 2.0, 30.0)
            if isinstance(candidates, bool) or int(candidates) != candidates or not 1 <= int(candidates) <= 100:
                raise ValueError("candidates must be an integer between 1 and 100")
            candidate_count = int(candidates)
            if seed is not None:
                if isinstance(seed, bool) or int(seed) != seed or not 0 <= int(seed) < 2**63:
                    raise ValueError("seed must be an integer between 0 and 2^63-1")
                seed = int(seed)

            from gps_track_lab.calibration import load_calibration
            from gps_track_lab.generator import generate_best_candidate, load_settings, write_outputs

            calibration = load_calibration(self.lab_root / "config" / "track.json")
            settings = load_settings(self.lab_root / "config.example.json")
            points, selected_seed, quality = generate_best_candidate(
                calibration=calibration,
                settings=settings,
                distance_m=distance * 1000.0,
                pace_min_per_km=pace,
                candidates=candidate_count,
                seed=seed,
            )
            self.prune()          # retention first, so the cache stays bounded
            output = write_outputs(
                points,
                self.simulation_root,
                selected_seed,
                calibration,
                settings,
                quality=quality,
            )
            with (output / "manifest.json").open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            return self._ok(
                local_only=True,
                synthetic=True,
                run_id=output.name,
                output_dir=str(output),
                point_count=manifest.get("point_count"),
                modeled_distance_m=manifest.get("modeled_distance_m"),
                duration_seconds=manifest.get("duration_seconds"),
                seed=manifest.get("seed"),
                quality=manifest.get("quality", {}),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error("generate_failed", str(exc))
        finally:
            self._generation_lock.release()

    def list_simulations(self, limit: int = 10) -> dict[str, Any]:
        try:
            if isinstance(limit, bool) or int(limit) != limit or not 1 <= int(limit) <= 100:
                raise ValueError("limit must be an integer between 1 and 100")
            if not self.simulation_root.exists():
                return self._ok(count=0, simulations=[])
            directories = sorted(
                (
                    path for path in self.simulation_root.iterdir()
                    if path.is_dir() and path.name.startswith("synthetic-")
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            simulations = []
            for directory in directories[: int(limit)]:
                try:
                    with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    quality = manifest.get("quality", {})
                    simulations.append({
                        "run_id": directory.name,
                        "point_count": manifest.get("point_count"),
                        "distance_m": manifest.get("modeled_distance_m"),
                        "duration_seconds": manifest.get("duration_seconds"),
                        "valid": quality.get("valid"),
                        "score": quality.get("score"),
                        # 业务口径（>3km 且 ≤30min）——与卡片上的「合格」同一来源；
                        # quality.valid 是物理校验（其加速度上限仍按旧匀速模型设定，
                        # 走跑分段模型下恒为 False），只作诊断不再用于界面判定。
                        "qualified": (quality.get("run_plan") or {}).get("qualified"),
                    })
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            return self._ok(count=len(simulations), simulations=simulations)
        except Exception as exc:  # noqa: BLE001
            return self._error("list_simulations_failed", str(exc))

    def simulation_detail(self, run_id: str) -> dict[str, Any]:
        try:
            name = str(run_id or "")
            if name != Path(name).name or not name.startswith("synthetic-"):
                raise ValueError("invalid run_id")
            directory = self._contained(self.simulation_root / name, must_exist=True)
            if not directory.is_dir():
                raise FileNotFoundError(name)
            with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            return self._ok(run_id=name, output_dir=str(directory), manifest=manifest)
        except Exception as exc:  # noqa: BLE001
            return self._error("simulation_detail_failed", str(exc))

    # ------------------------------------------------------ card rendering

    def _card_module(self):
        if str(self.project_dir) not in sys.path:
            sys.path.insert(0, str(self.project_dir))
        import track_card

        return track_card

    def _resolve_basemap(self, mode: Any) -> Any:
        """把 'full' / 'none' 解析成底图对象。

        未知值一律按 'full' 处理：宁可多发底图（用户看得见的那一种），也不因为一个
        拼错的参数把卡片悄悄变成空底——那样问题是静默的，很难发现。
        """
        choice = str(mode or "full").strip().lower()
        if choice == "none":
            return EmptyBasemap()
        from track_card.basemap import Basemap

        return Basemap()

    def card_from_run(self, run_id: str, basemap_mode: str = "full") -> dict[str, Any]:
        """Render the share card for a simulated run.

        basemap_mode: 'full' 带高德底图；'none' 只有深色渐变 + 轨迹线（其他群用这个）。
        """
        try:
            name = str(run_id or "")
            if name != Path(name).name or not name.startswith("synthetic-"):
                raise ValueError("invalid run_id")
            directory = self._contained(self.simulation_root / name, must_exist=True)
            with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            import csv

            with (directory / "track.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            if not rows:
                raise ValueError("run has no points")

            points = [(float(row["longitude"]), float(row["latitude"])) for row in rows]
            distance_m = float(rows[-1]["modeled_distance_m"])
            plan = dict((manifest.get("quality") or {}).get("run_plan") or {})
            sidecar = directory / CARD_PLAN_FILE
            if sidecar.is_file():
                plan.update(json.loads(sidecar.read_text(encoding="utf-8")))
            calibration = manifest.get("calibration") or {}
            distance_km = distance_m / 1000.0
            duration_s = float(manifest.get("duration_seconds") or 0.0)
            lap_length = calibration.get("lap_length_m")
            cadence = plan.get("cadence")
            steps = plan.get("step_total") or (
                int(round(float(cadence) * duration_s / 60.0)) if cadence else None)

            card = self._card_module()
            # Pace trace for the card's chart: the CSV carries the simulated speed per point,
            # and how that speed moves is the one thing the summary numbers cannot show.
            pace_trace: list[float] = []
            recorded = (manifest.get("quality") or {}).get("pace_trace") or []
            for value in recorded:
                try:
                    pace_trace.append(round(float(value), 3))
                except (TypeError, ValueError):
                    continue
            if len(pace_trace) < 4:
                # Fallback for runs made before the trace was recorded: derive it from the
                # CSV, which is noisier but better than no chart at all.
                pace_trace = []
                for row in rows:
                    try:
                        speed = abs(float(row.get("modeled_speed_mps") or 0.0))
                    except (TypeError, ValueError):
                        continue
                    if speed > 0.2:
                        pace_trace.append(round(1000.0 / (60.0 * speed), 3))
            data = card.CardData(
                points=points,
                subtitle=name,
                distance_km=distance_km,
                duration_s=duration_s,
                pace_min_per_km=(duration_s / 60.0) / distance_km if distance_km else None,
                cadence=cadence,
                stride_m=plan.get("stride_m") or (distance_m / steps if steps else None),
                step_total=steps,
                laps=(distance_m / float(lap_length)) if lap_length else None,
                lap_length_m=float(lap_length) if lap_length else None,
                seed=manifest.get("seed"),
                qualified=plan.get("qualified"),
                labels=["本地仿真 · 未上传"],
                pace_trace=pace_trace,
                started_at=str(rows[0].get("timestamp", ""))[:16].replace("T", " "),
            )
            output = directory / "card.png"
            meta = card.render_card(data, output, basemap=self._resolve_basemap(basemap_mode))
            meta["run_id"] = name
            meta["png"] = str(output)
            return self._ok(**meta)
        except Exception as exc:  # noqa: BLE001
            return self._error("card_failed", str(exc))

    # ------------------------------------------------------ stadium lock

    def stadium_status(self) -> dict[str, Any]:
        """Which running task (stadium) a run would be submitted to, and why.

        Read-only: ``/run/getHomeRunInfo`` creates nothing. The stadium is decided geometrically
        (the task whose fence contains the calibration centre), because task ids and list order
        both change between semesters (observed: the same field got a different id each term).
        """
        try:
            runner = self._runner()
            runner.set_args(str(self.config_path))
            client = runner.default_client()
            obj = client.post_json("/run/getHomeRunInfo", "", raise_on_business_code=False)
            cralist = ((obj.get("data") or {}).get("cralist") or [])
            if not cralist:
                return self._error("no_tasks", "服务端没有下发任何跑步任务（cralist 为空）")
            task, why = self._resolve_task(runner, cralist)
            return self._ok(
                task_id=task.get("id"),
                stadium_name=task.get("raName"),
                ra_run_area=task.get("raRunArea"),
                reason=why,
                fence_points=len(runner._fence_points(task)),
                task_count=len(cralist),
                calibration=runner._load_track_center(),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error("stadium_unresolved", str(exc))

    def _resolve_task(self, runner, cralist):
        """Pick the task, honouring ``YUN_TASK_ID`` when the list cannot be trusted/read.

        The override exists for offline work: with the task list unreachable there is nothing to
        match against, so pinning the id explicitly beats guessing.
        """
        override = str(os.environ.get("YUN_TASK_ID", "")).strip()
        if override:
            for task in cralist:
                if str(task.get("id")) == override:
                    return task, f"按环境变量 YUN_TASK_ID={override} 指定"
            raise RuntimeError(
                f"YUN_TASK_ID={override} 不在服务端任务列表里："
                + ", ".join(str(t.get("id")) for t in cralist))
        return runner.select_task(cralist, runner._load_track_center())

    def _fence_check(self, points, *, required: bool = True) -> dict[str, Any]:
        """Verify the trajectory lies inside the selected stadium's fence.

        The trajectory is generated around ``track.json``; the task decides where the record is
        filed. If those disagree the run is silently attached to another stadium -- which is what
        happened twice -- so the generated points are checked against the fence before staging.
        """
        try:
            runner = self._runner()
            runner.set_args(str(self.config_path))
            client = runner.default_client()
            obj = client.post_json("/run/getHomeRunInfo", "", raise_on_business_code=False)
            cralist = ((obj.get("data") or {}).get("cralist") or [])
            if not cralist:
                raise RuntimeError("服务端没有下发任何跑步任务（cralist 为空）")
            task, why = self._resolve_task(runner, cralist)
        except Exception as exc:  # noqa: BLE001
            message = f"无法读取任务列表以核对场地：{type(exc).__name__}: {exc}"
            if required:
                return self._error("fence_check_failed", message)
            # Generation is local; an unreachable server must not stop it. The run is marked
            # unverified and execute_run refuses until the check can actually run.
            return self._ok(verified=False, task_id=None, stadium_name=None, note=message)
        checkpoints = runner._fence_points(task)
        if not checkpoints or not points:
            return self._error("checkpoints_missing",
                               "服务端未下发打卡点坐标或轨迹为空，无法核对场地")
        # Distance from each checkpoint to the nearest trajectory point. The historical records
        # satisfy this within a few metres, so anything beyond the tolerance means the trajectory
        # was built for a different stadium.
        gaps = []
        for lon, lat in checkpoints:
            nearest = min(
                math.hypot((x - lon) * 111320.0 * math.cos(math.radians(lat)),
                           (y - lat) * 111320.0)
                for x, y in points)
            gaps.append((round(nearest, 1), f"{lon:.6f},{lat:.6f}"))
        worst_gap, worst_at = max(gaps)
        if worst_gap > CHECKPOINT_TOLERANCE_M:
            return self._error(
                "checkpoints_not_reached",
                f"轨迹没有经过所选场地（{task.get('raName')}）的打卡点："
                f"最远一个距离轨迹 {worst_gap:.0f} m（上限 {CHECKPOINT_TOLERANCE_M:.0f} m），"
                f"位置 {worst_at}。轨迹是按项目标定生成的，与任务场地不符，"
                "已停止（不会创建记录）。各打卡点距离: "
                + ", ".join(f"{gap:.0f}m" for gap, _ in gaps))
        return self._ok(
            verified=True,
            task_id=task.get("id"),
            stadium_name=task.get("raName"),
            reason=why,
            checked_points=len(points),
            checkpoint_gaps_m=[gap for gap, _ in gaps],
            worst_gap_m=worst_gap,
            # 人脸开关随任务下发（SportRunMapActivity.java:4314）：准备阶段就带给
            # 调用方做"开头提醒"；执行时 runner 仍会自己重新核对，不信任这里的缓存。
            face_status=str(task.get("runFaceStatus") or "").strip().upper(),
        )

    # ------------------------------------------------------ staged runs

    def _runner(self):
        """Import the runner (``runner/main.py``) once, by path.

        Loaded under a private module name so it cannot shadow a caller's ``main``. The
        runner sets global state at import time (it chdirs to the project root and parses
        its own CLI), so this happens once per bridge instance and only for a submission.
        """
        if self._runner_module is not None:
            return self._runner_module
        source = self.runner_dir / "main.py"
        # The runner imports its siblings (yun_http, yun_face, tools.*) by bare name.
        for extra in (str(self.runner_dir), str(self.runner_dir / "tools")):
            if extra not in sys.path:
                sys.path.insert(0, extra)
        spec = importlib.util.spec_from_file_location("yun_runner", source)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load runner: {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["yun_runner"] = module
        cwd = Path.cwd()
        try:
            spec.loader.exec_module(module)
        finally:
            import os

            try:
                os.chdir(cwd)      # the runner chdirs on import; put the caller's back
            except OSError:
                pass
        self._runner_module = module
        return module

    def _school_id(self) -> int:
        try:
            return int(self._config().get("Yun", "school_id", fallback="0"))
        except ValueError:
            return 0

    def task_rules(self) -> dict[str, Any]:
        """Read the account's current run tasks with a read-only request.

        ``/run/getHomeRunInfo`` is a pure read: it creates no record and changes no state.
        It is the only way to see, before committing to a run, whether the task needs face
        verification and what distance/cadence band the school enforces.
        """
        try:
            runner = self._runner()
            runner.set_args(str(self.config_path))
            client = runner.default_client()
            obj = client.post_json("/run/getHomeRunInfo", "")
            if obj.get("code") != 200:
                return self._error("home_info_failed",
                                   f"code={obj.get('code')} msg={obj.get('msg')}")
            cralist = (obj.get("data") or {}).get("cralist") or []
            tasks = []
            for item in cralist:
                status = str(item.get("runFaceStatus", "")).strip().upper()
                fence = str(item.get("points") or "")
                tasks.append({
                    "id": item.get("id"),
                    # The readable name is the only field that tells a human which field this is;
                    # ids change between semesters (observed: the same field got a different id each term).
                    "ra_name": item.get("raName"),
                    "ra_run_area": item.get("raRunArea"),
                    "ra_type": item.get("raType"),
                    "min_km": item.get("raSingleMileageMin"),
                    "max_km": item.get("raSingleMileageMax"),
                    "cadence_min": item.get("raCadenceMin"),
                    "cadence_max": item.get("raCadenceMax"),
                    "dislikes": item.get("raDislikes"),
                    "face_status": status or "(字段缺失 = 未启用)",
                    "face_required": status == "Y",
                    "fence_points": len(fence.split("|")) if fence else 0,
                })

            # Which task a run would actually use. The runner is locked to the stadium the
            # calibration was fitted to, so this is the answer the caller needs -- and when
            # nothing matches, the run cannot proceed, which is worth knowing before staging
            # anything rather than discovering it at submission time.
            selected = None
            selection_note = ""
            selection_error = None
            try:
                calibration = runner._load_track_center()
                task, why = runner.select_task(cralist, calibration)
                selected = task.get("id")
                selection_note = why
            except Exception as exc:  # noqa: BLE001 - a read-only view must not raise
                selection_error = str(exc)

            return self._ok(
                count=len(tasks), tasks=tasks,
                selected_task=selected,
                selection_note=selection_note,
                selection_error=selection_error,
                note=("只读请求，未创建任何跑步记录。轨迹围绕项目标定生成，"
                      "执行时会锁定到围栏包含该标定中心的那个任务。"))
        except Exception as exc:  # noqa: BLE001
            return self._error("home_info_failed", str(exc))

    def prepare_run(
        self,
        campus: str = "else",
        distance_km: float | None = None,
        pace_min_per_km: float | None = None,
        candidates: int = 8,
        seed: int | None = None,
        render_card: bool = True,
        basemap_mode: str = "full",
    ) -> dict[str, Any]:
        """Phase 1: generate a staged table and its card. Never touches the network.

        Returns a single-use token. Nothing is uploaded here, so this can be called as
        often as the caller likes without consequence.

        basemap_mode: 'full' 卡片带高德底图；'none' 只有渐变背景 + 轨迹线。
        底图会把田径场的位置画出来，所以调用方按会话（群/私聊）决定传哪个。
        """
        if not self._generation_lock.acquire(blocking=False):
            return self._error("busy", "another local simulation is already running")
        try:
            campus_dir = CAMPUS_DIRS.get(str(campus or "else").strip().lower())
            if campus_dir is None:
                raise ValueError(f"unsupported campus: {campus}")
            # 这个目录是打表文件的落点。干净 clone 上它还不存在（不入库），原来这里
            # must_exist=True 会抛一个只有路径的 FileNotFoundError，第一次生成必挂——
            # 建出来就行。
            campus_path = self._contained(self.project_dir / campus_dir, must_exist=False)
            campus_path.mkdir(parents=True, exist_ok=True)

            if distance_km is not None:
                distance_km = self._finite_number(distance_km, "distance_km", 3.01, 50.0)
            if pace_min_per_km is not None:
                pace_min_per_km = self._finite_number(pace_min_per_km, "pace_min_per_km", 2.0, 30.0)
            if isinstance(candidates, bool) or int(candidates) != candidates or not 1 <= int(candidates) <= 100:
                raise ValueError("candidates must be an integer between 1 and 100")
            if seed is not None and (isinstance(seed, bool) or int(seed) != seed or not 0 <= int(seed) < 2**63):
                raise ValueError("seed must be an integer between 0 and 2^63-1")

            from tasklist_builder import build_tasklist

            document, plan, quality = build_tasklist(
                seed=seed, distance_km=distance_km, pace_min_per_km=pace_min_per_km,
                candidates=int(candidates), school_id=self._school_id(),
            )
            if not plan.qualified:
                return self._error(
                    "not_qualified",
                    f"planned run fails the local rule: {plan.distance_m / 1000.0:.2f} km / "
                    f"{plan.duration_s / 60.0:.1f} min")
            points = quality.pop("_points", [])
            # The pace that actually goes to the server, used by the card's chart. Dropping it
            # here (as this used to) makes the bot's card fall back to the CSV's noisy speed
            # column, which draws a hairball instead of the run's shape.
            pace_trace = quality.get("pace_trace") or []
            # Refuse to stage a trajectory that does not lie in the stadium the record would be
            # filed under: the two are decided independently (calibration vs task list), and when
            # they disagree the record silently lands on another field.
            fence = self._fence_check([(float(p["point"].split(",")[0]),
                                        float(p["point"].split(",")[1]))
                                       for p in document["data"]["pointsList"]],
                                      required=False)
            # A *mismatch* is fatal here (never stage a run for the wrong field); an unreachable
            # server is not, because generation is local and the hard gate belongs at execution.
            if not fence.get("ok") and fence.get("error", {}).get("code") != "fence_check_failed":
                return fence
            stadium_verified = bool(fence.get("ok") and fence.get("verified"))
            stadium_note = fence.get("note") if not stadium_verified else None
            # 开头提醒：任务要求人脸但人脸库未激活时，在令牌签发阶段就亮红灯——
            # 不是拦截生成（本地生成无副作用），而是把"确认后必然在开始前被拒"
            # 提前说清楚，省得用户确认完才失败。
            face_status_raw = str(fence.get("face_status") or "").strip().upper()
            # 只有「服务端可达且核对通过」时，才谈得上「这个任务没启用人脸」。核对没做成
            # （fence_check_failed）时 face_status 根本没这个键，把它当成"未启用"会让
            # 前端显示「纯打表」，而执行端仍然会拦——用户会以为万事俱备。
            face_known = bool(fence.get("ok"))
            face_required = face_status_raw == "Y"
            face_ready = bool(self.face_ready)
            face_warning = None
            if face_required and not face_ready:
                face_warning = ("该任务启用人脸核验（runFaceStatus=Y），但人脸库未激活照片："
                                "现在确认开跑会在开始前被明确拒绝（不创建记录）。"
                                "请先到管理前台「⑥ 人脸库」导入并激活。")
            elif not face_known:
                face_warning = ("场地核对没做成（服务端不可达），人脸要求**未知**：别当成"
                                "「没启用人脸」，执行前会再核一次，可能被拒。")
            elif not face_status_raw and stadium_verified:
                face_warning = "人脸要求未知（服务器未下发该字段，执行时按未启用核对）"
            if not points:
                raise RuntimeError("business layer returned no trajectory points")

            self.staging_root.mkdir(parents=True, exist_ok=True)
            task_name = (f"tasklist_sim_{time.strftime('%Y%m%d-%H%M%S')}"
                         f"_{plan.seed & 0xFFFF:04x}.json")
            task_path = self.staging_root / task_name
            plan_values = {
                "distance_km": round(plan.distance_m / 1000.0, 3),
                "duration_s": round(plan.duration_s, 1),
                "pace_min_per_km": round(plan.pace_min_per_km, 2),
                "cadence": document["data"]["recodeCadence"],
                "stride_m": round(plan.stride_m, 3),
                "step_total": plan.step_total,
                "qualified": True,
                "seed": plan.seed,
            }
            task_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            # The table exists now, so trimming to the full limit afterwards leaves exactly
            # the limit on disk (trimming to limit-1 beforehand would settle one short).
            from gps_track_lab.retention import prune_tasklists

            prune_tasklists(self.staging_root, self.keep_tasklists)

            summary = {k: v for k, v in plan_values.items() if k != "seed"}
            summary["campus"] = campus_dir

            card_meta: dict[str, Any] = {}
            run_id = None
            if render_card:
                from gps_track_lab.calibration import load_calibration
                from gps_track_lab.generator import load_settings, write_outputs

                calibration = load_calibration(self.lab_root / "config" / "track.json")
                settings = load_settings(self.lab_root / "config.example.json")
                quality["run_plan"] = summary
                if pace_trace:
                    quality["pace_trace"] = list(pace_trace)
                # Prune before writing, leaving one free slot for the run about to be
                # written -- otherwise the steady state is limit+1, which is what "12/10"
                # in the console was reporting: keep 10, then add an 11th. The staged table
                # was already written above, so it is trimmed to the full limit instead.
                self.prune(keep_runs=max(0, self.keep_runs - 1),
                           keep_tasklists=self.keep_tasklists)
                output = write_outputs(points, self.simulation_root, plan.seed,
                                       calibration, settings, quality=quality)
                run_id = output.name
                (output / CARD_PLAN_FILE).write_text(
                    json.dumps(plan_values, ensure_ascii=False, indent=2), encoding="utf-8")
                # Trim again now that this run is on disk. The pre-write prune cannot hold
                # the invariant by itself: two entry points generating at the same moment
                # (the bot and the console are separate processes) can each keep 9 and then
                # each write, landing on limit+1. This pass is idempotent, cheap, and never
                # removes the run that was just written.
                self.prune(keep_runs=self.keep_runs, keep_tasklists=self.keep_tasklists)
                rendered = self.card_from_run(run_id, basemap_mode=basemap_mode)
                if rendered.get("ok"):
                    card_meta = rendered

            prepared = self.runs.add(
                campus=campus_dir,
                task_file=str(task_path),
                card_png=str(card_meta.get("png", "")),
                distance_km=summary["distance_km"],
                duration_s=summary["duration_s"],
                pace_min_per_km=summary["pace_min_per_km"],
                cadence=summary["cadence"],
                stride_m=summary["stride_m"],
                point_count=len(document["data"]["pointsList"]),
                qualified=True,
                seed=plan.seed,
                meta={"run_id": run_id,
                      "card": {k: str(v) for k, v in card_meta.items() if k not in ("png",)},
                      # Which stadium this record will be filed under. Carried on the prepared
                      # run so the card text and the pending list can show it before submission.
                      "stadium": {"task_id": fence.get("task_id"),
                                  "name": fence.get("stadium_name"),
                                  "verified": stadium_verified,
                                  "note": stadium_note},
                      "face": {"required": face_required,
                               "known": face_known,
                               "status": face_status_raw or "(未知)",
                               "ready": face_ready,
                               "warning": face_warning}},
            )
            payload = prepared.summary()
            payload["stadium_task_id"] = fence.get("task_id")
            payload["stadium_name"] = fence.get("stadium_name")
            payload["stadium_verified"] = stadium_verified
            payload["stadium_note"] = stadium_note
            payload["face"] = {"required": face_required,
                               "known": face_known,
                               "status": face_status_raw or "(未知)",
                               "ready": face_ready,
                               "warning": face_warning}
            return self._ok(
                prepared=payload,
                stadium={"task_id": fence.get("task_id"), "name": fence.get("stadium_name"),
                         "verified": stadium_verified, "note": stadium_note},
                campus=campus_dir,
                staging_dir=str(self.staging_root),
                card={"png": card_meta.get("png"), "run_id": run_id},
                next_step=("把卡片发给用户；得到明确同意后再调用 yun_confirm_run，"
                           "并带上同一个 token。"),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error("prepare_failed", str(exc))
        finally:
            self._generation_lock.release()

    def start_background_run(self, token: str, confirmed: bool = False,
                             dry_run: bool = False, is_drift: bool = False,
                             timeout_s: float = 3600.0) -> dict[str, Any]:
        """Start a run on a detached thread and return immediately.

        A real record takes as long as it says it takes (16-25 minutes of wall clock), and
        hosts impose their own limit on a tool call -- 60 seconds is a common one, after which
        it cancels the awaiting coroutine. Blocking on a synchronous ``execute_run`` there
        does not just time out: it can leave the chain running as an orphan while the
        caller is told it failed. So the work moves to its own thread and the caller polls
        ``run_state()`` for progress, which is what the console and the bot both do.

        This is the only place a background run may be started, and it keeps the same
        gates as ``execute_run``: a pending token, ``confirmed=True``, and ``allow_submit``
        (enforced by the worker actually calling ``execute_run``).
        """
        run = self.runs.get(token)
        if run is None:
            return self._error(
                "unknown_token",
                "no pending run for that token: it expired (30 分钟), was already used, was "
                "cancelled, or its staged table is gone. Call yun_prepare_run again for a "
                "new token; do not retry an old one.")
        if not confirmed:
            return self._error("confirmation_required",
                               "refusing to start without confirmed=True: show the card first")
        if not self.allow_submit:
            return self._error("submit_disabled",
                               "this bridge was constructed with allow_submit=False")
        with self._run_state_lock:
            if self._run_state is not None and self._run_state.get("status") == "running":
                previous = self._run_state
                return self._error(
                    "already_running",
                    f"run {previous.get('token')} 仍在执行中（已 {previous.get('elapsed_seconds')} 秒，"
                    f"模式 {'演练' if previous.get('dry_run') else '真实提交'}）；"
                    "先等它结束，或用 yun_abort_run 中止")
            if not self._run_lock.acquire(blocking=False):
                return self._error(
                    "runner_busy",
                    "执行锁被占用：可能有上一次被宿主取消后遗留的线程仍在跑。"
                    "先等待或用 yun_abort_run 中止，再重试")
            self._abort_event = threading.Event()
            state: dict[str, Any] = {
                "status": "running", "token": token, "dry_run": bool(dry_run),
                "started_at": time.time(), "lines": [], "result": None,
            }
            run.started_at = state["started_at"]
            self._run_state = state
            # Publish immediately: from this moment another process can see (and stop) it.
            self._write_run_state_locked()

        run_lock = self._run_lock
        state_lock = self._run_state_lock
        state = self._run_state

        if self.spawn_detached:
            return self._spawn_detached_run(
                token, run, state=state, run_lock=run_lock, state_lock=state_lock,
                dry_run=dry_run, is_drift=is_drift, timeout_s=timeout_s)

        return self._start_in_process_run(token, run, state=state, run_lock=run_lock,
                                          state_lock=state_lock, dry_run=dry_run,
                                          is_drift=is_drift, timeout_s=timeout_s)

    def _spawn_detached_run(self, token: str, run: PreparedRun, *, state: dict[str, Any],
                            run_lock: threading.Lock, state_lock: threading.RLock,
                            dry_run: bool, is_drift: bool,
                            timeout_s: float) -> dict[str, Any]:
        """Hand the run to ``scripts/run_worker.py`` and return at once.

        The child re-checks the gates itself (the token is single-use, so it cannot be
        started twice), writes progress and the final result to the shared state file, and
        outlives this process -- which is the whole point: a host restarting mid-record
        used to kill the upload and leave no record behind.
        """
        import subprocess

        worker = self.project_dir.parent / "scripts" / "run_worker.py"
        if not worker.is_file():
            # Fall back rather than refuse: a project checked out without scripts/ still
            # deserves a working (if host-bound) run.
            return self._start_in_process_run(token, run, state=state, run_lock=run_lock,
                                              state_lock=state_lock, dry_run=dry_run,
                                              is_drift=is_drift, timeout_s=timeout_s)

        interpreter = sys.executable or "python"
        log_path = self.staging_root / f"{RUN_LOG_PREFIX}{token}_{int(time.time())}.log"
        if getattr(sys, "frozen", False):
            # 被冻结成可执行文件时 sys.executable 是入口程序本身，改用「--worker
            # 自委托」分派（见 launcher.py），脚本路径参数不再传。
            prefix = [interpreter, "--worker"]
        else:
            prefix = [interpreter, "-X", "utf8", str(worker)]
        command = [
            *prefix,
            "--token", token,
            "--project", str(self.project_dir),
            "--config", str(self.config_path),
            "--keep-runs", str(self.keep_runs),
            "--keep-tasklists", str(self.keep_tasklists),
            "--timeout", str(timeout_s),
            "--log", str(log_path),
        ]
        if self.external_simulation_root:
            command += ["--simulation-root", str(self.simulation_root)]
        if is_drift:
            command += ["--is-drift"]
        if dry_run:
            command += ["--dry-run"]
        if os.environ.get("YUN_WORKER_DEBUG"):
            command.append("--debug")

        env = dict(os.environ)
        # Explicit plugin settings must survive the handoff to the fresh worker.
        face = self.face_args()
        env["YUN_FACE_PHOTO"] = str(face.get("photo") or "")
        env["YUN_FACE_DETECTION"] = str(face.get("detection") or "")
        env["YUN_FACE_MIRROR"] = "1" if face.get("mirror") else "0"
        parts = [str(self.project_dir), str(self.project_dir / "gps_track_lab" / "src"),
                 str(self.project_dir / "runner"), str(self.project_dir / "tools")]
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(parts + ([existing] if existing else []))
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        popen_extra: dict[str, Any] = {}
        if os.name == "nt":
            # Detach from this console/session so the child is not killed with the host.
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # POSIX 的等价物：让子进程进独立会话（setsid）。否则父进程所在的终端窗口一关，
            # SIGHUP 就会把正在上传的执行进程打死——那正是 Windows 侧 DETACHED_PROCESS
            # 要防的事，macOS / Linux 上必须用 start_new_session 补上。
            popen_extra["start_new_session"] = True
        try:
            self.staging_root.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "ab")
            try:
                child = subprocess.Popen(
                    command, env=env, cwd=str(self.project_dir), creationflags=creationflags,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle, stderr=subprocess.STDOUT, close_fds=True,
                    **popen_extra)
            finally:
                # The child owns its own handle; leaving ours open keeps the file locked on
                # Windows and, worse, means nothing ever flushes the child's output.
                log_handle.close()
        except Exception as exc:  # noqa: BLE001
            self._release_run(reset_state=True)
            return self._error("spawn_failed",
                               f"无法启动独立执行进程: {type(exc).__name__}: {exc}")

        # The child owns the run now: hand over the lock and the state, keep the token out
        # of this process's registry (single use), and publish the child's pid so any
        # process can see -- and stop -- the run.
        run_lock.release()
        with state_lock:
            state["pid"] = child.pid
            state["detached_pid"] = child.pid
            state["log_path"] = str(log_path)
            self._run_state = None
            self._write_detached_state(state)
        return self._ok(
            detached=True, detached_pid=child.pid, token=token, dry_run=bool(dry_run),
            status="running", log=str(log_path),
            note=("跑步已在独立进程开始执行（打表模式）。它不随宿主退出而中断；"
                  "用 yun_run_status 查进度，yun_abort_run 中止。"))

    def _write_detached_state(self, state: dict[str, Any]) -> None:
        """Publish a detached run's state under the same schema as an in-process run."""
        payload = {
            "version": 1, "status": "running", "token": state.get("token"),
            "dry_run": state.get("dry_run"), "started_at": state.get("started_at"),
            "finished_at": None, "pid": state.get("detached_pid"),
            "detached": True, "log": state.get("log_path"),
            "line_count": 0, "lines": [], "result": None,
        }
        target = self._state_path_for(payload.get("token"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            pass

    def _release_run(self, *, reset_state: bool = False) -> None:
        """Give back the execution lock taken by a start that then failed."""
        try:
            self._run_lock.release()
        except RuntimeError:
            pass
        if reset_state:
            with self._run_state_lock:
                self._run_state = None

    def _start_in_process_run(self, token: str, run: PreparedRun, *, state: dict[str, Any],
                              run_lock: threading.Lock, state_lock: threading.RLock,
                              dry_run: bool, is_drift: bool,
                              timeout_s: float) -> dict[str, Any]:
        """The fallback used when no worker script is available (and by unit tests)."""
        abort_path = self._abort_flag_for()
        try:
            abort_path.unlink()
        except OSError:
            pass
        abort_event = self._abort_event
        notes = self._run_note

        def worker() -> None:
            try:
                result = self._run_guarded(token, run, dry_run=dry_run, is_drift=is_drift,
                                           timeout_s=timeout_s, progress_cb=notes,
                                           abort_event=abort_event,
                                           abort_flag_path=abort_path)
            except Exception as exc:  # noqa: BLE001 - the thread must always settle
                result = self._error("runner_crashed", f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    abort_path.unlink()
                except OSError:
                    pass
                try:
                    run_lock.release()
                finally:
                    with state_lock:
                        state["result"] = result
                        state["status"] = ("done" if result.get("ok") else
                                           ("aborted" if abort_event.is_set() else "failed"))
                        state["finished_at"] = time.time()
                        self._write_run_state_locked()

        thread = threading.Thread(target=worker, name=f"yunrun-{token}", daemon=True)
        run.worker = thread
        thread.start()
        return self._ok(
            detached=False, token=token, dry_run=bool(dry_run), status="running",
            note=("跑步已在后台开始执行（打表模式）。用 yun_run_status 查询进度；"
                  "不要重复调用确认，否则会被拒绝。"))

    def _run_note(self, line: str) -> None:
        """Feed one progress line into the live run state (called from the worker thread).

        Note that this must not go through ``print``: the worker holds a process-wide
        ``redirect_stdout`` while the chain runs, so anything written to stdout by another
        thread lands in the captured buffer too. Appending straight to the shared state
        keeps the progress observable while the run is still going.
        """
        with self._run_state_lock:
            if self._run_state is not None:
                self._run_state["lines"].append(str(line))
                self._write_run_state_locked()

    def _abort_flag_for(self, pid: int | None = None) -> Path:
        """Path of the abort flag the owning process watches.

        Named after the owning process so a stop request from another process (the console
        stopping a run the bot started) lands on exactly that run.
        """
        return self.staging_root / f"abort.{pid or os.getpid()}.flag"

    def _state_path_for(self, token: str | None) -> Path:
        """Per-run state file. One file for every run meant concurrent runs clobbered each other."""
        name = str(token or "").strip()
        if not name:
            return self.run_state_path
        return self.run_state_path.with_name(f"{self.run_state_path.stem}.{name}.json")

    def _write_run_state_locked(self) -> None:
        """Mirror the live run state to disk so other processes can see it."""
        state = self._run_state
        if state is None:
            return
        payload = {
            "version": 1,
            "status": state.get("status"),
            "token": state.get("token"),
            "dry_run": state.get("dry_run"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "pid": os.getpid(),
            "line_count": len(state.get("lines") or []),
            "lines": (state.get("lines") or [])[-400:],
            "result": _jsonable(state.get("result")),
        }
        target = self._state_path_for(state.get("token"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            # Losing the mirror only costs observability, never the run itself.
            pass

    def forget_lost_run(self) -> dict[str, Any]:
        """清掉「进程已经没了」的共享运行状态文件。

        共享状态文件是给「另一个进程正在跑」这件事用的。但控制台窗口被直接关掉、机器重启、
        或者宿主被杀之后，文件里还留着 status=running 和一个早已不存在的 pid——前端于是
        永远显示「这次执行失联了」，三个按钮全灭，刷新页面、重启服务、重新生成轨迹都解不开，
        只能手动去删 `yunrun/tasks_sim/active_run*.json`。

        这里只删 pid 已确认不存活的文件；pid 还活着一律不动（那可能是真的在跑）。
        """
        removed: list[str] = []
        kept: list[str] = []
        for path in self._state_files():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and _pid_alive(payload.get("pid")):
                kept.append(path.name)
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                kept.append(path.name)
        return self._ok(removed=removed, kept=kept,
                        note=("已清掉 %d 个失联状态文件" % len(removed)) if removed
                        else "没有可清理的失联状态")

    def _state_files(self) -> list[Path]:
        """Every state file present, newest first (per-run files plus the legacy single one)."""
        root = self.run_state_path.parent
        if not root.is_dir():
            return []
        candidates = list(root.glob(f"{self.run_state_path.stem}.*.json"))
        if self.run_state_path.is_file():
            candidates.append(self.run_state_path)
        # Sort by mtime, newest first. ``_state_path_for`` writes the token in the name, so a
        # caller that knows its token does not need this ordering at all.
        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)

    def _read_shared_run_state_raw(self, token: str | None = None) -> dict[str, Any] | None:
        """The shared state of the newest run (or of ``token``), without the liveness verdict."""
        if token:
            paths = [self._state_path_for(token)]
        else:
            paths = self._state_files()
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("status"):
                return payload
        return None

    def _read_shared_run_state(self) -> dict[str, Any] | None:
        """Read the run state another process wrote, if it is still meaningful."""
        payload = self._read_shared_run_state_raw()
        if payload is None or "status" not in payload:
            return None
        status = payload.get("status")
        result = payload.get("result")
        if status == "running" and (result or payload.get("finished_at")):
            # A verdict is already recorded, so the run is over whatever the probe says. The
            # worker writes its result and exits immediately, and ``_pid_alive`` can lose that
            # race -- which reported successful runs as "lost" about two times in three.
            payload["status"] = "done" if (result or {}).get("ok") else "failed"
        elif status == "running" and not _pid_alive(payload.get("pid")):
            # Genuinely unfinished and its process is gone: nothing is uploading any more, so
            # claiming "running" would be a lie the reader cannot act on.
            payload["status"] = "lost"
            payload["note"] = ("发起进程已退出，这次执行的结果无法确认；"
                              "记录可能只上传了一部分，请到 App 里核对成绩")
        payload["shared"] = True
        payload["lines"] = [str(line) for line in payload.get("lines") or []]
        return payload

    def run_state(self) -> dict[str, Any] | None:
        """Snapshot of the run in flight, or None when nothing is executing.

        Falls back to the shared file so the question can be answered by a *different*
        process than the one running the chain -- which is the normal case: the bot's MCP
        server starts the run, the plugin answers yun_run_status.
        """
        with self._run_state_lock:
            state = self._run_state
            if state is None:
                return self._read_shared_run_state()
            snapshot = dict(state)
            snapshot["lines"] = [str(line) for line in state.get("lines") or []]
            snapshot.setdefault("token", state.get("token"))
            snapshot["elapsed_seconds"] = round(
                (state.get("finished_at") or time.time()) - state["started_at"], 1)
        return snapshot

    def run_activity(self) -> dict[str, Any]:
        """A JSON-safe version of ``run_state`` for tool results (no live callables).

        When nothing is live in this process it reports what the shared state file says, so
        a *different* process can still answer "is it running?": the bot's MCP server starts
        the run and the plugin answers yun_run_status, and without this the plugin
        truthfully but uselessly reported ``active: false`` for a run in flight.
        """
        state = self.run_state()
        if state is None:
            return self._ok(active=False)
        lines = [str(line) for line in state.get("lines") or []]
        started = state.get("started_at")
        elapsed = state.get("elapsed_seconds")
        if elapsed is None and started:
            elapsed = round((state.get("finished_at") or time.time()) - float(started), 1)
        return self._ok(
            active=state.get("status") == "running",
            status=state.get("status"),
            token=state.get("token"),
            dry_run=state.get("dry_run"),
            elapsed_seconds=elapsed,
            line_count=len(lines),
            recent_lines=lines[-8:],
            result=state.get("result"),
            shared=bool(state.get("shared")),
            note=state.get("note"),
        )

    def abort_run(self, *tokens: str) -> dict[str, Any]:
        """Request an abort of the run in flight, and/or drop pending tokens.

        The abort is only detected inside the runner's sleep, which is exactly where a
        run spends its time between batches, so it takes effect within one batch rather
        than instantly. Explicit tokens that have not started are cancelled outright.
        """
        cancelled: list[str] = []
        for token in tokens:
            if token and self.runs.pop(token) is not None:
                cancelled.append(token)
        with self._run_state_lock:
            state = self._run_state
        if state is not None and state.get("status") == "running":
            self._abort_event.set()
            return self._ok(aborted=True, cancelled=cancelled, active=True,
                            token=state.get("token"), where="this-process",
                            note="已发出中止信号：会在下一次分批间隔处停止（不会 finish）")
        # Not ours: the run may belong to another process (the bot's MCP server starts it,
        # the console stops it). The shared state names that process, so leave a flag its
        # worker checks on every batch boundary.
        #
        # Read the file raw rather than through _read_shared_run_state: that helper reports
        # a run whose process looks gone as "lost", and a stop request must not be refused
        # just because the liveness probe was unavailable or wrong. Worst case the flag is
        # never picked up, which is exactly what happens if we decline to write it.
        shared = self._read_shared_run_state_raw()
        if shared is not None and shared.get("status") == "running" and shared.get("pid"):
            flag = self._abort_flag_for(int(shared["pid"]))
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(str(time.time()), encoding="utf-8")
            except OSError as exc:
                return self._error("abort_failed",
                                   f"无法向进程 {shared['pid']} 发出中止信号: {exc}")
            return self._ok(aborted=True, cancelled=cancelled, active=True,
                            token=shared.get("token"), where=f"pid:{shared['pid']}",
                            note="已向另一个进程发出中止信号：会在下一次分批间隔处停止"
                                 "（不会 finish）")
        return self._ok(aborted=False, cancelled=cancelled, active=False,
                        note="当前没有在执行的跑步；已取消的待确认令牌见 cancelled")

    def pending_runs(self) -> dict[str, Any]:
        pending = self.runs.pending()
        return self._ok(count=len(pending), pending=[run.summary() for run in pending])

    def cancel_run(self, token: str) -> dict[str, Any]:
        run = self.runs.pop(token)
        if run is None:
            return self._error("unknown_token", "no pending run for that token")
        return self._ok(cancelled=run.token, task_file=run.task_file)

    def execute_run(self, token: str, confirmed: bool = False, dry_run: bool = False,
                    is_drift: bool = False, timeout_s: float = 3600.0,
                    progress_cb: "Callable[[str], None] | None" = None,
                    _abort_event: threading.Event | None = None,
                    publish_state: bool = True) -> dict[str, Any]:
        """Phase 2: submit a prepared table in batched (打表) mode.

        Three gates, in order:

        1. a token minted by ``prepare_run`` (so a caller cannot invent a run);
        2. ``confirmed=True`` (the human said yes after seeing the card);
        3. ``allow_submit`` on the bridge instance.

        ``dry_run=True`` walks the whole chain over a fake transport: no network, no
        waiting. Use it to verify a prepared table before committing to the real thing.

        ``progress_cb`` receives each progress line as it happens, which is what lets a
        live UI follow a 20-minute run; the buffered list is still returned either way.
        """
        if not self.allow_submit:
            return self._error("submit_disabled",
                               "this bridge was constructed with allow_submit=False")
        if not confirmed:
            return self._error(
                "confirmation_required",
                "execute_run refuses to run without confirmed=True; show the card to the "
                "user and only then confirm")
        run = self.runs.get(token)
        if run is None:
            return self._error(
                "unknown_token",
                "no pending run for that token: it expired (30 分钟), was already used, was "
                "cancelled, or its staged table is gone. Call yun_prepare_run again for a "
                "new token; do not retry an old one.")
        if run.card_png and not Path(run.card_png).is_file() and not dry_run:
            return self._error("card_missing", "the prepared card is gone; prepare again")
        # The stadium must be verified before a record is filed: this is the moment a mismatch
        # would actually attach the run to another field, so the gate lives here rather than at
        # generation time (which is local and must keep working during an outage).
        stadium = (getattr(run, "meta", None) or {}).get("stadium") or {}
        if stadium and stadium.get("verified") is False and not dry_run:
            return self._error(
                "stadium_unverified",
                "这次准备时没能核对场地（服务端不可达），为避免记录挂到别的场地已拒绝执行；"
                "请等服务端恢复后重新 prepare。"
                + (f"（{stadium.get('note')}）" if stadium.get("note") else ""))

        if not self._run_lock.acquire(blocking=False):
            return self._error(
                "busy",
                "another run is already executing（可能是上一次调用被宿主取消后仍在跑的线程；"
                "用 yun_run_status 看进度，或 yun_abort_run 中止）")
        # 跨进程中止：abort_run() 是往「共享状态里记录的那个 pid」写 abort.<pid>.flag 的。
        # 独立执行进程里那个 pid 就是自己，所以这里必须把路径交给执行链——否则
        # abortable_sleep 只看 event/flag 都为 None 就直接 time.sleep，中止会变成
        # 静默空操作（还会回一个 aborted=true），20 分钟的上传链照样跑到 finish。
        abort_flag_path = self._abort_flag_for()
        try:
            abort_flag_path.unlink()
        except OSError:
            pass
        try:
            return self._run_guarded(token, run, dry_run=dry_run, is_drift=is_drift,
                                     timeout_s=timeout_s, progress_cb=progress_cb,
                                     abort_event=_abort_event,
                                     abort_flag_path=abort_flag_path,
                                     publish_state=publish_state)
        finally:
            try:
                abort_flag_path.unlink()
            except OSError:
                pass
            self._run_lock.release()

    def _run_guarded(self, token: str, run: PreparedRun, *, dry_run: bool, is_drift: bool,
                      timeout_s: float, progress_cb: "Callable[[str], None] | None",
                      abort_event: threading.Event | None,
                      abort_flag_path: Path | None = None,
                      publish_state: bool = False) -> dict[str, Any]:
        from run_control import RegistryError, file_mutex
        import hashlib

        # Bridges using this config share a mutex even if their staging roots differ.
        key = hashlib.sha256(os.path.normcase(str(self.config_path.resolve())).encode()).hexdigest()[:24]
        lock_path = self.project_dir / "tasks_sim" / f"execution.{key}.lock"
        try:
            mutex = file_mutex(lock_path, timeout=0.0)
            mutex.__enter__()
        except RegistryError as exc:
            failed = self._error("shared_execution_unavailable", str(exc))
            failed["token"] = token
            if publish_state:
                self._publish_final_state(failed)
            return failed
        try:
            return self._run_claimed(token, run, dry_run=dry_run, is_drift=is_drift,
                                     timeout_s=timeout_s, progress_cb=progress_cb,
                                     abort_event=abort_event, abort_flag_path=abort_flag_path,
                                     publish_state=publish_state)
        finally:
            thread = getattr(self, "_execution_thread", None)
            if thread is not None and thread.is_alive():
                # A timeout is not thread termination. Keep the process mutex until the
                # upload thread actually exits, while letting the caller see the timeout.
                def release_when_stopped():
                    try:
                        thread.join()
                    finally:
                        mutex.__exit__(None, None, None)
                threading.Thread(target=release_when_stopped, daemon=True).start()
            else:
                mutex.__exit__(None, None, None)

    def _run_claimed(self, token: str, run: PreparedRun, *, dry_run: bool, is_drift: bool,
                     timeout_s: float, progress_cb: "Callable[[str], None] | None",
                     abort_event: threading.Event | None,
                     abort_flag_path: Path | None = None,
                     publish_state: bool = False) -> dict[str, Any]:
        """The run itself, with the execution lock already held by the caller.

        Split out from :meth:`execute_run` because the detached path
        (:meth:`start_background_run`) takes the lock on the calling thread and then runs
        the work on its own; acquiring it a second time there would deadlock against
        itself and report "busy" for a run nobody else is doing.

        ``publish_state`` is what the detached worker uses: it is a plain ``execute_run``
        call in its own process, and without publishing, the run would be invisible to
        every other process and would leave nothing behind but a log file.
        """
        task_path = self._contained(run.task_file, must_exist=True)
        progress: list[str] = []

        if publish_state:
            with self._run_state_lock:
                self._run_state = {
                    "status": "running", "token": token, "dry_run": bool(dry_run),
                    "started_at": time.time(), "lines": [], "result": None,
                }
                self._write_run_state_locked()

        def note(message: str) -> None:
            progress.append(f"[{time.strftime('%H:%M:%S')}] {message}")
            if publish_state:
                self._run_note(progress[-1])
            if progress_cb is not None:
                try:
                    progress_cb(progress[-1])
                except Exception:  # noqa: BLE001 - a broken observer must not abort a run
                    pass

        try:
            claimed = self.runs.pop(token)
            if claimed is None:
                failed = self._error("unknown_token", "token already consumed, expired or cancelled; execution refused")
                failed["token"] = token
                if publish_state:
                    self._publish_final_state(failed)
                return failed
            run = claimed
            task_path = self._contained(run.task_file, must_exist=True)
            note(f"模式=打表(batched) dry_run={dry_run} campus={run.campus}")
            note(f"表格={task_path.name} 点数={run.point_count} 距离={run.distance_km}km "
                 f"用时={run.duration_s / 60:.1f}min")
            result = self._execute_table(task_path, dry_run=dry_run, is_drift=is_drift,
                                         note=note, timeout_s=timeout_s,
                                         progress_cb=progress_cb, abort_event=abort_event,
                                         abort_flag_path=abort_flag_path,
                                         publish_state=publish_state,
                                         face_config=self.face_args())
            result.update({"progress": progress, "token": run.token,
                           "task_file": str(task_path), "dry_run": dry_run,
                           "mode": "batched-table"})
            if publish_state:
                self._publish_final_state(result)
            return result
        except Exception as exc:  # noqa: BLE001
            note(f"执行失败: {type(exc).__name__}: {exc}")
            failed = self._error("execute_failed", str(exc))
            failed["progress"] = progress
            if publish_state:
                self._publish_final_state(failed)
            return failed

    def _publish_final_state(self, result: dict[str, Any]) -> None:
        """Write the verdict of a finished run to the shared state file."""
        with self._run_state_lock:
            if self._run_state is None:
                self._run_state = {"status": "running", "token": result.get("token"),
                                   "dry_run": result.get("dry_run"),
                                   "started_at": time.time(), "lines": [], "result": None}
            self._run_state["result"] = result
            self._run_state["status"] = "done" if result.get("ok") else "failed"
            self._run_state["finished_at"] = time.time()
            self._write_run_state_locked()

    def face_args(self) -> dict[str, Any]:
        """人脸源的配置快照（供 runner.build_face_runner 使用）。

        独立成方法是为了让 detached worker 也能拿到：worker 是个新进程，它按 token
        从 pending_runs.json 取回 prepared run，再调用 bridge，于是同样会走到这里。
        """
        return {"photo": self.face_photo, "detection": self.face_detection,
                "mirror": self.face_mirror}

    def face_status(self) -> dict[str, Any]:
        """人脸配置状态（脱敏），供 yun_status 显示是否接通。"""
        import os as _os

        def _shown(path: str) -> str:
            if not path:
                return ""
            return f"{_os.path.basename(path)}"
        return {
            "configured": bool(self.face_ready),
            "photo": _shown(self.face_photo),
            "detection": _shown(self.face_detection),
            "mirror": self.face_mirror,
            "note": ("已配置人脸源：runFaceStatus=Y 时会自动比对"
                     if self.face_ready else
                     "未配置人脸源：runFaceStatus=Y 的任务会被 runner 明确拒绝"
                     "（在插件配置里填 face_photo / face_detection）"),
        }

    def _build_face_runner(self, runner, face_config: dict[str, Any] | None):
        """按配置构建人脸 runner（委托给 runner.build_face_runner，不重写其预检）。

        为什么委托而不是自己拼 FaceRunner：build_face_runner 里那套预检是**刻意**的
        （main.py:1358 "预检在任何真实网络请求之前"）——照片解码、标注内容哈希绑定、
        取景质量门、最终压缩形态全在 start 之前跑完，失败即未发出任何请求。
        绕开它就等于把必然失败的输入推迟到 start 之后。

        返回 None 表示"没配人脸源"——此时若任务要求人脸，runner 会自己明确停止。
        """
        cfg = dict(face_config or {})
        photo = str(cfg.get("photo") or "").strip()
        detection = str(cfg.get("detection") or "").strip()
        if not (photo and detection):
            return None
        # 能力守卫：测试替身或旧版 runner 可能没有 build_face_runner。硬调会变成
        # AttributeError，把"这个 runner 没接人脸能力"伪装成执行失败；这里显式
        # 当作"没有该能力"，与"没配人脸源"走同一条路径。
        builder = getattr(runner, "build_face_runner", None)
        if not callable(builder):
            return None
        import argparse as _argparse

        args = _argparse.Namespace(face_photo=photo, face_video=None,
                                   face_detection=detection,
                                   face_mirror=bool(cfg.get("mirror")))
        return builder(args)

    def _execute_table(self, task_path: Path, *, dry_run: bool, is_drift: bool,
                       note, timeout_s: float,
                       progress_cb: "Callable[[str], None] | None" = None,
                       abort_event: threading.Event | None = None,
                       abort_flag_path: Path | None = None,
                       publish_state: bool = False,
                       face_config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run ``start -> do_by_points_map -> finish_by_points_map`` for one table file."""
        abort_event = abort_event if abort_event is not None else threading.Event()
        runner = self._runner()
        config = self._config()

        def tee_line(line: str) -> None:
            # The runner's own output IS the progress (one line per uploaded batch). When
            # this process owns the shared state, publish it too: otherwise the state file
            # reported "3 lines" for a run that had already uploaded fifty batches.
            if publish_state:
                self._run_note(line)
            if progress_cb is not None:
                try:
                    progress_cb(line)
                except Exception:  # noqa: BLE001 - an observer must not break a run
                    pass

        buffer = _TeeLog(tee_line)
        box: dict[str, Any] = {}

        def abortable_sleep(seconds: float) -> None:
            """Sleep in slices so an abort lands within a second of being requested.

            A record's run time is almost entirely spent waiting between batches, so this
            is where a stop request has to be noticed; without it an abort would only
            happen after the whole record finished, which is no abort at all.

            Two channels are checked: the event (same process, immediate) and a flag file
            named after this process (another process asked, e.g. the web console stopping
            a run the bot started).
            """
            if abort_event is None and abort_flag_path is None:
                time.sleep(seconds)
                return
            remaining = max(0.0, float(seconds))
            while remaining > 0:
                _check_abort(abort_event, abort_flag_path)
                slice_s = min(1.0, remaining)
                time.sleep(slice_s)
                remaining -= slice_s
            _check_abort(abort_event, abort_flag_path)

        class _AbortAwareClient:
            """Proxy that routes the runner's sleeping through the abort check.

            A proxy rather than an attribute patch because the session builds its own
            client lazily (``self.client = client or default_client()``), so at the moment
            the session is constructed there may be no client to patch yet -- and patching
            the wrong one silently turns "中止" into a button that does nothing.

            ``instant`` keeps the dry-run contract (fake transport, no waiting): the abort
            check still runs, but the delay itself is skipped. Without that distinction the
            abort wrapper would turn a 2-second rehearsal back into a real-time run.
            """

            def __init__(self, inner, abort_event: threading.Event | None, instant: bool,
                         flag_path: Path | None = None):
                self._inner = inner
                self._abort_event = abort_event
                self._instant = instant
                self._flag_path = flag_path

            def __getattr__(self, name):
                value = getattr(self._inner, name)
                if name in ("post", "post_json") and callable(value):
                    def checked(*args, **kwargs):
                        _check_abort(self._abort_event, self._flag_path)
                        return value(*args, **kwargs)
                    return checked
                return value

            def sleep(self, seconds: float) -> None:
                if self._instant:
                    _check_abort(self._abort_event, self._flag_path)
                    return None
                return abortable_sleep(seconds)

        def wrap_abort(client, *, instant: bool = False):
            if abort_event is None and abort_flag_path is None and not instant:
                return client
            if client is None:
                return None
            return _AbortAwareClient(client, abort_event, instant, abort_flag_path)

        def work() -> None:
            import os

            cwd = Path.cwd()
            session = None
            try:
                runner.set_args(str(self.config_path))
                if dry_run:
                    # Mirrors run_dry() in the runner: fake transport answering from the
                    # fixture, a fixed key pair so the envelope can be unwrapped locally,
                    # and no sleeping. No network, no waiting.
                    home = json.loads(
                        (self.project_dir / "dry_run_home.json").read_text(encoding="utf-8"))
                    profile = runner.build_profile(config)
                    transport = runner.FakeTransport(
                        runner.dry_run_responder(home),
                        sm2box=runner.SM2Box(runner.PUBLIC_KEY, runner.PRIVATE_KEY),
                        fixed_pair=((profile.cipherkey_encrypted, profile.cipherkey)
                                    if profile.cipherkey_encrypted else None),
                        fallback_key_b64=profile.cipherkey or None,
                    )
                    client = runner.YunClient(
                        profile, base_url=runner.my_host, transport=transport,
                        rng=runner.random.Random(20260911), sleep=lambda _seconds: None)
                    session = runner.Yun_For_New(client=wrap_abort(client, instant=True),
                                                 home_info=home)
                elif not dry_run:
                    # Real path: build the client here and hand the session a proxy, so the
                    # run can be stopped between batches. Patching afterwards is too late --
                    # the session holds its own reference to whatever it was given.
                    #
                    # 人脸源：任务 runFaceStatus=Y 时 runner 要求 face_runner，否则直接停止
                    # （main.py:706-730）。构建失败（照片缺失/标注哈希不符/质量门不过）
                    # 会抛 FaceInputError——这里让它原样冒泡到下面的 except，报出**为什么**
                    # 失败，而不是留下一条"跑到一半才发现没人脸"的现场。
                    face_runner = self._build_face_runner(runner, face_config)
                    if face_runner is not None:
                        note("[人脸] 已接入照片源：runFaceStatus=Y 时将自动比对")
                    elif not self.face_ready:
                        note("[人脸] 未配置照片源：若任务要求人脸核验，runner 会明确停止")
                    session = runner.Yun_For_New(client=wrap_abort(runner.default_client()),
                                                 face_runner=face_runner)
                else:
                    session = runner.Yun_For_New()

                with redirect_stdout(buffer), redirect_stderr(buffer):
                    session.start()
                    box["record_id"] = getattr(session, "crsRunRecordId", None)
                    session.do_by_points_map(
                        path=str(task_path.parent), random_choose=False,
                        isDrift=is_drift, pick=task_path.name)
                    session.finish_by_points_map()
                box["record_id"] = getattr(session, "crsRunRecordId", None)
                box["last_confirmed_mileage_m"] = getattr(session, "_last_confirmed_mileage_m", None)
            except BaseException as exc:  # noqa: BLE001 - report, never re-raise into a thread
                box["error"] = f"{type(exc).__name__}: {exc}"
                box["record_id"] = getattr(session, "crsRunRecordId", None)
                box["last_confirmed_mileage_m"] = getattr(session, "_last_confirmed_mileage_m", None)
            finally:
                try:
                    os.chdir(cwd)
                except OSError:
                    pass

        thread = threading.Thread(target=work, daemon=True)
        self._execution_thread = thread
        thread.start()
        thread.join(timeout=timeout_s)
        tail = buffer.getvalue()
        note(f"链路输出 {len(tail)} 字符")
        if thread.is_alive():
            abort_event.set()
            failed = self._error("timeout", f"run exceeded {timeout_s:.0f}s; abort requested, execution lock held until worker exits")
            failed["record_id"] = box.get("record_id")
            failed["output_tail"] = tail[-4000:]
            return failed
        if box.get("error"):
            failed = self._error("runner_failed", str(box["error"]))
            failed["record_id"] = box.get("record_id")
            failed["last_confirmed_mileage_m"] = box.get("last_confirmed_mileage_m")
            failed["output_tail"] = tail[-8000:]
            return failed
        return self._ok(
            record_id=box.get("record_id"),
            last_confirmed_mileage_m=box.get("last_confirmed_mileage_m"),
            output_tail=tail[-8000:],
        )

