import asyncio
import html as html_lib
import re
from urllib.parse import parse_qs, urlsplit

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


def test_every_catalog_city_round_trips_a_v1_token_and_export() -> None:
    async def exercise() -> None:
        service = PlannerService()
        restarted = PlannerService()
        for destination_id in service.catalog.destinations:
            plan = await service.create_plan(
                [{"destination_id": destination_id, "nights": 1}],
                include_live_data=False,
            )
            token = service.summarize(plan)["content_token"]
            restored = restarted.decode(token)
            assert restored["segments"][0]["destination_id"] == destination_id
            assert restarted.summarize(restored)["content_token"].startswith("tp1.")
            assert "<!doctype html>" in render_export_html(
                restored, restarted.catalog, asset_base_url="/assets/"
            ).lower()

    run(exercise())


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


def test_tampered_token_rejects_missing_structure_and_google_lookalikes() -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan["segments"] = []
    with pytest.raises(TokenError, match="no segments"):
        service.decode(encode_content_token(plan))

    plan = make_plan(service)
    plan["days"][0]["activities"][0]["map_url"] = "https://www.google.com.evil.test/maps/search/?api=1"
    with pytest.raises(TokenError, match="official Google Maps host"):
        service.decode(encode_content_token(plan))


@pytest.mark.parametrize(
    ("path", "malicious_value"),
    [
        (("shorter_variant",), {"oops": 1}),
        (("live_data", "weather"), []),
        (("live_data", "weather"), {"status": "skipped", "segments": [None]}),
        (("live_data", "exchange"), []),
        (("live_data", "exchange", "rates"), None),
        (("live_data", "exchange"), {"status": "skipped", "rates": [None]}),
        (("days", 0, "route_map_url"), {}),
        (("days", 0, "activities", 0, "map_url"), []),
        (("days", 0, "activities", 0), "not-an-activity"),
        (("days", 0, "legs", 0), "not-a-leg"),
        (("days", 0, "legs", 0, "route_urls"), []),
        (("days", 0, "legs", 0, "route_urls", "transit"), {}),
        (("days", 0, "legs", 0, "route_urls", "walking"), []),
        (("days", 0, "legs", 0, "from"), []),
        (("days", 0, "legs", 0, "to"), "not-an-endpoint"),
    ],
)
def test_malicious_nested_token_structures_always_raise_token_error(
    path: tuple[object, ...], malicious_value: object
) -> None:
    service = PlannerService()
    plan = make_plan(service)
    parent = plan
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = malicious_value

    with pytest.raises(TokenError):
        service.decode(encode_content_token(plan))


def test_legacy_v1_optional_live_data_and_shorter_variant_remain_compatible() -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan.pop("shorter_variant")
    plan["live_data"] = {}

    restored = service.decode(encode_content_token(plan))

    assert "shorter_variant" not in restored
    assert restored["live_data"] == {}


@pytest.mark.parametrize("malicious_activity", ([], {}, "not-an-activity"))
def test_malformed_legacy_structure_does_not_crash_during_leg_migration(
    malicious_activity: object,
) -> None:
    service = PlannerService()
    plan = make_plan(service)
    plan["days"][0].pop("legs")
    plan["days"][0]["activities"][0] = malicious_activity

    with pytest.raises(TokenError):
        service.decode(encode_content_token(plan))


def test_content_token_revision_must_be_a_positive_integer() -> None:
    service = PlannerService()
    for revision in (0, -1, True, 1.5, "1"):
        plan = make_plan(service)
        plan["revision"] = revision
        with pytest.raises(TokenError, match="plan identity"):
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


