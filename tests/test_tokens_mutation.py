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
    assert len(next_plan["days"][0]["legs"]) == len(next_plan["days"][0]["activities"]) - 1
    assert next_plan["days"][0]["legs"][0]["from"]["activity_id"] == changed["id"]
    assert "Kotoku-in" in next_plan["days"][0]["legs"][0]["route_urls"]["transit"]
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


def test_legacy_v1_token_without_legs_is_migrated_deterministically() -> None:
    service = PlannerService()
    plan = make_plan(service)
    for day in plan["days"]:
        day.pop("legs")
    restored = service.decode(encode_content_token(plan))
    assert all(
        len(day["legs"]) == max(0, len(day["activities"]) - 1)
        for day in restored["days"]
    )
    assert service.decode(encode_content_token(restored))["days"] == restored["days"]


def test_tampered_leg_url_rejects_non_google_and_mismatched_routes() -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan["days"][0]["legs"][0]["route_urls"]["walking"] = (
        "https://example.com/maps/dir/?api=1"
    )
    with pytest.raises(TokenError, match="official Google Maps host"):
        service.decode(encode_content_token(plan))

    plan = make_plan(service)
    plan["days"][0]["legs"][0]["route_urls"]["walking"] = (
        "https://www.google.com/maps/dir/?api=1&origin=wrong&destination=wrong&travelmode=walking"
    )
    with pytest.raises(TokenError, match="do not match adjacent activities"):
        service.decode(encode_content_token(plan))


def test_standalone_export_includes_each_city_live_clock_fx_phrases_and_legs() -> None:
    service = PlannerService()
    plan = run(
        service.create_plan(
            segments=[
                {"destination_id": "tokyo", "nights": 2},
                {"destination_id": "taipei", "nights": 2},
            ],
            include_live_data=False,
        )
    )
    plan["live_data"]["exchange"] = {
        "status": "live",
        "base": "KRW",
        "fetched_at": "2026-07-14T01:02:03+00:00",
        "rates": [
            {
                "status": "live",
                "currency": "JPY",
                "krw_per_unit": 9.2345,
                "fetched_at": "2026-07-14T01:02:03+00:00",
                "updated_at": "Mon, 14 Jul 2026 00:00:01 +0000",
            },
            {
                "status": "live",
                "currency": "TWD",
                "krw_per_unit": 42.125,
                "fetched_at": "2026-07-14T01:02:03+00:00",
                "updated_at": "Mon, 14 Jul 2026 00:00:01 +0000",
            },
        ],
    }

    export = render_export_html(plan, service.catalog)

    assert "/viewer/assets/heroes/tokyo.jpg" in export
    assert "/viewer/assets/heroes/taipei.jpg" in export
    assert 'data-time-zone="Asia/Seoul"' in export
    assert 'data-time-zone="Asia/Tokyo"' in export
    assert 'data-time-zone="Asia/Taipei"' in export
    assert "new Intl.DateTimeFormat" in export
    assert "window.setInterval(updateClocks, 1000)" in export
    assert "1 JPY = ₩9.2345" in export
    assert "1 TWD = ₩42.125" in export
    assert "Mon, 14 Jul 2026 00:00:01 +0000" in export
    assert "すみません" in export
    assert "你好" in export
    assert "대중교통" in export and "도보" in export and "차량" in export
    assert "travelmode=transit" in export
    assert "travelmode=walking" in export
    assert "travelmode=driving" in export


def test_standalone_export_escapes_copy_and_rebuilds_unsafe_links() -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan["title"] = '</title><script id="injected">alert(1)</script>'
    plan["days"][0]["activities"][0]["map_url"] = "javascript:alert(1)"
    for route in plan["days"][0]["legs"][0]["route_urls"]:
        plan["days"][0]["legs"][0]["route_urls"][route] = "javascript:alert(1)"

    export = render_export_html(
        plan,
        service.catalog,
        asset_base_url="javascript:alert(1)",
    )

    assert '<script id="injected">' not in export
    assert 'href="javascript:' not in export
    assert 'src="javascript:' not in export
    assert "https://www.google.com/maps/search/?" in export
    assert "https://www.google.com/maps/dir/?" in export
