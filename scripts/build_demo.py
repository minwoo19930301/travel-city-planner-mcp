from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner import PlannerService


async def main() -> None:
    service = PlannerService()
    plan = await service.plan_from_query(
        "도쿄로 4-5박 정도 머무르며 애니, 야경, 카페를 즐기는 가상 여행 추천 플래너",
        include_live_data=False,
    )
    plan["demo"] = {
        "fictional": True,
        "note": "문서와 viewer 검증을 위한 가상 시나리오이며 실제 예약 정보가 아닙니다.",
    }
    target = ROOT / "examples" / "demo-plan.json"
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote fictional demo: {target}")


if __name__ == "__main__":
    asyncio.run(main())
