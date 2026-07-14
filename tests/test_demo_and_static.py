import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fictional_demo_and_static_viewer_files() -> None:
    demo = json.loads((ROOT / "examples/demo-plan.json").read_text(encoding="utf-8"))
    assert demo["demo"]["fictional"] is True
    assert demo["duration"]["selected_nights"] == 5
    assert (ROOT / "viewer/index.html").is_file()
    assert (ROOT / "viewer/app.js").is_file()
    assert (ROOT / "viewer/styles.css").is_file()


def test_viewer_exposes_live_clocks_phrases_exchange_and_leg_routes() -> None:
    script = (ROOT / "viewer/app.js").read_text(encoding="utf-8")
    assert 'catalog.destinations[id] || catalog.destinations.tokyo' not in script
    assert 'throw new Error(`알 수 없는 목적지입니다: ${id}`)' in script
    assert 'clockRow("한국 · 서울", "Asia/Seoul")' in script
    assert "updateClocks(clockPanel)" in script
    assert "destination.phrases" in script
    assert "/api/city-guide/" in script
    assert 'directionsUrl(from, to, "transit")' in script
    assert 'directionsUrl(from, to, "walking")' in script
    assert 'directionsUrl(from, to, "driving")' in script
    assert "showMapPreview(firstCityActivity, false)" in script
    assert "segment-thumb" in script
