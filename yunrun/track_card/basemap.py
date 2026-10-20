# -*- coding: utf-8 -*-
"""Basemap tile fetching, caching and mosaicking for the trajectory renderer.

Tiles are downloaded once and cached on disk, then embedded into the card as data URIs
so the final screenshot needs no network at all. That split matters: fetching is the
slow, failure-prone part, while rendering must be deterministic and offline.
"""
from __future__ import annotations

import base64
import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .tiles import TILE_SIZE, TileRange

USER_AGENT = "gps-track-lab/0.3 (local trajectory renderer)"

# 瓦片抓取直连（不走任何代理）：urllib 默认读环境变量与 Windows 注册表代理，
# 而云运动 App 的三个 OkHttp 客户端全部显式 proxy(Proxy.NO_PROXY)——系统挂着
# 代理（Clash/沙箱注入等）时按默认行为会把卫星瓦片全部拦失败，卡片退化成
# 纯色块。与 yun_http._get_session 的 trust_env=False 同一对齐依据。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 默认底图 = **高德卫星影像（amap_satellite）**。理由（2026-10-04 用眼睛逐张核对后定的）：
#
# 需求是"保留底图、但不要底图上的关键地点文字"。实测结论：
#   · webrd0x + style=8（曾用默认）  底图密布地名——体育场/食堂/超市/街道/宿舍楼名一目了然，
#     肉眼可见，确认不合格；
#   · wprd0x  + style=7（"无注记档"）**照样密布文字**（义苑食堂、合一福超市、泰安大街…），
#     跟 style=8 差不多，并没有解决问题；
#   · webrd0x + style=7             返回**纯色空白瓦片**（1 位深 1 色调色板 IDAT 31B）；
#   · 高德内置样式 normal/light/grey/whitesmoke… 全部带注记；graffiti/wine 是纯色块
#     装饰而非地图（唯一色数仅 21~32，真地图 normal 是 276 色）；
#   · 自适应阈值擦字（Bradley）       只能降约 45%，且检测器底噪高、有误伤路网风险，已弃用；
#   · **卫星影像天然没有任何文字**     ← 最终采用，零注记、零水印，且田径场/建筑清晰可辨。
#
# 原始色版本（未做灰度/降饱和）是逐张对比后选定的：细节最丰富、观感最自然，
# 青色轨迹线在深绿场地上对比依然充足。
# 若需切回旧行为：把 DEFAULT_PROVIDER 改回 "amap_vector" 即可
# （旧文件备份在 data/server_watch/basemap_backup_v1_original.py）。
PROVIDERS: dict[str, str] = {
    "amap_satellite": "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
    "amap_vector": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
    "amap_vector_nolabels": "https://wprd0{s}.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7",
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    # CARTO 现已要求 API key：未带 key 会整片返回 "API KEY REQUIRED" 水印瓦片，故不用于链路。
    "carto_light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
}
DEFAULT_PROVIDER = "amap_satellite"

# 各家的**影像覆盖上限**。高德卫星影像到 z18 为止；z19 起整片返回一张写着
# 「此区域无卫星图」的占位瓦片（256×256、RGBA、恒定 4235 B）。卡片的选 zoom 逻辑是
# 「能装下整条轨迹的最大 zoom」，默认上限 19，于是默认底图会在绝大多数学校上拼出一整片
# 占位图——看上去像是底图坏了。所以这里按 provider 夹一次。
PROVIDER_MAX_ZOOM: dict[str, int] = {
    "amap_satellite": 18,
    "amap_vector": 18,
    "amap_vector_nolabels": 18,
    "osm": 19,
    "carto_light": 20,
}


@dataclass
class Tile:
    z: int
    x: int
    y: int
    path: Path | None
    ok: bool
    mime: str = "image/png"

    @property
    def data_uri(self) -> str | None:
        if not self.ok or self.path is None:
            return None
        encoded = base64.b64encode(self.path.read_bytes()).decode("ascii")
        return f"data:{self.mime};base64,{encoded}"


