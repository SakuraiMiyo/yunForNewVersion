# -*- coding: utf-8 -*-
"""Compile a run into a self-contained HTML card, then rasterise it with headless Edge.

The renderer is split in two on purpose:

* ``compile_card`` is pure: it turns a trajectory plus metrics into one HTML document
  with every tile embedded as a data URI. It can be unit tested with no browser and no
  network, and its output is stable for the same input.
* ``render_png`` shells out to headless Edge to screenshot that document.

Why not Pillow: this machine has no Pillow/matplotlib, and the card needs Chinese text
plus satellite tiles -- an HTML/SVG page in a browser does all three with the system
fonts already installed and nothing to install.

The markup is a plain ``string.Template`` (``$name`` placeholders) rather than an
f-string: CSS is full of braces, and f-string escaping turns the stylesheet into noise.
"""
from __future__ import annotations

import base64
import contextlib
import html
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from .basemap import DEFAULT_PROVIDER, Basemap
from .cdp import WS, evaluate, page_ws_url
from .tiles import choose_zoom, metres_per_pixel, plan_view, route_pixels, simplify

CARD_WIDTH = 1080
MAP_HEIGHT = 760
CARD_HEIGHT = 1500

# 截图只要求「Chromium 内核 + headless + CDP」，跟操作系统无关：
#   Windows  → 系统自带 Edge（也可能装了 Chrome）
#   macOS    → Edge / Chrome / Chromium 的 .app 内可执行文件
#   Linux    → 发行版包名各不相同，再补一层 PATH 查找
# 想强制指定就用环境变量 YUNRUN_BROWSER（旧的 EDGE_PATH 也认）。
BROWSER_CANDIDATES = (
    # Windows
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # macOS
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    # Linux
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)

# 最后一道：直接查 PATH（Linux 打包版、Homebrew 等把可执行文件放在别处时靠这条）
BROWSER_COMMANDS = (
    "msedge",
    "microsoft-edge",
    "microsoft-edge-stable",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
)

# 旧名字保留（历史代码/文档里都叫 EDGE_CANDIDATES）
EDGE_CANDIDATES = BROWSER_CANDIDATES


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])

# Metrics shown under the map: (internal key, Chinese label, source field).
METRIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("duration_text", "时长", "duration"),
    ("distance_text", "距离", "distance_km"),
    ("pace_text", "配速", "pace"),
    ("cadence_text", "步频", "cadence"),
    ("stride_text", "步幅", "stride_m"),
    ("steps_text", "步数", "step_total"),
    ("points_text", "轨迹点", "points"),
    ("speed_text", "均速", "speed_kmh"),
)


@dataclass
class CardData:
    """Everything the card needs. Missing fields render as an em dash, never crash."""

    points: list[tuple[float, float]]
    title: str = "健康跑"
    subtitle: str = ""
    started_at: str = ""
    finished_at: str = ""
    distance_km: float | None = None
    duration_s: float | None = None
    pace_min_per_km: float | None = None
    cadence: float | None = None
    stride_m: float | None = None
    step_total: int | None = None
    laps: float | None = None
    lap_length_m: float | None = None
    seed: int | None = None
    qualified: bool | None = None
    labels: list[str] = field(default_factory=list)
    footer_note: str = ""
    accent: str = "#22d3ee"
    # Per-point pace (min/km) in distance order. Drawn as the pace chart, because how the
    # pace moves through a run -- steady, or run-then-walk-and-back -- is the one thing about
    # a synthetic record that cannot be judged from the summary numbers.
    pace_trace: list[float] = field(default_factory=list)

    @property
    def point_count(self) -> int:
        return len(self.points)


def _fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _fmt(value: Any, digits: int, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}{suffix}"


def build_metric_cells(data: CardData) -> list[dict[str, str]]:
    """Format every metric cell. Pure, so the layout can be checked without a browser."""
    speed_kmh = None
    if data.distance_km and data.duration_s:
        speed_kmh = data.distance_km / (data.duration_s / 3600.0)
    values = {
        "duration_text": _fmt_duration(data.duration_s),
        "distance_text": _fmt(data.distance_km, 2, " km"),
        "pace_text": _fmt(data.pace_min_per_km, 2, " /km"),
        "cadence_text": _fmt(data.cadence, 0, " 步/分"),
        "stride_text": _fmt(data.stride_m, 2, " m"),
        "steps_text": _fmt(data.step_total, 0, " 步"),
        "points_text": f"{data.point_count} 点",
        "speed_text": _fmt(speed_kmh, 2, " km/h"),
    }
    return [{"key": key, "label": label, "value": values[key]} for key, label, _ in METRIC_FIELDS]


