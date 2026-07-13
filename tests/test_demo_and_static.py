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
