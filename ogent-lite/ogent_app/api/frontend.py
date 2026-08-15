"""Load the built React frontend into one nonce-protected HTTP response."""

from __future__ import annotations

import functools
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
HTML_PATH = WEB_ROOT / "shell.html"
CSS_PATH = WEB_ROOT / "dist" / "assets" / "ogent.css"
JAVASCRIPT_PATH = WEB_ROOT / "dist" / "assets" / "ogent.js"
JAVASCRIPT_PATHS = (JAVASCRIPT_PATH,)
CSS_PLACEHOLDER = "__OGENT_INLINE_CSS__"
JAVASCRIPT_PLACEHOLDER = "__OGENT_INLINE_JAVASCRIPT__"


class FrontendResourceError(RuntimeError):
    """A deterministic startup error for missing packaged UI resources."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FrontendResourceError(
            f"Packaged frontend resource is unavailable: {path.name}"
        ) from exc


@functools.lru_cache(maxsize=1)
def load_html_template() -> str:
    """Compose Vite output into the local app's nonce-protected document."""
    shell = _read_text(HTML_PATH)
    if shell.count(CSS_PLACEHOLDER) != 1 or shell.count(JAVASCRIPT_PLACEHOLDER) != 1:
        raise FrontendResourceError("Packaged frontend placeholders are invalid.")
    stylesheet = _read_text(CSS_PATH)
    javascript = "".join(_read_text(path) for path in JAVASCRIPT_PATHS)
    if "</style" in stylesheet.casefold() or "</script" in javascript.casefold():
        raise FrontendResourceError(
            "Packaged frontend output contains an unsafe inline terminator."
        )
    return shell.replace(CSS_PLACEHOLDER, stylesheet).replace(
        JAVASCRIPT_PLACEHOLDER,
        javascript,
    )