def sniff_image_mime(payload: bytes) -> str | None:
    """Detect the image type from magic bytes.

    Needed because tile URLs lie about their format: 高德's ``appmaptile`` endpoints
    serve JPEG from URLs ending in ``.png``, and trusting the extension or an HTTP
    header means either rejecting good tiles or embedding a mislabelled data URI that
    the browser refuses to draw.
    """
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if payload[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


class Basemap:
    """Disk-cached tile source."""

    def __init__(self, provider: str = DEFAULT_PROVIDER, cache_dir: Path | None = None,
                 timeout: float = 12.0, subdomains: str = "1234") -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}; choose from {sorted(PROVIDERS)}")
        self.provider = provider
        self.template = PROVIDERS[provider]
        # 超过这条线就只能拿到占位瓦片（见 PROVIDER_MAX_ZOOM 的说明）
        self.max_zoom = PROVIDER_MAX_ZOOM.get(provider, 19)
        self.timeout = timeout
        self.subdomains = subdomains
        self.cache_dir = Path(cache_dir) if cache_dir else Path(__file__).resolve().parent / "tile_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.failures: list[str] = []
        self.downloads = 0
        self.cache_hits = 0

    def _path(self, z: int, x: int, y: int) -> Path:
        key = hashlib.sha1(f"{self.provider}/{z}/{x}/{y}".encode()).hexdigest()[:16]
        return self.cache_dir / f"{self.provider}_{z}_{x}_{y}_{key}.png"

    def url(self, z: int, x: int, y: int) -> str:
        sub = self.subdomains[(x + y) % len(self.subdomains)]
        return self.template.format(s=sub, x=x, y=y, z=z)

    def _valid_png(self, payload: bytes) -> bool:
        """Reject error pages: a tile server answering 200 with HTML would otherwise be
        cached as a picture and silently poison every later render."""
        return payload[:8] == b"\x89PNG\r\n\x1a\n" and len(payload) > 100

    def tile(self, z: int, x: int, y: int) -> Tile:
        path = self._path(z, x, y)
        if path.is_file() and path.stat().st_size > 0:
            self.cache_hits += 1
            mime = sniff_image_mime(path.read_bytes()[:16]) or "image/png"
            return Tile(z, x, y, path, True, mime)
        request = urllib.request.Request(self.url(z, x, y), headers={"User-Agent": USER_AGENT})
        try:
            with _DIRECT_OPENER.open(request, timeout=self.timeout) as response:
                payload = response.read()
            mime = sniff_image_mime(payload)
            if mime is None:
                raise ValueError(f"not an image ({len(payload)} bytes)")
            path.write_bytes(payload)
            self.downloads += 1
            return Tile(z, x, y, path, True, mime)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures.append(f"{z}/{x}/{y} sub={self.subdomains[(x + y) % len(self.subdomains)]}: "
                                 f"{type(exc).__name__} {str(exc)[:60]}")
            path.unlink(missing_ok=True)
            return Tile(z, x, y, None, False)

    def mosaic(self, tile_range: TileRange,
               fallback: tuple[int, int, int] = (18, 22, 30)) -> list[list[tuple[str | None, Tile]]]:
        """Fetch every tile in the range and return rows of (data_uri, tile).

        A missing tile becomes ``None``; the renderer paints the fallback colour there so
        a partial network failure degrades to a plain patch instead of a broken image.
        """
        rows: list[list[tuple[str | None, Tile]]] = []
        recorded = "\n".join(self.failures)
        for y in range(tile_range.y_min, tile_range.y_max + 1):
            row: list[tuple[str | None, Tile]] = []
            for x in range(tile_range.x_min, tile_range.x_max + 1):
                tile = self.tile(tile_range.z, x, y)
                marker = f"{tile_range.z}/{x}/{y}"
                if not tile.ok and marker not in recorded:
                    # Count failures from the returned tile, not only from the internal
                    # log: a subclass or a cache layer must not be able to hide them.
                    self.failures.append(f"{marker}: unavailable")
                row.append((tile.data_uri, tile))
            rows.append(row)
        return rows
