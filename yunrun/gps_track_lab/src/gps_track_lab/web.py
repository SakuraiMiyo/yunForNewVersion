from __future__ import annotations

import csv
import json
import math
import mimetypes
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .calibration import load_calibration
from .generator import (
    SimulationSettings,
    generate_best_candidate,
    load_settings,
    write_outputs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VIEWER_PATH = PROJECT_ROOT / "track_viewer.html"
TRACK_PATH = PROJECT_ROOT / "config" / "track.json"
SETTINGS_PATH = PROJECT_ROOT / "config.example.json"
RUNS_PATH = PROJECT_ROOT / "runs"
TOOLS_PATH = PROJECT_ROOT / "tools"
_generation_lock = threading.Lock()


def _bounded_number(
    payload: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = payload.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _parse_seed(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ValueError("seed must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("seed must be an integer") from exc
    if not 0 <= value < 2**63:
        raise ValueError("seed must be between 0 and 2^63-1")
    return value


def _settings_from_payload(payload: dict[str, Any]) -> SimulationSettings:
    settings = load_settings(SETTINGS_PATH)
    overrides = payload.get("settings")
    if overrides is None:
        return settings
    if not isinstance(overrides, dict):
        raise ValueError("settings must be an object")

    values = asdict(settings)
    numeric_fields = set(SimulationSettings.__dataclass_fields__) - {"coordinate_system"}
    for key, raw in overrides.items():
        if key not in numeric_fields:
            raise ValueError(f"unsupported setting: {key}")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number") from exc
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        values[key] = value
    result = SimulationSettings.from_dict(values)
    result.validate()
    return result


def _run_directory(run_id: str) -> Path:
    if not run_id or run_id != Path(run_id).name or not run_id.startswith("synthetic-"):
        raise ValueError("invalid run id")
    directory = (RUNS_PATH / run_id).resolve()
    directory.relative_to(RUNS_PATH.resolve())
    if not directory.is_dir():
        raise FileNotFoundError(run_id)
    return directory


def _series_from_csv(path: Path) -> dict[str, list[Any]]:
    coordinates: list[list[float]] = []
    timestamps: list[str] = []
    speeds: list[float] = []
    distances: list[float] = []
    laps: list[int] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            coordinates.append([float(row["longitude"]), float(row["latitude"])])
            timestamps.append(row["timestamp"])
            speeds.append(float(row["modeled_speed_mps"]))
            distances.append(float(row["modeled_distance_m"]))
            laps.append(int(row["lap"]))
    return {
        "coordinates": coordinates,
        "timestamps": timestamps,
        "speeds_mps": speeds,
        "distances_m": distances,
        "laps": laps,
    }


def load_run(run_id: str) -> dict[str, Any]:
    directory = _run_directory(run_id)
    with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    series = _series_from_csv(directory / "track.csv")
    started = datetime.fromisoformat(series["timestamps"][0].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(series["timestamps"][-1].replace("Z", "+00:00"))
    base_url = f"/api/runs/{quote(run_id)}"
    result = {
        "run_id": run_id,
        "synthetic": True,
        "seed": manifest["seed"],
        "point_count": len(series["coordinates"]),
        "duration_seconds": (ended - started).total_seconds(),
        "modeled_distance_m": series["distances_m"][-1],
        "quality": manifest.get("quality", {}),
        "calibration": manifest.get("calibration", {}),
        "settings": manifest.get("settings", {}),
        "series": series,
        "downloads": {
            "csv": f"{base_url}/track.csv",
            "geojson": f"{base_url}/track.geojson",
            "manifest": f"{base_url}/manifest.json",
            "card": f"{base_url}/card.png",
        },
    }
    # A card is rendered from the run itself, so the plan must survive a restart: read it
    # back out of the manifest rather than relying on the generating request's memory.
    plan = manifest.get("quality", {}).get("run_plan")
    if isinstance(plan, dict):
        result["run_plan"] = plan
    return result


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    if not RUNS_PATH.exists():
        return []
    items = []
    directories = sorted(
        (path for path in RUNS_PATH.iterdir() if path.is_dir() and path.name.startswith("synthetic-")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for directory in directories[:limit]:
        try:
            with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            quality = manifest.get("quality", {})
            items.append({
                "run_id": directory.name,
                "seed": manifest.get("seed"),
                "point_count": manifest.get("point_count"),
                "valid": bool(quality.get("valid")),
                "score": quality.get("score"),
                "modeled_distance_m": manifest.get("modeled_distance_m"),
                "duration_seconds": manifest.get("duration_seconds"),
            })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return items


def run_plan_enabled(payload: dict[str, Any]) -> bool:
    """Whether this request should go through the 健康跑 business layer.

    The viewer's own form posts explicit distance/pace, which keeps the raw simulator
    behaviour. Turning the toggle on (or posting an explicit ``run_plan: true``) routes
    the request through ``tools/tasklist_builder.py`` instead, so distance, duration,
    cadence and stride follow the measured real-run distributions.
    """
    if "run_plan" in payload:
        return bool(payload.get("run_plan"))
    return False


def _tasklist_module():
    """Import the business layer (``tools/tasklist_builder.py``).

    Searched rather than hard-coded: this file lives inside the lab, while the business
    layer sits at the project root (``<project>/tools``). A fixed ``TOOLS_PATH`` broke the
    moment the tooling moved, so probe both layouts instead.
    """
    import sys

    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        for candidate in (parent / "tools", parent / "gps_track_lab" / "tools",
                          parent.parent / "tools"):
            if (candidate / "tasklist_builder.py").is_file():
                path = str(candidate)
                if path not in sys.path:
                    sys.path.insert(0, path)
                import tasklist_builder

                return tasklist_builder
    raise ModuleNotFoundError(f"tasklist_builder not found near {here}")


RUN_PLAN_PANEL = """
<section class="panel" id="run-plan-panel" aria-labelledby="run-plan-title">
  <h2 id="run-plan-title">健康跑口径（本地业务规则）</h2>
  <label class="rp-row" for="run-plan-toggle">
    <input id="run-plan-toggle" type="checkbox" checked>
    <span>按口径随机生成（距离 &gt; 3 km、用时 16~25 min）</span>
  </label>
  <div class="rp-note" id="run-plan-note">
    开启后忽略上面的距离/配速输入，按真实记录分布抽取距离、配速、步频；步幅由
    「平均速度 × 60 / 步频」推导，因此里程 / 用时 / 步数 / 步频互相自洽。
  </div>
  <div class="rp-metrics" id="run-plan-metrics" hidden>
    <span id="rp-pace">—</span><span id="rp-cadence">—</span><span id="rp-stride">—</span>
    <span id="rp-qualified">—</span>
  </div>
  <button class="primary" id="card-btn" type="button" disabled>生成轨迹卡片图片</button>
  <div class="rp-note" id="card-note">先生成一条轨迹，再点这里把它渲染成一张 PNG 卡片。</div>
  <div id="card-output" hidden>
    <img id="card-image" alt="轨迹卡片">
    <a class="rp-download" id="card-download" href="#" download>下载 PNG</a>
  </div>
</section>
"""

RUN_PLAN_STYLE = """
<style>
#run-plan-panel .rp-row { display:flex; align-items:center; gap:8px; font-size:.85rem; color:#d7dce6; cursor:pointer; }
#run-plan-panel .rp-row input { width:16px; height:16px; accent-color:var(--accent); }
#run-plan-panel .rp-note { margin-top:8px; color:var(--muted); font-size:.76rem; line-height:1.5; }
#run-plan-panel .rp-metrics { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; font-size:.76rem; }
#run-plan-panel .rp-metrics span { background:#151a24; border:1px solid var(--line); border-radius:6px; padding:3px 7px; color:#c6cddb; }
#run-plan-panel .rp-metrics .rp-ok { color:var(--cyan); border-color:var(--cyan); }
#run-plan-panel .rp-metrics .rp-bad { color:var(--danger); border-color:var(--danger); }
#run-plan-panel #card-btn { margin-top:12px; }
#run-plan-panel #card-output { margin-top:10px; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:#0b0f16; }
#run-plan-panel #card-image { display:block; width:100%; height:auto; }
#run-plan-panel .rp-download { display:block; padding:6px 8px; font-size:.76rem; color:var(--cyan); text-decoration:none; }
</style>
"""

RUN_PLAN_SCRIPT = """
(function () {
  var search = new URLSearchParams(location.search);
  var toggle = document.getElementById("run-plan-toggle");
  var button = document.getElementById("generate-btn");
  var cardButton = document.getElementById("card-btn");
  var cardNote = document.getElementById("card-note");
  var cardOutput = document.getElementById("card-output");
  var cardImage = document.getElementById("card-image");
  var cardDownload = document.getElementById("card-download");
  var lastRunId = null;
  if (search.get("runplan") === "off" && toggle) { toggle.checked = false; }

  function label() {
    if (button) { button.textContent = toggle && toggle.checked ? "按口径生成并显示" : "生成并显示"; }
  }
  if (toggle) { toggle.addEventListener("change", label); label(); }

  function remember(runId) {
    if (!runId) { return; }
    lastRunId = runId;
    if (cardButton) { cardButton.disabled = false; }
    if (cardNote) { cardNote.textContent = "可以渲染卡片了：" + runId; }
  }

  if (cardButton) {
    cardButton.addEventListener("click", function () {
      if (!lastRunId) { return; }
      cardButton.disabled = true;
      cardButton.textContent = "正在渲染卡片…";
      if (cardNote) { cardNote.textContent = "抓取底图并合成画面（首次会稍慢）…"; }
      var url = "/api/runs/" + encodeURIComponent(lastRunId) + "/card";
      // 写接口在服务端是统一的闸门（要求 Content-Type 恰好是 application/json），
      // 所以这条无 body 的 POST 也得显式带上，否则会被自己的闸门拒成 403。
      window.fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } })
        .then(function (r) {
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.json();
        })
        .then(function (meta) {
          // Cache-bust: the PNG path is stable per run, so a plain reload could show
          // the previous render of the same run.
          var src = "/api/runs/" + encodeURIComponent(lastRunId) + "/card.png?t=" + Date.now();
          if (cardImage) { cardImage.src = src; }
          if (cardDownload) { cardDownload.href = src; }
          if (cardOutput) { cardOutput.hidden = false; }
          if (cardNote) {
            cardNote.textContent = "卡片已生成：" + (meta.width || 1080) + "×" + (meta.height || 1500)
              + " · " + Math.round((meta.png_bytes || 0) / 1024) + " KB · 底图 "
              + (meta.tiles || 0) + " 张（" + meta.render_seconds + "s）";
          }
        })
        .catch(function (error) {
          if (cardNote) { cardNote.textContent = "卡片渲染失败：" + error.message; }
        })
        .then(function () {
          cardButton.disabled = false;
          cardButton.textContent = "重新生成卡片图片";
        });
    });
  }

  var originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var isGenerate = url.indexOf("/api/generate") !== -1 && init && init.body;
    if (!isGenerate || !toggle || !toggle.checked) {
      return originalFetch(input, init).then(function (response) {
        if (isGenerate && response.ok) {
          response.clone().json().then(function (doc) { remember(doc.run_id); });
        }
        return response;
      });
    }
    var payload;
    try { payload = JSON.parse(init.body); } catch (e) { return originalFetch(input, init); }
    payload.run_plan = true;
    // Let the business layer sample distance and pace itself.
    delete payload.distance_km;
    delete payload.pace_min_per_km;
    return originalFetch(input, Object.assign({}, init, { body: JSON.stringify(payload) }))
      .then(function (response) {
        if (!response.ok) { return response; }
        return response.clone().json().then(function (doc) {
          remember(doc.run_id);
          var plan = doc.run_plan || {};
          var box = document.getElementById("run-plan-metrics");
          if (box) { box.hidden = false; }
          var set = function (id, text) { var el = document.getElementById(id); if (el) { el.textContent = text; } };
          set("rp-pace", "配速 " + (plan.pace_min_per_km || "?") + " min/km");
          set("rp-cadence", "步频 " + (plan.cadence || "?") + " 步/分");
          set("rp-stride", "步幅 " + (plan.stride_m || "?") + " m");
          set("rp-qualified", plan.qualified ? "合格 ✓" : "不合格 ✗");
          var badge = document.getElementById("rp-qualified");
          if (badge) { badge.className = plan.qualified ? "rp-ok" : "rp-bad"; }
          return response;
        });
      });
  };
})();
"""


def render_viewer() -> bytes:
    """Serve the viewer with the business-rule panel injected.

    The existing business panel and fetch adapter are retained. Shared Studio styling
    is applied last, so the viewer matches the admin and running workspace.
    """
    markup = VIEWER_PATH.read_text(encoding="utf-8")
    form_anchor = '<form id="generator-form">'
    if form_anchor in markup:
        markup = markup.replace(form_anchor, RUN_PLAN_PANEL + form_anchor, 1)
    if "</head>" in markup:
        markup = markup.replace("</head>", RUN_PLAN_STYLE + "</head>", 1)
    if "</body>" in markup:
        markup = markup.replace("</body>", "<script>" + RUN_PLAN_SCRIPT + "</script>\n</body>", 1)
    from .ui import style_markup

    return style_markup(markup).encode("utf-8")


def _card_module():
    """Import the card renderer.

    ``track_card`` sits at the project root (a sibling of ``gps_track_lab``), while
    ``PROJECT_ROOT`` in this module is the lab directory itself -- so search upwards for
    the package instead of assuming a fixed number of parents. Both layouts are probed so
    the renderer keeps working whether it sits next to the lab or one level above it.
    """
    import sys

    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        for candidate in (parent, parent / "gps_track_lab"):
            if (candidate / "track_card" / "__init__.py").is_file():
                path = str(candidate)
                if path not in sys.path:
                    sys.path.insert(0, path)
                import track_card

                return track_card
    raise ModuleNotFoundError(f"track_card package not found near {here}")


def _pace_trace(series: dict[str, Any], quality: dict[str, Any] | None = None) -> list[float]:
    """Per-point pace (min/km) for the card's chart.

    Prefers the trace recorded at generation time: that is the pace that was actually
    submitted, while the CSV's ``modeled_speed_mps`` is the same value with the simulation
    noise still on it (it spans 2-21 min/km and renders as a hairy band instead of the
    run/walk shape). The CSV path stays as a fallback so older runs remain drawable.
    """
    recorded = (quality or {}).get("pace_trace")
    if recorded:
        trace: list[float] = []
        for value in recorded:
            try:
                trace.append(round(float(value), 3))
            except (TypeError, ValueError):
                continue
        if len(trace) >= 4:
            return trace
    speeds = series.get("speeds_mps")
    if not speeds:
        return []
    trace = []
    for speed in speeds:
        try:
            value = abs(float(speed))
        except (TypeError, ValueError):
            continue
        if value > 0.2:
            trace.append(round(1000.0 / (60.0 * value), 3))
    return trace


def card_metadata(run_id: str) -> dict[str, Any]:
    """Render the share card for a run and return where it landed.

    The card is built from the run's own CSV plus its manifest, so it can be produced at
    any time after the fact and not only at generation time.
    """
    directory = _run_directory(run_id)
    with (directory / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    series = _series_from_csv(directory / "track.csv")
    plan = manifest.get("quality", {}).get("run_plan", {}) or {}
    calibration = manifest.get("calibration", {})

    card = _card_module()
    distance_m = float(series["distances_m"][-1])
    duration_s = float(manifest.get("duration_seconds") or 0.0)
    distance_km = distance_m / 1000.0
    lap_length = calibration.get("lap_length_m")
    points = [(lon, lat) for lon, lat in series["coordinates"]]

    started = datetime.fromisoformat(series["timestamps"][0].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(series["timestamps"][-1].replace("Z", "+00:00"))
    local = started.astimezone()

    cadence = plan.get("cadence")
    stride = plan.get("stride_m")
    steps = plan.get("step_total")
    if not steps and cadence and duration_s:
        steps = int(round(float(cadence) * duration_s / 60.0))
    if not stride and steps:
        stride = distance_m / steps

    data = card.CardData(
        points=points,
        title="健康跑",
        subtitle=f"{run_id}",
        started_at=local.strftime("%Y-%m-%d %H:%M"),
        finished_at=ended.astimezone().strftime("%H:%M"),
        distance_km=distance_km,
        duration_s=duration_s,
        pace_min_per_km=(duration_s / 60.0) / distance_km if distance_km else None,
        cadence=cadence,
        stride_m=stride,
        step_total=steps,
        laps=(distance_m / float(lap_length)) if lap_length else None,
        lap_length_m=float(lap_length) if lap_length else None,
        seed=manifest.get("seed"),
        pace_trace=_pace_trace(series, manifest.get("quality")),
        qualified=plan.get("qualified"),
        labels=["本地仿真 · 不用于上传"],
        footer_note="synthetic",
    )

    output = directory / "card.png"
    meta = card.render_card(data, output)
    meta["run_id"] = run_id
    meta["url"] = f"/api/runs/{quote(run_id)}/card.png"
    return meta


def generate_run_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate one record under the local 健康跑 rule (distance > 3 km, <= 30 min)."""
    builder = _tasklist_module()
    candidates_value = _bounded_number(payload, "candidates", 8, 1, 100)
    if not candidates_value.is_integer():
        raise ValueError("candidates must be an integer")
    seed = _parse_seed(payload.get("seed"))

    duration_range = builder.GEN_DURATION_MIN_RANGE
    distance_range = builder.GEN_DISTANCE_KM_RANGE
    raw_duration = payload.get("duration_min")
    if raw_duration not in (None, ""):
        value = _bounded_number(payload, "duration_min", duration_range[0], 1.0, 240.0)
        duration_range = (value, value)

    settings = _settings_from_payload(payload)
    document, plan, quality = builder.build_tasklist(
        seed=seed,
        candidates=int(candidates_value),
        dislikes=3,
        calibration_path=str(TRACK_PATH),
        settings_path=str(SETTINGS_PATH),
        duration_range=duration_range,
        distance_range=distance_range,
    )

    points = quality.pop("_points", [])
    if not points:
        raise RuntimeError("business layer returned no trajectory points")
    summary = {
        "distance_km": round(plan.distance_m / 1000.0, 3),
        "duration_s": round(plan.duration_s, 1),
        "duration_min": round(plan.duration_s / 60.0, 2),
        "pace_min_per_km": round(plan.pace_min_per_km, 2),
        "cadence": document["data"]["recodeCadence"],
        "stride_m": round(plan.stride_m, 3),
        "step_total": plan.step_total,
        "qualified": bool(plan.qualified),
    }
    quality["run_plan"] = summary
    output = write_outputs(
        points,
        RUNS_PATH,
        plan.seed,
        load_calibration(TRACK_PATH),
        settings,
        quality=quality,
    )
    run = load_run(output.name)
    run["run_plan"] = summary
    return run


def generate_run(payload: dict[str, Any]) -> dict[str, Any]:
    distance_km = _bounded_number(payload, "distance_km", 3.05, 0.1, 50.0)
    pace = _bounded_number(payload, "pace_min_per_km", 6.0, 2.0, 30.0)
    candidates_value = _bounded_number(payload, "candidates", 8, 1, 100)
    if not candidates_value.is_integer():
        raise ValueError("candidates must be an integer")
    candidates = int(candidates_value)
    seed = _parse_seed(payload.get("seed"))
    calibration = load_calibration(TRACK_PATH)
    settings = _settings_from_payload(payload)

    points, selected_seed, quality = generate_best_candidate(
        calibration=calibration,
        settings=settings,
        distance_m=distance_km * 1000.0,
        pace_min_per_km=pace,
        candidates=candidates,
        seed=seed,
    )
    output = write_outputs(
        points,
        RUNS_PATH,
        selected_seed,
        calibration,
        settings,
        quality=quality,
    )
    return load_run(output.name)


def initial_state() -> dict[str, Any]:
    """Preview-page state.

    ``config/track.json`` only exists after a calibration, and calibrating is the first
    thing the guided flow asks for -- so on a fresh clone the file is legitimately absent.
    It used to be read unconditionally, which raised FileNotFoundError here, turned
    ``/api/state`` into a 404 and took the whole viewer page down before the user had a
    chance to calibrate. Degrade to ``calibration: None`` with a hint instead.
    """
    settings = load_settings(SETTINGS_PATH)
    calibration = None
    calibration_error = None
    try:
        calibration = load_calibration(TRACK_PATH)
    except FileNotFoundError:
        calibration_error = "还没有标定（config/track.json 不存在）——先到 /admin 走一遍标定"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        calibration_error = f"标定文件读不出来：{exc}"
    return {
        "ready": calibration is not None,
        "local_only": True,
        "calibration": calibration.to_dict() if calibration is not None else None,
        "calibration_error": calibration_error,
        "settings": asdict(settings),
        "defaults": {
            "distance_km": 3.05,
            "pace_min_per_km": 6.0,
            "candidates": 8,
        },
        "runs": list_runs(),
    }


_console_instance = None
_console_lock = threading.Lock()


def console_instance():
    """Lazily build the running console.

    Deferred so that importing this module (and serving the preview page) never depends on
    the bridge finding a usable ``config.ini``. The console is a sibling module that owns
    ``/ui``; keeping the import inside the router means the two can be developed apart.
    """
    global _console_instance
    with _console_lock:
        if _console_instance is None:
            from .console import Console

            _console_instance = Console(project_dir=PROJECT_ROOT.parent)
        return _console_instance


def console_route(path: str) -> bool:
    from .console import API_PREFIX, CONSOLE_PATH

    return path == CONSOLE_PATH or path.startswith(API_PREFIX)


_admin_instance = None
_admin_lock = threading.Lock()


def admin_instance():
    """Lazily build the guided admin console (owner of ``/admin``).

    Shares the console instance so the two pages sit on one bridge: tokens, run
    state and the face-library activation pointer are the same objects.
    """
    global _admin_instance
    with _admin_lock:
        if _admin_instance is None:
            from .admin import ADMIN_PATH, API_PREFIX, Admin

            _admin_instance = Admin(console_instance())
        return _admin_instance


def admin_route(path: str) -> bool:
    from .admin import ADMIN_PATH, API_PREFIX

    return path == ADMIN_PATH or path.startswith(API_PREFIX)


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


class TrackLabHandler(BaseHTTPRequestHandler):
    server_version = "GPSTrackLab/0.2"

    def _console_request_allowed(self, *, write: bool = False,
                                 content_types: set[str] | None = None) -> bool:
        """Reject rebinding and browser cross-site requests before dispatching any action.

        ``content_types`` widens the write check for raw-body endpoints (the admin
        face upload posts image bytes, not JSON); the Host/Origin/Fetch-Metadata
        rules are identical either way.
        """
        if not _is_loopback(self.client_address[0]):
            return False
        host_headers = self.headers.get_all("Host") or []
        if len(host_headers) != 1:
            return False
        host = host_headers[0]
        try:
            target = urlparse("http://" + host)
            if target.username or target.password or target.path or target.query or target.fragment:
                return False
            if target.hostname not in {"localhost", "127.0.0.1", "::1"}:
                return False
            if (target.port or 80) != self.server.server_address[1]:
                return False
            origins = self.headers.get_all("Origin") or []
            if origins:
                if len(origins) != 1:
                    return False
                origin = urlparse(origins[0])
                if (origin.scheme != "http" or origin.netloc != target.netloc
                        or origin.path or origin.query or origin.fragment):
                    return False
            # Browser requests send Fetch Metadata even when no Origin is supplied.
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return False
        except ValueError:
            return False
        if write:
            allowed = content_types or {"application/json"}
            given = self.headers.get_all("Content-Type") or []
            if len(given) != 1 or given[0].split(";", 1)[0].strip().lower() not in allowed:
                return False
        return True

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        # SAMEORIGIN (not DENY): the guided admin page embeds /ui in an iframe on
        # the same origin; cross-origin framing stays blocked.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _send_json(self, status: int, document: Any) -> None:
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, *, download: bool = False) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(body)

    def _console_get(self, path: str, query: dict[str, list[str]]) -> None:
        status, payload, content_type = console_instance().handle_get(path, query)
        if isinstance(payload, bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send_json(status, payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if not (admin_route(path) or console_route(path)):
            # 预览路由（/、/api/state、/api/runs*、track.csv）也要过同一道闸：少了这一步，
            # DNS rebinding 能让任意网页读走标定中心与整条轨迹；用 --host 绑到非回环地址时
            # 更是整个局域网都能读。写侧一直是拦住的，读侧原本没有。
            if not self._console_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "loopback-only"})
                return
        if admin_route(path):
            # The admin page edits config.ini and holds the face library: loopback only,
            # same gate as the console.
            if not self._console_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"error": "admin is loopback-only"})
                return
            try:
                parsed = urlparse(self.path)
                status, payload, content_type = admin_instance().handle_get(
                    path, parse_qs(parsed.query))
                if isinstance(payload, bytes):
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self._send_json(status, payload)
            except Exception as exc:  # noqa: BLE001 - the admin page must not kill the viewer
                self.log_error("admin GET failed: %s", exc)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"error": f"admin failed: {exc}"})
            return
        if console_route(path):
            parsed = urlparse(self.path)
            # The console can submit real records, so it is reachable from this machine
            # only. 预览路由同样只服务本机（见 do_GET 开头那道闸）——读侧会暴露标定中心
            # 与整条轨迹，所以 `--host` 绑到非回环地址时，除本机外的客户端一律 403。
            if not self._console_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"error": "console is loopback-only"})
                return
            try:
                self._console_get(path, parse_qs(parsed.query))
            except Exception as exc:  # noqa: BLE001 - the console must not kill the viewer
                self.log_error("console GET failed: %s", exc)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"error": f"console failed: {exc}"})
            return
        try:
            if path in {"/", "/track_viewer.html"}:
                body = render_viewer()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, initial_state())
                return
            if path == "/api/runs":
                self._send_json(HTTPStatus.OK, {"runs": list_runs()})
                return
            if path.startswith("/api/runs/"):
                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) == 3:
                    self._send_json(HTTPStatus.OK, load_run(parts[2]))
                    return
                if len(parts) == 4 and parts[3] in {"track.csv", "track.geojson", "manifest.json"}:
                    self._send_file(_run_directory(parts[2]) / parts[3], download=True)
                    return
                if len(parts) == 4 and parts[3] == "card.png":
                    card_path = _run_directory(parts[2]) / "card.png"
                    if not card_path.is_file():
                        # Render on demand so any historical run can produce a card.
                        card_metadata(parts[2])
                    self._send_file(card_path)
                    return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self.log_error("card render failed: %s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "card render failed"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if not (admin_route(route) or console_route(route)):
            # 其余写接口（/api/generate、/api/runs/<id>/card）也要过同的一道闸。
            # 少了这一步，任意网页用「简单请求」（text/plain、无预检）就能盲打本机服务：
            # /api/generate 会清掉旧的 run 与 tasklist，还会拉起无头浏览器。
            if not self._console_request_allowed(write=True):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "loopback-only"})
                return
        if admin_route(route):
            from .admin import MAX_UPLOAD_BYTES

            upload = route.endswith("/face/upload")
            if not self._console_request_allowed(
                    write=True,
                    content_types=({"image/jpeg", "image/png", "image/webp",
                                    "application/octet-stream"} if upload else None)):
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"error": "admin is loopback-only"})
                return
            try:
                admin = admin_instance()
                if upload:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_UPLOAD_BYTES:
                        raise ValueError(f"upload body must be between 1 and {MAX_UPLOAD_BYTES} bytes")
                    name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
                    result = admin.face_upload(self.rfile.read(length), name)
                    self._send_json(201 if result.get("ok") else 400, result)
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if 0 < length <= 1_048_576 else b""
                    try:
                        payload = json.loads(raw.decode("utf-8")) if raw else {}
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ValueError(str(exc)) from exc
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be a JSON object")
                    status, document = admin.handle_post(route, payload)
                    self._send_json(status, document)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self.log_error("admin POST failed: %s", exc)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"error": f"admin failed: {exc}"})
            return
        if console_route(route):
            from .console import parse_json_body

            if not self._console_request_allowed(write=True):
                self._send_json(HTTPStatus.FORBIDDEN,
                                {"error": "console is loopback-only"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if 0 < length <= 65_536 else b""
                payload = parse_json_body(raw)
                status, document = console_instance().handle_post(route, payload)
                self._send_json(status, document)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - the console must not kill the viewer
                self.log_error("console POST failed: %s", exc)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"error": f"console failed: {exc}"})
            return
        if route.startswith("/api/runs/") and route.endswith("/card"):
            parts = [unquote(part) for part in route.split("/") if part]
            if len(parts) == 4:
                try:
                    self._send_json(HTTPStatus.CREATED, card_metadata(parts[2]))
                except FileNotFoundError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                except Exception as exc:  # noqa: BLE001
                    self.log_error("card render failed: %s", exc)
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "card render failed"})
                return
        if route != "/api/generate":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65_536:
                raise ValueError("request body must be between 1 and 65536 bytes")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if not _generation_lock.acquire(blocking=False):
                self._send_json(HTTPStatus.CONFLICT, {"error": "a trajectory is already being generated"})
                return
            try:
                if run_plan_enabled(payload):
                    result = generate_run_plan(payload)
                else:
                    result = generate_run(payload)
            finally:
                _generation_lock.release()
            self._send_json(HTTPStatus.CREATED, result)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.log_error("generation failed: %s", exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "generation failed"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")


def run_server(host: str = "127.0.0.1", port: int = 8437, open_browser: bool = False,
               console: bool = False, admin: bool = False) -> None:
    """Serve the preview page and (always) the running console and the guided admin page.

    ``console=True`` / ``admin=True`` only change which page the browser opens on
    start; every route is served either way, so the preview page keeps working
    exactly as before.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"[警告] 绑定到 {host}：闸门只放行本机客户端，别的机器访问页面与接口都会被拒。")
    server = ThreadingHTTPServer((host, port), TrackLabHandler)
    target = "admin" if admin else ("ui" if console else "")
    url = f"http://{host}:{port}/" + target
    print(f"GPS Track Lab viewer: http://{host}:{port}/")
    print(f"跑步控制台: http://{host}:{port}/ui")
    print(f"管理前台（引导）: http://{host}:{port}/admin")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
