from __future__ import annotations

import json
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the renderer without executing planner/__init__.py. The browser gate
# only needs pure stdlib catalog/render code, so npm test:browser remains
# independent from the MCP server's optional HTTP runtime dependencies.
planner_package = types.ModuleType("planner")
planner_package.__path__ = [str(ROOT / "planner")]
sys.modules.setdefault("planner", planner_package)

from planner.catalog import Catalog
from planner.render import render_export_html


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_export_fixture.py PLAN_JSON OUTPUT_HTML")
    plan_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output_path.write_text(render_export_html(plan, Catalog()), encoding="utf-8")


if __name__ == "__main__":
    main()