def build_facts(data: CardData) -> list[str]:
    facts: list[str] = []
    if data.laps and data.lap_length_m:
        facts.append(f"{data.laps:.2f} 圈 × {data.lap_length_m:.0f} m")
    facts.extend(data.labels)
    if data.seed is not None:
        facts.append(f"seed {data.seed}")
    return facts


CARD_TEMPLATE = Template("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { width:${width}px; background:${page_bg};
    font-family:"HarmonyOS Sans SC","HarmonyOS Sans","Microsoft YaHei",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased; }
  /* The card is as tall as its content: a fixed height left a large empty strip below
     the metrics, which stretched the exported image for no reason. */
  .card { width:${width}px; display:flex; flex-direction:column; background:#0b0f16; }
  .map { position:relative; width:${width}px; height:${map_height}px; overflow:hidden;
    background:radial-gradient(circle at 50% 45%, #1b2433 0%, #0e131c 70%); }
  .tile { position:absolute; width:256px; height:256px; }
  .tile-missing { background:#141b26; }
  .veil { position:absolute; inset:0;
    background:linear-gradient(180deg, rgba(7,10,15,.62) 0%, rgba(7,10,15,.10) 34%,
      rgba(7,10,15,.14) 62%, rgba(11,15,22,.97) 100%); }
  svg.route { position:absolute; inset:0; }
  .r-halo { fill:none; stroke:rgba(34,211,238,.22); stroke-width:13; stroke-linejoin:round;
    stroke-linecap:round; }
  .r-line { fill:none; stroke:${accent}; stroke-width:4.5; stroke-linejoin:round;
    stroke-linecap:round; filter:drop-shadow(0 0 6px rgba(34,211,238,.85)); }
  .head { position:absolute; left:38px; right:38px; top:34px; }
  .eyebrow { font-size:19px; letter-spacing:.30em; color:rgba(226,236,248,.62); font-weight:500; }
  .headline { margin-top:6px; font-size:58px; font-weight:800; color:#f2f7ff;
    letter-spacing:-.01em; text-shadow:0 2px 18px rgba(0,0,0,.65); }
  .sub { margin-top:8px; font-size:20px; color:rgba(214,226,242,.78); display:flex;
    align-items:center; gap:12px; flex-wrap:wrap; }
  .badge { font-size:17px; font-weight:700; padding:4px 12px; border-radius:999px; }
  .badge.ok { color:#062b2f; background:linear-gradient(120deg,#22d3ee,#4ade80); }
  .badge.bad { color:#2b0606; background:linear-gradient(120deg,#fb7185,#f59e0b); }
  .marker { position:absolute; width:22px; height:22px; margin:-11px 0 0 -11px; border-radius:50%;
    border:4px solid #0b0f16; box-shadow:0 0 0 2px rgba(255,255,255,.85), 0 3px 12px rgba(0,0,0,.6); }
  .marker.start { background:#4ade80; }
  .marker.end { background:#fbbf24; }
  .marker-label { position:absolute; font-size:16px; font-weight:700; color:#eef6ff;
    text-shadow:0 2px 8px rgba(0,0,0,.9); }
  .panel { flex:1; padding:26px 38px 20px; display:flex; flex-direction:column; gap:16px; }
  .metrics { display:grid; grid-template-columns:repeat(4, 1fr); gap:14px; }
  .metric { background:linear-gradient(150deg,#182031,#131a27); border:1px solid #26314a;
    border-radius:14px; padding:14px 16px; display:flex; flex-direction:column; gap:6px; }
  .m-label { font-size:17px; color:#8ea0bd; letter-spacing:.08em; }
  .m-value { font-size:30px; font-weight:750; color:#f4f8ff; }
  .facts { display:flex; flex-wrap:wrap; gap:10px; }
  .facts span { font-size:17px; color:#9fb0cb; background:#131a27; border:1px solid #223048;
    border-radius:999px; padding:5px 13px; }
  .chart { background:linear-gradient(150deg,#151d2c,#111827); border:1px solid #223048;
    border-radius:14px; padding:12px 16px 8px; }
  .chart-title { font-size:16px; color:#8ea0bd; letter-spacing:.08em; margin-bottom:6px; }
  .chart svg { display:block; }
  .chart-note { font-size:15px; color:#6f8098; margin-top:4px; }
  .foot { margin-top:auto; display:flex; justify-content:space-between; align-items:center;
    border-top:1px solid #1f2937; padding-top:14px; font-size:16px; color:#6f8098; }
  .brand { font-weight:700; letter-spacing:.16em; color:#8ea0bd; }
</style></head>
<body><div class="card">
  <div class="map">
    ${tiles}
    <div class="veil"></div>
    <svg class="route" width="${width}" height="${map_height}" viewBox="0 0 ${width} ${map_height}">
      <path class="r-halo" d="${path_d}"/>
      <path class="r-line" d="${path_d}"/>
    </svg>
    <div class="marker start" style="left:${start_x}px;top:${start_y}px"></div>
    <div class="marker end" style="left:${end_x}px;top:${end_y}px"></div>
    <div class="marker-label" style="left:${start_label_x}px;top:${start_label_y}px">起点</div>
    <div class="marker-label" style="left:${end_label_x}px;top:${end_label_y}px">终点</div>
    <div class="head">
      <div class="eyebrow">LOCAL SIMULATION · SYNTHETIC</div>
      <div class="headline">${headline}</div>
      <div class="sub">${badge}<span>${subtitle}</span></div>
    </div>
  </div>
  <div class="panel">
    ${speed_chart}
    <div class="metrics">${metrics}</div>
    <div class="facts">${facts}</div>
    <div class="foot">
      <span>${period}</span>
      <span class="brand">GPS TRACK LAB</span>
    </div>
  </div>
</div></body></html>""")


def build_speed_chart(data: "CardData", width: int = CARD_WIDTH) -> str:
    """SVG pace chart for the card, or "" when there is nothing worth drawing.

    Mirrors the app's own chart: **faster pace is drawn lower** (the axis is min/km, so a
    smaller number means quicker), the line is coloured green-to-red by pace, and the slow
    stretches are what the eye is meant to find. A flat trace -- one constant speed for the
    whole run -- is exactly what this is meant to expose, so it is drawn honestly rather
    than smoothed.
    """
    trace = [float(value) for value in (data.pace_trace or []) if value is not None]
    if len(trace) < 4:
        return ""

    plot_w = width - 76                     # 38px padding on each side, like the panel
    plot_h = 104
    left, top = 46, 16
    lo = min(trace)
    hi = max(trace)
    # A dead-flat trace has no range to scale into; keep a minimum span so the line reads as
    # a line at the right height instead of dividing by zero.
    span = max(hi - lo, 0.6)
    mid = (hi + lo) / 2.0
    lo, hi = mid - span / 2.0, mid + span / 2.0

    def x_at(index: int) -> float:
        return left + plot_w * index / max(1, len(trace) - 1)

    def y_at(pace: float) -> float:
        # Larger pace (slower) -> higher on the chart, matching the app's orientation.
        return top + plot_h * (pace - lo) / (hi - lo)

    points = " ".join(f"{x_at(i):.1f},{y_at(p):.1f}" for i, p in enumerate(trace))
    area = (f"M{x_at(0):.1f},{top + plot_h:.1f} L" +
            " L".join(f"{x_at(i):.1f},{y_at(p):.1f}" for i, p in enumerate(trace)) +
            f" L{x_at(len(trace) - 1):.1f},{top + plot_h:.1f} Z")

    # Slow stretches get a vertical band so a walk break is visible at a glance.
    bands = []
    walk_threshold = min(hi, max(lo, data.pace_min_per_km or mid) + 1.2)
    index = 0
    while index < len(trace):
        if trace[index] >= walk_threshold:
            start = index
            while index < len(trace) and trace[index] >= walk_threshold:
                index += 1
            bands.append((x_at(start), max(3.0, x_at(index - 1) - x_at(start))))
        else:
            index += 1
    band_svg = "".join(
        f'<rect x="{x:.1f}" y="{top}" width="{w:.1f}" height="{plot_h}" fill="#f97316" '
        f'opacity="0.16"/>' for x, w in bands
    )

    def label(pace: float, y: float) -> str:
        return (f'<text x="6" y="{y + 5:.1f}" fill="#6f8098" font-size="14" '
                f'font-family="Segoe UI, sans-serif">{pace:.1f}</text>')

    ticks = ""
    for fraction in (0.0, 0.5, 1.0):
        pace = lo + (hi - lo) * (1.0 - fraction)
        ticks += label(pace, top + plot_h * fraction)
        ticks += (f'<line x1="{left}" y1="{top + plot_h * fraction:.1f}" '
                  f'x2="{left + plot_w}" y2="{top + plot_h * fraction:.1f}" '
                  f'stroke="#223048" stroke-width="1"/>')

    slow_count = len(bands)
    note = (f"配速曲线（min/km，越低越快）· 波动 {hi - lo:.2f} · 慢速段 {slow_count} 处"
            if bands else
            f"配速曲线（min/km，越低越快）· 波动 {hi - lo:.2f} · 全程匀速")
    return (
        '<div class="chart">'
        f'<div class="chart-title">配速曲线</div>'
        f'<svg width="{width - 76}" height="{plot_h + 34}" viewBox="0 0 {width - 76} {plot_h + 34}">'
        f'<defs><linearGradient id="paceGrad" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0%" stop-color="#4ade80"/><stop offset="55%" stop-color="#fbbf24"/>'
        '<stop offset="100%" stop-color="#f87171"/></linearGradient>'
        '<linearGradient id="paceFill" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#22d3ee" stop-opacity="0.28"/>'
        '<stop offset="100%" stop-color="#22d3ee" stop-opacity="0"/></linearGradient></defs>'
        f'{ticks}{band_svg}'
        f'<path d="{area}" fill="url(#paceFill)"/>'
        f'<polyline points="{points}" fill="none" stroke="url(#paceGrad)" stroke-width="3" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<text x="{left}" y="{plot_h + 28}" fill="#6f8098" font-size="14" '
        'font-family="Segoe UI, sans-serif">0 km</text>'
        f'<text x="{left + plot_w - 46}" y="{plot_h + 28}" fill="#6f8098" font-size="14" '
        f'font-family="Segoe UI, sans-serif">{_fmt(data.distance_km, 2)} km</text>'
        '</svg>'
        f'<div class="chart-note">{html.escape(note)}</div>'
        '</div>'
    )


def compile_card(data: CardData, basemap: Basemap | None = None,
                 width: int = CARD_WIDTH, map_height: int = MAP_HEIGHT,
                 page_bg: str = "#f4f6fa") -> tuple[str, dict[str, Any]]:
    """Return (html, meta). Everything embedded; the page needs no network.

    The page background is deliberately light while the card is dark: the renderer clips
    the capture to the card's own box, and a contrasting surround makes any leak of page
    background into the picture obvious instead of blending in.
    """
    if len(data.points) < 2:
        raise ValueError("a card needs at least two trajectory points")

    basemap = basemap or Basemap()
    # 按底图自己的影像覆盖上限夹一下 zoom：高德卫星只到 z18，再往上整片是
    # 「此区域无卫星图」占位瓦片（详见 basemap.PROVIDER_MAX_ZOOM）。
    zoom = choose_zoom(data.points, width, map_height, padding_px=70,
                       max_zoom=getattr(basemap, "max_zoom", 19))
    tile_range, centre = plan_view(data.points, zoom, width, map_height)
    mosaic = basemap.mosaic(tile_range)

    canvas = simplify(
        route_pixels(data.points, zoom, tile_range, centre, width, map_height),
        tolerance_px=0.3,
    )
    path_d = " ".join(
        ("M" if index == 0 else "L") + f"{x:.1f} {y:.1f}" for index, (x, y) in enumerate(canvas)
    )

    # Tiles are absolutely positioned so a missing one leaves a visible gap instead of
    # shifting the rest of the mosaic.
    tile_parts = []
    for row_index, row in enumerate(mosaic):
        for column_index, (uri, tile) in enumerate(row):
            left = column_index * 256 - tile_range.offset_x
            top = row_index * 256 - tile_range.offset_y
            if uri:
                tile_parts.append(f'<img class="tile" src="{uri}" style="left:{left:.1f}px;top:{top:.1f}px">')
            else:
                tile_parts.append(f'<div class="tile tile-missing" style="left:{left:.1f}px;top:{top:.1f}px"></div>')

    metrics = build_metric_cells(data)
    metric_html = "".join(
        f'<div class="metric"><span class="m-label">{html.escape(cell["label"])}</span>'
        f'<span class="m-value">{html.escape(cell["value"])}</span></div>'
        for cell in metrics
    )
    facts = build_facts(data)
    fact_html = "".join(f"<span>{html.escape(fact)}</span>" for fact in facts)

    badge = ""
    if data.qualified is not None:
        badge = ('<span class="badge ok">合格</span>' if data.qualified
                 else '<span class="badge bad">不合格</span>')
    headline = f"{_fmt(data.distance_km, 2)} km · {_fmt_duration(data.duration_s)}"
    period = f"{data.started_at} ~ {data.finished_at}" if data.started_at else data.footer_note

    start, end = canvas[0], canvas[-1]
    document = CARD_TEMPLATE.substitute(
        width=width,
        map_height=map_height,
        page_bg=page_bg,
        accent=data.accent,
        tiles="".join(tile_parts),
        path_d=path_d,
        start_x=f"{start[0]:.1f}", start_y=f"{start[1]:.1f}",
        end_x=f"{end[0]:.1f}", end_y=f"{end[1]:.1f}",
        start_label_x=f"{start[0] + 16:.1f}", start_label_y=f"{start[1] - 12:.1f}",
        end_label_x=f"{end[0] + 16:.1f}", end_label_y=f"{end[1] - 12:.1f}",
        headline=html.escape(headline),
        badge=badge,
        subtitle=html.escape(data.subtitle),
        speed_chart=build_speed_chart(data, width=width),
        metrics=metric_html,
        facts=fact_html,
        period=html.escape(period),
    )

    meta = {
        "zoom": zoom,
        "metres_per_pixel": round(metres_per_pixel(data.points[0][1], zoom), 3),
        "tiles": tile_range.count,
        "tiles_fetched": basemap.downloads,
        "tiles_cached": basemap.cache_hits,
        "tiles_failed": len(basemap.failures),
        "tile_failures": basemap.failures[:5],
        "path_points": len(canvas),
        "provider": basemap.provider,
    }
    return document, meta


def find_browser() -> str:
    """Locate a Chromium-based browser usable for headless screenshots.

    Order: explicit override (``YUNRUN_BROWSER`` / ``EDGE_PATH``) → well-known
    install paths per OS → ``PATH`` lookup. Raises with an actionable message
    instead of a bare ``FileNotFoundError`` so the caller can show it verbatim.
    """
    for env_name in ("YUNRUN_BROWSER", "EDGE_PATH"):
        override = os.environ.get(env_name)
        if override and Path(override).is_file():
            return override
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for command in BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    raise FileNotFoundError(
        "找不到可用于截图的无头浏览器（Edge / Chrome / Chromium 任一）。"
        "安装其中之一，或用环境变量 YUNRUN_BROWSER 指定可执行文件路径"
        "（Windows 例：set YUNRUN_BROWSER=C:\\...\\msedge.exe ；"
        "macOS/Linux 例：export YUNRUN_BROWSER=\"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\"）"
    )


def find_edge() -> str:
    """历史名字，等价于 :func:`find_browser`（截图早就不是 Edge 专属了）。"""
    return find_browser()


def render_png(document: str, output: Path, width: int = CARD_WIDTH,
               height: int | None = None, edge_path: str | None = None,
               timeout: float = 180.0, keep_html: Path | None = None) -> Path:
    """Screenshot a compiled card with headless Edge, cropped to the card itself.

    ``--screenshot`` always captures the whole window, so with a fixed window height the
    card came out with a large empty strip below the content. This drives the browser
    over CDP instead: the page reports its own content height and the capture is clipped
    to exactly that box. Passing ``height`` pins it; leaving it None auto-fits.
    """
    edge = edge_path or find_edge()
    # Edge resolves a relative --screenshot path against its own working directory, so
    # paths handed to the browser must be absolute.
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    html_path = Path(keep_html).resolve() if keep_html else output.with_suffix(".html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(document, encoding="utf-8")
    if output.exists():
        output.unlink()

    # A generous window so the layout is never squeezed before we measure it.
    window_height = max(height or 0, 2000)
    with tempfile.TemporaryDirectory(prefix="edge-cdp-") as profile:
        port = _free_port()
        command = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--no-first-run",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            f"--window-size={width},{window_height}",
            html_path.resolve().as_uri(),
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ws = WS(page_ws_url(port))
            ws.call("Runtime.enable")
            ws.call("Page.enable")
            # Wait for fonts and the embedded tiles, then measure the card box.
            measured = evaluate(ws, """(async () => {
                if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
                await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                const card = document.querySelector('.card') || document.body;
                const rect = card.getBoundingClientRect();
                return JSON.stringify({
                  width: Math.ceil(rect.width),
                  height: Math.ceil(rect.height),
                  scrollHeight: Math.ceil(card.scrollHeight),
                });
            })()""")
            box = json.loads(measured) if measured else {}
            capture_height = int(height or box.get("height") or box.get("scrollHeight") or 0)
            if capture_height <= 0:
                raise RuntimeError("page reported no content height")
            shot = ws.call("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": capture_height, "scale": 1},
            }, timeout=timeout)
            payload = base64.b64decode(shot["data"])
            if not payload:
                raise RuntimeError("empty screenshot payload")
            output.write_bytes(payload)
        finally:
            with contextlib.suppress(Exception):
                process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=15)
    if not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("headless Edge produced no screenshot")
    return output


def render_card(data: CardData, output: Path, basemap: Basemap | None = None,
                edge_path: str | None = None, keep_html: bool = True) -> dict[str, Any]:
    """Compile and rasterise in one call; returns the render metadata."""
    document, meta = compile_card(data, basemap)
    started = time.time()
    png_path = render_png(document, output, edge_path=edge_path,
                          keep_html=Path(output).with_suffix(".html") if keep_html else None)
    meta["png"] = str(png_path)
    meta["png_bytes"] = Path(png_path).stat().st_size
    meta["render_seconds"] = round(time.time() - started, 2)
    meta["html"] = str(Path(output).with_suffix(".html")) if keep_html else None
    # Measured from the file rather than assumed: the card height is content-driven.
    with png_path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        meta["width"] = int.from_bytes(header[16:20], "big")
        meta["height"] = int.from_bytes(header[20:24], "big")
    return meta


def load_tasklist_points(path: str | Path) -> list[tuple[float, float]]:
    """Read a 云运动 tasklist export and return its trajectory."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for point in document.get("data", {}).get("pointsList", []):
        try:
            lon, lat = (float(part) for part in str(point["point"]).split(",", 1))
        except (KeyError, TypeError, ValueError):
            continue
        out.append((lon, lat))
    return out


def card_from_tasklist(path: str | Path, title: str = "健康跑") -> CardData:
    """Build CardData from a tasklist export, filling what the file actually carries."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    data = document.get("data", {})
    points = load_tasklist_points(path)
    rows = data.get("pointsList", [])
    step_total = None
    stride = None
    if rows:
        try:
            step_total = int(float(rows[-1].get("runStep")))
            mileage = float(rows[-1].get("runMileage"))
            if step_total:
                stride = mileage / step_total
        except (TypeError, ValueError):
            pass
    duration = data.get("duration")
    distance_km = data.get("recordMileage")
    pace = None
    try:
        if distance_km and duration:
            pace = (float(duration) / 60.0) / float(distance_km)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return CardData(
        points=points,
        title=title,
        subtitle=Path(path).stem,
        distance_km=float(distance_km) if distance_km else None,
        duration_s=float(duration) if duration else None,
        pace_min_per_km=pace or data.get("recodePace"),
        cadence=float(data["recodeCadence"]) if data.get("recodeCadence") else None,
        stride_m=stride,
        step_total=step_total,
    )
