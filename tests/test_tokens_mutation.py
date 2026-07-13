import asyncio

import pytest

from planner import PlannerService
from planner.render import legacy_v3_payload, render_export_html
from planner.tokens import TokenError, encode_content_token


def run(coro):
    return asyncio.run(coro)


def make_plan(service: PlannerService):
    return run(service.plan_from_query("도쿄 4박 야경 여행", include_live_data=False))


def test_content_token_restores_after_new_service_instance() -> None:
    first = PlannerService()
    plan = make_plan(first)
    token = first.summarize(plan)["content_token"]
    restarted = PlannerService()
    restored = restarted.decode(token)
    assert restored["plan_id"] == plan["plan_id"]
    assert restored["days"] == plan["days"]


def test_revision_conflict_and_map_query_rebuild() -> None:
    service = PlannerService()
    plan = make_plan(service)
    token = service.summarize(plan)["content_token"]
    conflict = service.mutate(token, 0, [])
    assert conflict["code"] == "REVISION_CONFLICT"

    activity = plan["days"][0]["activities"][0]
    updated = service.mutate(
        token,
        1,
        [
            {
                "op": "update_activity",
                "day": 1,
                "activity_id": activity["id"],
                "changes": {"title": "가마쿠라 대불", "location": "Kotoku-in, Kamakura"},
            }
        ],
    )
    assert updated["revision"] == 2
    next_plan = service.decode(updated["content_token"])
    changed = next_plan["days"][0]["activities"][0]
    assert "Kotoku-in" in changed["map_query"]
    assert changed["map_url"] != activity["map_url"]
    second_writer = service.mutate(token, 1, [])
    assert second_writer["code"] == "REVISION_CONFLICT"


def test_legacy_icons_and_large_html_are_separate_exports() -> None:
    service = PlannerService()
    plan = make_plan(service)
    payload = legacy_v3_payload(plan)
    allowed = set(service.catalog.allowed_icons)
    assert payload["v"] == 3
    assert len(payload["i"]) == len(plan["days"])
    assert all(activity["k"] in allowed for day in payload["i"] for activity in day["a"])
    html = render_export_html(plan, service.catalog)
    assert "<!doctype html>" in html.lower()
    assert plan["title"] in html


def test_tampered_token_rejects_executable_map_urls() -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan["days"][0]["activities"][0]["map_url"] = "javascript:alert(document.domain)"
    with pytest.raises(TokenError, match="map_url must be an absolute http"):
        service.decode(encode_content_token(plan))