def test_google_map_routes_preserve_a_to_b_and_mode_queries() -> None:
    service = PlannerService()
    plan = make_plan(service)
    leg = plan["days"][0]["legs"][0]
    for mode, url in leg["route_urls"].items():
        params = parse_qs(urlsplit(url).query)
        assert params["api"] == ["1"]
        assert params["origin"] == [leg["from"]["map_query"]]
        assert params["destination"] == [leg["to"]["map_query"]]
        assert params["travelmode"] == [mode]


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

    assert "data:image/jpeg;base64," in export
    assert "data:font/woff2;base64," in export
    assert "Gmarket" in export
    assert export.count("<style>") == 1
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
    assert "data-city-select" in export
    assert "phrase-search" in export
    assert "fx-calculator" in export
    assert 'class="clock-row" data-city-id="tokyo"' in export
    assert 'class="clock-row" data-city-id="taipei"' in export
    assert 'data-fx-row data-city-id="tokyo"' in export
    assert 'data-fx-row data-city-id="taipei"' in export
    assert 'data-fx-refresh-status' in export
    assert '.clock-row[data-city-id],.fx-row[data-city-id]' in export
    assert 'body:before{z-index:0;pointer-events:none}' in export
    assert 'body:after{z-index:1;pointer-events:none}' in export
    assert 'main{position:relative;z-index:2}' in export
    assert 'body:after{background:rgba(5,8,5,.8)}' in export
    assert 'h1{max-width:13ch;overflow-wrap:anywhere;word-break:keep-all}' in export
    assert 'background:var(--control-bg);color:var(--control-text)' in export
    assert 'data-active-city-summary' in export
    assert 'summary.textContent = citySummary.textContent' in export
    assert 'valueNode.textContent = formatRate' in export
    assert 'metaNode.textContent =' in export
    assert '독립 HTML(file://)에서는 환율 실시간 갱신을 사용할 수 없습니다.' in export
    assert '저장된 snapshot을 유지합니다.' in export

    embedded_heroes = re.findall(
        r"data:image/jpeg;base64,[A-Za-z0-9+/=]+",
        export,
    )
    assert len(embedded_heroes) == 2
    assert len(set(embedded_heroes)) == 2
    assert all(export.count(hero) == 1 for hero in embedded_heroes)
    assert '<img src="data:image/jpeg;base64,' not in export
    assert 'data-hero="data:image/jpeg;base64,' not in export


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

    asset_attack = render_export_html(
        plan,
        service.catalog,
        asset_base_url='https://assets.example.test/"></style><script id="asset-injected">',
    )
    assert "asset-injected" not in asset_attack

    mismatched = make_plan(service)
    first_day = mismatched["days"][0]
    wrong_directions = (
        "https://www.google.com/maps/dir/?api=1&origin=WRONG+ORIGIN"
        "&destination=WRONG+DESTINATION&travelmode=transit"
    )
    wrong_search = "https://www.google.com/maps/search/?api=1&query=WRONG+PLACE"
    first_day["route_map_url"] = wrong_directions
    first_day["activities"][0]["map_url"] = wrong_search
    for route_urls in (leg["route_urls"] for leg in first_day["legs"]):
        for mode in route_urls:
            route_urls[mode] = wrong_directions

    mismatched_export = render_export_html(mismatched, service.catalog)
    hrefs = [
        html_lib.unescape(value)
        for value in re.findall(r'href="([^"]+)"', mismatched_export)
    ]
    assert not any("WRONG" in href for href in hrefs)
    map_queries = [
        parse_qs(urlsplit(href).query)
        for href in hrefs
        if urlsplit(href).path == "/maps/search/"
    ]
    assert {
        "api": ["1"],
        "query": [first_day["activities"][0]["map_query"]],
    } in map_queries
    actual_directions = {
        (
            params.get("origin", [None])[0],
            params.get("destination", [None])[0],
            params.get("travelmode", [None])[0],
        )
        for href in hrefs
        if urlsplit(href).path == "/maps/dir/"
        for params in [parse_qs(urlsplit(href).query)]
    }
    expected_directions = {
        (
            first_day["activities"][index]["map_query"],
            first_day["activities"][index + 1]["map_query"],
            mode,
        )
        for index in range(len(first_day["activities"]) - 1)
        for mode in ("transit", "walking", "driving")
    }
    assert expected_directions <= actual_directions
