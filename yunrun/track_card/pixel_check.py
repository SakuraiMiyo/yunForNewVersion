# -*- coding: utf-8 -*-
"""Pixel-level checks for a rendered card, using only the standard library.

Written because the renderer's failure modes are visual and silent: a card can come out
as a correctly sized PNG whose map is a flat colour (tiles were rejected) or whose route
is missing (projection mismatch). Decoding the PNG and looking at actual pixels catches
both without any third-party image library.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Image:
    width: int
    height: int
    pixels: list[list[tuple[int, int, int]]]

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        return self.pixels[y][x]

    def region(self, x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int, int]]:
        out = []
        for y in range(max(0, y0), min(self.height, y1)):
            row = self.pixels[y]
            for x in range(max(0, x0), min(self.width, x1)):
                out.append(row[x])
        return out


def decode_png(path: str | Path) -> Image:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    position = 8
    width = height = bit_depth = colour_type = interlace = 0
    idat = bytearray()
    while position < len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        chunk_type = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, colour_type, _comp, _filt, interlace = struct.unpack(">IIBBBBB", payload)
        elif chunk_type == b"IDAT":
            idat += payload
        elif chunk_type == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or colour_type not in (2, 6):
        raise ValueError(f"unsupported png: depth={bit_depth} colour={colour_type} interlace={interlace}")

    channels = 3 if colour_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[list[tuple[int, int, int]]] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unknown filter {filter_type}")
        rows.append([tuple(line[i:i + 3]) for i in range(0, stride, channels)])  # type: ignore[misc]
        previous = line
    return Image(width, height, rows)


def _variance(pixels: list[tuple[int, int, int]]) -> float:
    if not pixels:
        return 0.0
    mean = sum((r + g + b) / 3.0 for r, g, b in pixels) / len(pixels)
    return sum(((r + g + b) / 3.0 - mean) ** 2 for r, g, b in pixels) / len(pixels)


def _is_accent(pixel: tuple[int, int, int], tolerance: int = 70) -> bool:
    """Cyan-ish route colour: blue and green clearly above red."""
    red, green, blue = pixel
    return (green - red) > tolerance * 0.6 and (blue - red) > tolerance * 0.6 and blue > 120


def check_card(path: str | Path, map_height: int = 760, expected_width: int = 1080) -> dict:
    """Return a report describing whether the card actually rendered its map and route.

    The card height is content-driven, so this checks that the layout is *consistent*
    rather than a fixed number: the image is wide enough, the map band carries content,
    the route is drawn, and no large flat band sits below the footer (which is what the
    old fixed-height template used to leave behind).
    """
    image = decode_png(path)
    report: dict = {
        "width": image.width,
        "height": image.height,
        "size_ok": image.width == expected_width and image.height > map_height + 150,
    }
    map_pixels = image.region(0, 0, image.width, map_height)
    panel_pixels = image.region(0, map_height + 10, image.width, image.height)
    report["map_variance"] = round(_variance(map_pixels), 1)
    report["panel_variance"] = round(_variance(panel_pixels), 1)
    # Vector basemaps are flat by design, so the bar is much lower than for imagery.
    report["map_has_content"] = report["map_variance"] > 15.0
    accent = sum(1 for pixel in map_pixels if _is_accent(pixel))
    report["accent_pixels"] = accent
    report["accent_ratio"] = round(accent / max(1, len(map_pixels)), 5)
    report["route_drawn"] = accent > 1500
    report["map_mean_luma"] = round(
        sum((r + g + b) / 3.0 for r, g, b in map_pixels) / max(1, len(map_pixels)), 1)
    report["panel_mean_luma"] = round(
        sum((r + g + b) / 3.0 for r, g, b in panel_pixels) / max(1, len(panel_pixels)), 1)
    report["distinct_colors"] = len({pixel for pixel in map_pixels[::7]})

    # Trailing flat rows: a long run at the bottom means the card is taller than content.
    trailing = 0
    for y in range(image.height - 1, map_height, -1):
        if len(set(image.pixels[y])) <= 2:
            trailing += 1
        else:
            break
    report["trailing_flat_rows"] = trailing
    report["no_empty_tail"] = trailing <= 60

    report["ok"] = bool(report["size_ok"] and report["map_has_content"]
                        and report["route_drawn"] and report["no_empty_tail"])
    return report


if __name__ == "__main__":
    import json
    import sys

    for target in sys.argv[1:]:
        report = check_card(target)
        print(target)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # A stop-and-continue check: this is the renderer's own smoke test, so it must
        # return non-zero when a card came out broken.
        if not report["ok"]:
            sys.exit(1)
