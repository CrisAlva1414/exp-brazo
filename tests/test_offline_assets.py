"""El frontend debe ser 100 % local: sin CDN, fuentes ni telemetría externa."""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[1] / "exp_brazo" / "dashboard" / "static"
ASSETS = ("index.html", "app.js", "style.css")
FORBIDDEN = (
    "http://",
    "https://",
    "//cdn",
    "unpkg",
    "jsdelivr",
    "googleapis",
    "fonts.googleapis",
)


@pytest.mark.parametrize("filename", ASSETS)
def test_asset_has_no_external_references(filename: str) -> None:
    text = (STATIC_DIR / filename).read_text(encoding="utf-8").lower()
    for token in FORBIDDEN:
        assert token not in text, f"{filename} contiene referencia externa: {token}"


def test_index_references_local_assets() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "/static/app.js" in index
    assert "/static/style.css" in index


def test_index_has_token_placeholder() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'name="exp-brazo-token"' in index
    assert "__EXP_BRAZO_TOKEN__" in index
    assert 'data-src="/video.mjpeg"' in index


def test_javascript_avoids_eval_and_dynamic_html() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in script
    assert "innerHTML" not in script
