"""Running console for the whole 云运动 chain: generate -> review -> run.

This is the web-UI front end of the same business layer the host integrations use.
:class:`mcp_bridge.YunLocalBridge`, so the two hard rules are enforced in one place and
cannot drift apart:

1. **打表 (batched) mode only.** ``execute_run`` walks
   ``start -> do_by_points_map -> finish_by_points_map``; there is no one-shot upload
   path here at all.
2. **Two stages.** A run can only start from a token minted by ``prepare_run``, and the
   bridge refuses ``execute_run`` unless ``confirmed=True``. In this console that flag is
   supplied by exactly one place -- the confirm button inside the review dialog -- so a
   stray click on the page cannot submit a record.

The original trajectory preview page (``/``) is untouched by design: this module owns
``/ui`` and ``/api/console/*`` and calls into ``web`` for the shared run/card helpers
rather than duplicating them.

A real run takes as long as the record says it takes (16-25 minutes), so execution never
happens inside a request: it runs on a background thread and the page polls a job for
progress lines as they are emitted.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import web

CONSOLE_PATH = "/ui"
API_PREFIX = "/api/console"

# Progress lines kept per job. A full run prints roughly 90-120 lines (one per batch plus
# the face/status notes), so this is generous while still bounded.
MAX_JOB_LINES = 2000
# Finished jobs are dropped from memory after this long; the page polls until it sees a
# terminal state, so nothing of value is lost.
JOB_TTL_SECONDS = 30 * 60


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


class ConsoleJob:
    """One background execution, with an append-only tail of progress lines."""

    def __init__(self, job_id: str, token: str, *, dry_run: bool, campus: str) -> None:
        self.id = job_id
        self.token = token
        self.dry_run = dry_run
        self.campus = campus
        self.status = "running"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.result: dict[str, Any] | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._seq = 0
        # How much of the bridge's progress this job has already copied.
        self.seen_lines = 0

    def note(self, message: str) -> None:
        with self._lock:
            self._lines.append(str(message))
            del self._lines[:-MAX_JOB_LINES]
            self._seq += 1

    def finish(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.result = result
            self.status = "done" if result.get("ok") else "failed"
            self.finished_at = time.time()

    def snapshot(self, *, since: int = 0) -> dict[str, Any]:
        with self._lock:
            lines = self._lines[since:] if since < len(self._lines) else []
            total = len(self._lines)
            payload = {
                "job_id": self.id,
                "status": self.status,
                "dry_run": self.dry_run,
                "campus": self.campus,
                "total_lines": total,
                "lines": lines,
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
            }
        if self.result is not None:
            payload["result"] = self.result
        return payload


class Console:
    """Bridge + job registry behind the ``/ui`` page."""

    def __init__(
        self,
        *,
        project_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        simulation_root: str | Path | None = None,
        allow_submit: bool | None = None,
        campus: str | None = None,
        keep_runs: int | None = None,
        keep_tasklists: int | None = None,
        run_timeout_s: float | None = None,
    ) -> None:
        self.project_dir = Path(project_dir or os.environ.get("YUN_PROJECT_DIR")
                                or web.PROJECT_ROOT.parent).resolve()
        self._config_path = config_path or os.environ.get("YUN_CONFIG_PATH") or None
        self._simulation_root = simulation_root or os.environ.get("YUN_SIMULATION_ROOT") or None
        self.allow_submit = _truthy(os.environ.get("YUN_ALLOW_SUBMIT"), True) \
            if allow_submit is None else bool(allow_submit)
        # Same default as the host integrations: the custom campus.
        self.campus = str(campus or os.environ.get("YUN_CAMPUS") or "else").strip().lower()
        self.keep_runs = int(keep_runs if keep_runs is not None
                             else os.environ.get("YUN_KEEP_RUNS", 10))
        self.keep_tasklists = int(keep_tasklists if keep_tasklists is not None
                                  else os.environ.get("YUN_KEEP_TASKLISTS", 10))
        self.run_timeout_s = float(run_timeout_s if run_timeout_s is not None
                                   else os.environ.get("YUN_RUN_TIMEOUT", 2700))
        self._bridge = None
        self._bridge_lock = threading.Lock()
        self._jobs: dict[str, ConsoleJob] = {}
        self._jobs_lock = threading.Lock()
        self._active_job: str | None = None

    # ------------------------------------------------------------------ bridge

    def bridge(self):
        """Build the bridge on first use so a broken config fails in a request, not at import.

        The face source comes from the face library's activation pointer
        (``face_lib.active_config()``) -- the same photo/detection/mirror triple the
        host integrations used to pass, now sourced from data/face_library/active.json.
        """
        with self._bridge_lock:
            if self._bridge is None:
                import sys

                if str(self.project_dir) not in sys.path:
                    sys.path.insert(0, str(self.project_dir))
                from mcp_bridge import YunLocalBridge

                face_config = None
                try:
                    from face_lib import active_config

                    face_config = active_config()
                except Exception:  # noqa: BLE001 - no library / no deps: run without face
                    face_config = None

                self._bridge = YunLocalBridge(
                    self.project_dir,
                    self._config_path,
                    self._simulation_root,
                    allow_submit=self.allow_submit,
                    keep_runs=self.keep_runs,
                    keep_tasklists=self.keep_tasklists,
                    face_config=face_config,
                )
            return self._bridge

    def reset_bridge(self):
        """Drop the cached bridge so its next use rebuilds with fresh configuration.

        Called by the admin page after the face library activation changes: the bridge
        reads face_config at construction (same timing as the plugin), so rebuilding is
        how a newly activated photo reaches the next execution.
        """
        with self._bridge_lock:
            self._bridge = None

    # ------------------------------------------------------------------ reads

    def status(self) -> dict[str, Any]:
        """Local status for the header. Never talks to the school server."""
        try:
            bridge = self.bridge()
            payload = bridge.status()
        except Exception as exc:  # noqa: BLE001 - the page must still render
            return {"ok": False, "error": {"code": "bridge_unavailable",
                                           "message": self._bridge_error(exc)},
                    "console": self._console_meta()}
        payload["console"] = self._console_meta()
        return payload

    def _bridge_error(self, exc: Exception) -> str:
        """Turn the bridge's construction failures into something the page can show.

        A fresh clone has no ``config.ini`` (it is git-ignored), and the raw exception for
        that is just the file path -- useless as a page header. Say what to do instead.
        """
        if isinstance(exc, FileNotFoundError):
            return (f"还没有 {Path(self.project_dir, 'config.ini').name}：在 /admin 里保存一次"
                    f"配置会以 config.example.ini 为模板自动建，也可以手动复制 "
                    f"config.example.ini 到 config.ini（{exc}）")
        return str(exc)

    def _console_meta(self) -> dict[str, Any]:
        return {
            "campus": self.campus,
            "allow_submit": self.allow_submit,
            "run_timeout_s": self.run_timeout_s,
            "keep_runs": self.keep_runs,
            "keep_tasklists": self.keep_tasklists,
            "project_dir": str(self.project_dir),
        }

    def simulations(self, limit: int = 10) -> dict[str, Any]:
        """Historical trajectories, each enriched with its cached card if one exists.

        The bridge's own list only knows the manifest; the page needs the card path and
        the plan metrics to render the picker, so the detail read is folded in here.
        """
        try:
            result = self.bridge().list_simulations(limit)
            if not result.get("ok"):
                return result
            simulations = []
            for entry in result.get("simulations", []):
                run_id = entry.get("run_id")
                card_url = None
                try:
                    if (self.bridge().simulation_root / str(run_id) / "card.png").is_file():
                        card_url = f"{API_PREFIX}/runs/{run_id}/card.png"
                except Exception:  # noqa: BLE001 - a missing card is not an error
                    card_url = None
                entry = dict(entry)
                entry["card_url"] = card_url
                entry["distance_km"] = round((entry.get("distance_m") or 0.0) / 1000.0, 3)
                entry["duration_min"] = round((entry.get("duration_seconds") or 0.0) / 60.0, 2)
                simulations.append(entry)
            return {"ok": True, "count": len(simulations), "simulations": simulations}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "list_failed", "message": str(exc)}}

    def detail(self, run_id: str) -> dict[str, Any]:
        try:
            result = self.bridge().simulation_detail(run_id)
            if not result.get("ok"):
                return result
            manifest = result.get("manifest", {})
            plan = (manifest.get("quality", {}) or {}).get("run_plan", {}) or {}
            run = web.load_run(str(run_id))
            series = run.get("series", {}) or {}
            coordinates = series.get("coordinates", []) or []
            distances = series.get("distances_m", []) or []
            return {
                "ok": True,
                "run_id": run_id,
                "manifest": manifest,
                "plan": plan,
                "card_url": f"{API_PREFIX}/runs/{run_id}/card.png",
                "series": {
                    "distance_km": round(float(distances[-1]) / 1000.0, 3) if distances else None,
                    "points": len(coordinates),
                },
                "coordinates": coordinates,
                "downloads": run.get("downloads", {}),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "detail_failed", "message": str(exc)}}

    def card_bytes(self, run_id: str) -> bytes:
        """Render on demand, then return the PNG. Raises FileNotFoundError if impossible."""
        path = self.bridge().simulation_root / str(run_id) / "card.png"
        if not path.is_file():
            web.card_metadata(str(run_id))
        return Path(web._run_directory(str(run_id)) / "card.png").read_bytes()

    def pending(self) -> dict[str, Any]:
        try:
            result = self.bridge().pending_runs()
            active = self.bridge().run_state()
            if active is not None:
                # The bridge owns the run; the page uses this to show what is really
                # happening even if it never saw the job that started it (bot-started).
                result["active_run"] = {
                    "status": active.get("status"),
                    "token": active.get("token"),
                    "dry_run": active.get("dry_run"),
                    "elapsed_seconds": active.get("elapsed_seconds"),
                    "line_count": len(active.get("lines") or []),
                }
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "pending_failed", "message": str(exc)}}

    # ------------------------------------------------------------------ actions

    def generate(
        self,
        *,
        seed: int | None = None,
        distance_km: float | None = None,
        pace_min_per_km: float | None = None,
        candidates: int = 8,
        campus: str | None = None,
    ) -> dict[str, Any]:
        """Stage one table + card (the 'generate a trajectory' button).

        ``prepare_run`` prunes the caches before writing, so the retention limit holds
        without the caller doing anything.
        """
        try:
            result = self.bridge().prepare_run(
                campus=campus or self.campus,
                distance_km=distance_km,
                pace_min_per_km=pace_min_per_km,
                candidates=candidates,
                seed=seed,
                render_card=True,
            )
            if not result.get("ok"):
                return result
            result["card_url"] = f"{API_PREFIX}/runs/{result.get('card', {}).get('run_id')}/card.png"
            result["console"] = self._console_meta()
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "generate_failed", "message": str(exc)}}

    def start_run(self, token: str, *, dry_run: bool, confirmed: bool,
                  is_drift: bool = False, campus: str | None = None) -> dict[str, Any]:
        """Kick off a run and return the job handle the page polls.

        The execution itself lives on a bridge thread (``start_background_run``) rather
        than in this server, because the run outlives whichever request or tool call
        started it -- hosts cancel tool calls after 60 seconds, and a real record takes
        twenty minutes. This method only attaches a *watcher* that copies the bridge's
        progress lines into a job buffer the page can poll incrementally.

        ``confirmed`` is threaded straight through to the bridge, which is what actually
        refuses an unconfirmed run -- this method never hard-codes it to True.
        """
        token = str(token or "").strip()
        if not token:
            return {"ok": False, "error": {"code": "missing_token",
                                           "message": "需要先准备一次跑步（生成轨迹并核验）"}}
        # Restate the gate here so the caller gets a reason up front instead of a job that
        # dies a moment later on a worker thread. 下面给 bridge 传的 confirmed=True 是
        # 「已经过上面这道校验」的意思——真正拦未确认的是这个 if，不是服务端。
        if not confirmed:
            return {"ok": False, "error": {
                "code": "confirmation_required",
                "message": "未经确认不能开跑：请在核对卡片后点「确认开跑」"}}

        with self._jobs_lock:
            existing = self._jobs.get(self._active_job) if self._active_job else None
            if existing is not None and existing.status == "running" and existing.token == token:
                # Reattaching is fine: the page may have been reloaded mid-run.
                return {"ok": True, "job_id": existing.id, "dry_run": existing.dry_run,
                        "status": existing.status, "reattached": True,
                        "poll": f"{API_PREFIX}/jobs/{existing.id}"}

        started = self.bridge().start_background_run(
            token, confirmed=True, dry_run=bool(dry_run), is_drift=is_drift,
            timeout_s=self.run_timeout_s)
        if not started.get("ok"):
            return started

        job = ConsoleJob(secrets.token_hex(6), token, dry_run=bool(dry_run),
                         campus=str(campus or self.campus))
        with self._jobs_lock:
            self._jobs[job.id] = job
            self._active_job = job.id
        threading.Thread(target=self._watch_run, args=(job,), name=f"yun-console-{job.id}",
                         daemon=True).start()
        return {"ok": True, "job_id": job.id, "dry_run": job.dry_run,
                "status": job.status, "poll": f"{API_PREFIX}/jobs/{job.id}"}

    def _watch_run(self, job: ConsoleJob) -> None:
        """Copy the bridge's run progress into this job until the run settles.

        Polling rather than callbacks because the run is owned by the bridge and may have
        been started through another door (the bot); reading its state keeps one source of
        truth for what is actually happening.
        """
        bridge = self.bridge()
        terminal = {"done", "failed", "aborted"}
        while True:
            state = bridge.run_state() or {}
            for line in (state.get("lines") or [])[job.seen_lines:]:
                job.note(line)
            job.seen_lines = len(state.get("lines") or [])
            status = state.get("status")
            if status in terminal:
                job.finish(state.get("result") or {
                    "ok": False,
                    "error": {"code": "unknown_result", "message": "运行结束但未取到结果"}})
                break
            if state.get("token") and state.get("token") != job.token:
                # Another run took over (the previous one was abandoned); stop watching.
                job.finish({"ok": False, "error": {
                    "code": "superseded", "message": "该次执行的进度已被新任务取代"}})
                break
            time.sleep(0.5)
        self._sweep_jobs()
        with self._jobs_lock:
            if self._active_job == job.id:
                self._active_job = None

    def job(self, job_id: str, since: int = 0) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(str(job_id))
        if job is None:
            return {"ok": False, "error": {"code": "unknown_job", "message": "任务不存在或已过期"}}
        return {"ok": True, **job.snapshot(since=max(0, int(since)))}

    def _active_snapshot(self) -> dict[str, Any] | None:
        """The bridge's own view of the run in flight (single source of truth)."""
        state = self.bridge().run_state()
        if state is None:
            return None
        return {"status": state.get("status"), "token": state.get("token")}

    def _sweep_jobs(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        with self._jobs_lock:
            for job_id, job in list(self._jobs.items()):
                if job.finished_at and job.finished_at < cutoff:
                    self._jobs.pop(job_id, None)

    def cancel(self, token: str) -> dict[str, Any]:
        """Cancel a pending (not yet started) token."""
        try:
            return self.bridge().cancel_run(str(token))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "cancel_failed", "message": str(exc)}}

    def abort(self, token: str = "") -> dict[str, Any]:
        """Stop the run in flight (no finish) and optionally drop a pending token.

        Works across processes: the run may have been started by the bot's MCP server, and
        the abort is published through the shared state file so whichever process owns the
        worker sees it.
        """
        try:
            return self.bridge().abort_run(str(token or ""))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "abort_failed", "message": str(exc)}}

    # ------------------------------------------------------------------ routing

    def handle_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any, str]:
        """Return ``(status, payload, content_type)``; payload may be raw bytes."""
        if path == CONSOLE_PATH:
            return 200, CONSOLE_HTML.encode("utf-8"), "text/html; charset=utf-8"
        if path == f"{API_PREFIX}/status":
            return 200, self.status(), "application/json"
        if path == f"{API_PREFIX}/simulations":
            limit = self._int_query(query, "limit", 10, 1, 100)
            return 200, self.simulations(limit), "application/json"
        if path == f"{API_PREFIX}/pending":
            return 200, self.pending(), "application/json"
        if path.startswith(f"{API_PREFIX}/jobs/"):
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 4:
                since = self._int_query(query, "since", 0, 0, 1_000_000)
                payload = self.job(parts[3], since)
                return (200 if payload.get("ok") else 404), payload, "application/json"
        if path.startswith(f"{API_PREFIX}/runs/"):
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 4:
                return 200, self.detail(parts[3]), "application/json"
            if len(parts) == 5 and parts[4] == "card.png":
                try:
                    return 200, self.card_bytes(parts[3]), "image/png"
                except FileNotFoundError:
                    return 404, {"error": "run or card not found"}, "application/json"
        return 404, {"error": "not found"}, "application/json"

    def handle_post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        if path == f"{API_PREFIX}/generate":
            result = self.generate(
                seed=self._opt_int(payload, "seed"),
                distance_km=self._opt_float(payload, "distance_km"),
                pace_min_per_km=self._opt_float(payload, "pace_min_per_km"),
                candidates=int(payload.get("candidates") or 8),
                campus=payload.get("campus"),
            )
            return (201 if result.get("ok") else 400), result
        if path == f"{API_PREFIX}/forget-lost":
            # 失联判定来自共享状态文件，清掉它才有恢复路径（见 bridge.forget_lost_run）。
            return 200, self.bridge().forget_lost_run()
        if path == f"{API_PREFIX}/run":
            result = self.start_run(
                str(payload.get("token") or ""),
                dry_run=bool(payload.get("dry_run")),
                confirmed=payload.get("confirmed") is True,
                is_drift=bool(payload.get("is_drift")),
                campus=payload.get("campus"),
            )
            return (202 if result.get("ok") else 400), result
        if path == f"{API_PREFIX}/cancel":
            result = self.cancel(str(payload.get("token") or ""))
            return (200 if result.get("ok") else 400), result
        if path == f"{API_PREFIX}/abort":
            result = self.abort(str(payload.get("token") or ""))
            return (200 if result.get("ok") else 400), result
        return 404, {"error": "not found"}

    @staticmethod
    def _int_query(query: dict[str, list[str]], key: str, default: int,
                   minimum: int, maximum: int) -> int:
        raw = (query.get(key) or [None])[0]
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _opt_int(payload: dict[str, Any], key: str) -> int | None:
        raw = payload.get(key)
        if raw in (None, ""):
            return None
        return int(raw)

    @staticmethod
    def _opt_float(payload: dict[str, Any], key: str) -> float | None:
        raw = payload.get(key)
        if raw in (None, ""):
            return None
        return float(raw)


def parse_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    # Accept a body up to 64 KiB; anything larger is a mistake, not a legitimately big
    # request (every payload here is a handful of scalars).
    if len(raw) > 65_536:
        raise ValueError("request body too large")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("request body must be a JSON object")
    return document


from .ui import render_page

CONSOLE_HTML = render_page("console")
