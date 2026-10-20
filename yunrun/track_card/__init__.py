# -*- coding: utf-8 -*-
"""Trajectory card renderer: map + route + key run metrics, as a single PNG.

Two entry points:

    from track_card import CardData, render_card
    render_card(CardData(points=[...], distance_km=3.04, duration_s=1032, ...),
                "renders/my_run.png")

    python -m track_card --tasklist path/to/tasklist.json --output renders/card.png
"""
from .basemap import DEFAULT_PROVIDER, PROVIDERS, Basemap
from .render_card import (
    CARD_HEIGHT,
    CARD_WIDTH,
    CardData,
    MAP_HEIGHT,
    build_facts,
    build_metric_cells,
    card_from_tasklist,
    compile_card,
    find_browser,
    find_edge,
    load_tasklist_points,
    render_card,
    render_png,
)
from .tiles import choose_zoom, lonlat_to_pixel, metres_per_pixel, plan_view, route_pixels, simplify

__all__ = [
    "Basemap",
    "CardData",
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "DEFAULT_PROVIDER",
    "MAP_HEIGHT",
    "PROVIDERS",
    "build_facts",
    "build_metric_cells",
    "card_from_tasklist",
    "choose_zoom",
    "compile_card",
    "find_browser",
    "find_edge",
    "load_tasklist_points",
    "lonlat_to_pixel",
    "metres_per_pixel",
    "plan_view",
    "render_card",
    "render_png",
    "route_pixels",
    "simplify",
]
