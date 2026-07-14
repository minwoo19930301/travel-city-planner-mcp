import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]+)\}}", css)
    assert match, f"missing CSS rule: {selector}"
    return match.group(1)


def _css_variables(css: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", _css_rule(css, ":root")))


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_fictional_demo_and_static_viewer_files() -> None:
    demo = json.loads((ROOT / "examples/demo-plan.json").read_text(encoding="utf-8"))
    assert demo["demo"]["fictional"] is True
    assert demo["duration"]["selected_nights"] == 5
    assert (ROOT / "viewer/index.html").is_file()
    assert (ROOT / "viewer/app.js").is_file()
    assert (ROOT / "viewer/styles.css").is_file()


def test_viewer_exposes_live_clocks_phrases_exchange_and_leg_routes() -> None:
    index = (ROOT / "viewer/index.html").read_text(encoding="utf-8")
    css = (ROOT / "viewer/styles.css").read_text(encoding="utf-8")
    script = (ROOT / "viewer/app.js").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in index
    for weight in ("Light", "Medium", "Bold"):
        font = ROOT / f"viewer/assets/fonts/GmarketSans{weight}.woff2"
        assert font.is_file() and font.stat().st_size > 0
        assert font.name in index
    assert 'font-family: "Gmarket Sans"' in css
    assert "IBM Plex" not in css
    assert 'catalog.destinations[id] || catalog.destinations.tokyo' not in script
    assert 'throw new Error(`알 수 없는 목적지입니다: ${id}`)' in script
    assert 'clockRow("한국 · 서울", "Asia/Seoul")' in script
    assert "updateClocks(clockPanel)" in script
    assert "destination.phrases" in script
    assert "/api/city-guide/" in script
    assert 'directionsUrl(from, to, "transit")' in script
    assert 'directionsUrl(from, to, "walking")' in script
    assert 'directionsUrl(from, to, "driving")' in script
    assert "button.dataset.destinationId" in script
    assert "root.dataset.activeDestination = destination.id" in script
    assert "content.dataset.destinationId = destinationId" in script
    assert "desk.dataset.destinationId = activity.destination_id || activeDestinationId" in script
    assert 'summary.dataset.activeCitySummary = ""' in script
    assert "summary.textContent = destination.summary" in script
    assert "showMapPreview(cityMapSelection(plan, destinationId), false)" in script
    assert "AbortController" in script
    assert "renderSource" not in script


def test_viewer_city_background_controls_and_contrast_contract() -> None:
    css = (ROOT / "viewer/styles.css").read_text(encoding="utf-8")
    variables = _css_variables(css)
    active_rule = _css_rule(
        css,
        '.segment-row button[aria-pressed="true"], .city-tabs button[aria-pressed="true"]',
    )
    matte_rule = _css_rule(css, "body::after")

    assert _contrast_ratio(variables["--control-bg"], variables["--control-text"]) >= 4.5
    assert "background: var(--control-bg)" in active_rule
    assert "color: var(--control-text)" in active_rule
    assert "border-color: var(--accent)" in active_rule
    assert "background: var(--accent)" not in active_rule
    assert "linear-gradient" not in matte_rule
    assert "rgba(5, 8, 5, .8)" in matte_rule
    assert '--hero-image' in _css_rule(css, "body::before")
    assert "z-index: 0" in _css_rule(css, "body::before, body::after")
    assert "z-index: 1" in matte_rule
    assert "z-index: 2" in _css_rule(css, "body > :not(dialog)")
    assert "z-index: -" not in css
    assert ".site-actions button { min-height: 38px" not in css
    assert ".site-actions button { min-height: 44px" in css
    assert "min-height: 44px" in _css_rule(css, ".map-link, .map-preview")
    assert "min-height: 44px" in _css_rule(css, ".leg-links a, .leg-links span")
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "overflow-x: hidden" in css
    assert "overflow-wrap: normal" in _css_rule(css, ".cover h1")
    assert "word-break: keep-all" in _css_rule(css, ".cover h1")
