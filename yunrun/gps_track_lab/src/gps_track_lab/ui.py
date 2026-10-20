"""Load the local UI assets without changing the application's HTTP API."""
from pathlib import Path

FRONTEND = Path(__file__).with_name("frontend")


def render_page(name: str) -> str:
    markup = (FRONTEND / f"{name}.html").read_text(encoding="utf-8")
    return style_markup(markup)


def style_markup(markup: str) -> str:
    style = (FRONTEND / "studio.css").read_text(encoding="utf-8")
    script = (FRONTEND / "studio.js").read_text(encoding="utf-8")
    return markup.replace("<!-- STUDIO_STYLE -->", f"<style>{style}</style>").replace(
        "<!-- STUDIO_SCRIPT -->", f"<script>{script}</script>"
    ).replace(
        "<!-- ADMIN_STYLE -->",
        f"<style>{(FRONTEND / 'admin.css').read_text(encoding='utf-8')}</style>"
        if "<!-- ADMIN_STYLE -->" in markup else "",
    ).replace(
        "<!-- ONBOARDING_SCRIPT -->",
        f"<script>{(FRONTEND / 'onboarding.js').read_text(encoding='utf-8')}</script>"
        if "<!-- ONBOARDING_SCRIPT -->" in markup else "",
    )
