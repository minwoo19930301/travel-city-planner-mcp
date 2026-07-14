import asyncio
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from planner import PlannerService
from planner.live_data import LiveDataProvider


def run(coro):
    return asyncio.run(coro)


def test_korean_duration_range_selects_five_nights_and_shorter_hint() -> None:
    service = PlannerService()
    plan = run(
        service.plan_from_query(
            "도쿄로 4-5박 정도 머무를건데 애니와 맛집 위주로 추천 플래너 짜줘",
            include_live_data=False,
        )
    )
    assert plan["title"] == "도쿄 5박 6일"
    assert len(plan["days"]) == 6
    assert plan["duration"]["requested_range"] == [4, 5]
    assert plan["shorter_variant"]["nights"] == 4
    assert "4박 5일" in plan["shorter_variant"]["hint"]
    assert any("여유일" in day["title"] for day in plan["days"])


def test_default_summary_excludes_html_and_uses_place_queries() -> None:
    service = PlannerService()
    plan = run(service.plan_from_query("도쿄 4박 여행", include_live_data=False))
    summary = service.summarize(plan)
    assert "html" not in summary
    assert summary["content_token"].startswith("tp1.")
    first = plan["days"][0]["activities"][0]
    assert first["location"] in first["map_query"]
    assert "query=" in first["map_url"]
    compact = summary["itinerary"][0]["activities"][0]
    assert compact["activity_id"] == first["id"]
    assert compact["destination_id"] == "tokyo"
    assert compact["location"] == first["location"]
    assert compact["map_query"] == first["map_query"]


def test_every_adjacent_activity_has_three_google_route_options() -> None:
    service = PlannerService()
    plan = run(service.plan_from_query("도쿄 4박 여행", include_live_data=False))
    for day in plan["days"]:
        assert len(day["legs"]) == max(0, len(day["activities"]) - 1)
        for index, leg in enumerate(day["legs"]):
            assert leg["from"]["activity_id"] == day["activities"][index]["id"]
            assert leg["to"]["activity_id"] == day["activities"][index + 1]["id"]
            assert leg["suggested_mode"] == "transit"
            assert set(leg["route_urls"]) == {"transit", "walking", "driving"}
            for mode, url in leg["route_urls"].items():
                parsed = urlsplit(url)
                assert parsed.scheme == "https"
                assert parsed.hostname == "www.google.com"
                assert parse_qs(parsed.query)["travelmode"] == [mode]


def test_multicity_plan_merges_transfer_day() -> None:
    service = PlannerService()
    plan = run(
        service.create_plan(
            [
                {"destination_id": "tokyo", "nights": 3},
                {"destination_id": "taipei", "nights": 2},
            ],
            start_date="2026-09-01",
            include_live_data=False,
        )
    )
    assert plan["duration"]["selected_nights"] == 5
    assert len(plan["days"]) == 6
    assert len(plan["segments"]) == 2
    transfer_days = [day for day in plan["days"] if len(day["destination_ids"]) > 1]
    assert len(transfer_days) == 1
    assert any("이동" in item["title"] for item in transfer_days[0]["activities"])


def test_undated_weather_never_substitutes_current_conditions() -> None:
    service = PlannerService()
    provider = LiveDataProvider()
    segment = {"destination_id": "tokyo", "start_date": None, "end_date": None}
    weather = run(provider._weather([segment], service.catalog, client=None))
    assert weather["status"] == "date_required"
    assert "현재 날씨" in weather["message"]


class GuideLiveData:
    async def exchange_for_currency(self, currency):
        return {
            "status": "live",
            "currency": currency,
            "krw_per_unit": 9.125,
            "fetched_at": "2026-07-14T00:00:00+00:00",
            "source": "https://example.test/rates",
            "updated_at": "2026-07-13T00:00:00+00:00",
        }


def test_city_guide_exposes_city_media_clocks_phrases_map_and_exchange() -> None:
    service = PlannerService(live_data=GuideLiveData())
    guide = run(service.city_guide("도쿄", phrase_query="계산"))
    assert guide["destination"]["id"] == "tokyo"
    assert guide["destination"]["hero_image"].endswith("tokyo.jpg")
    assert guide["destination"]["time_zone"] == "Asia/Tokyo"
    assert guide["language"] == "日本語"
    assert guide["phrases"]
    assert all("계산" in phrase["meaning"] for phrase in guide["phrases"])
    assert urlsplit(guide["map_url"]).hostname == "www.google.com"
    assert guide["exchange"]["currency"] == "JPY"
    assert guide["exchange"]["krw_per_unit"] == 9.125
    seoul = datetime.fromisoformat(guide["clocks"]["seoul"]["iso"])
    local = datetime.fromisoformat(guide["clocks"]["local"]["iso"])
    assert seoul.utcoffset() is not None
    assert local.utcoffset() is not None


def test_all_69_city_guides_have_valid_timezone_image_and_language_data() -> None:
    service = PlannerService(live_data=GuideLiveData())
    for destination in service.catalog.destinations.values():
        guide = run(service.city_guide(destination["cityKo"]))
        assert guide["destination"]["id"] == destination["id"]
        assert guide["destination"]["hero_image"] == destination["heroImage"]
        assert guide["destination"]["hero_url"].endswith(destination["heroImage"])
        assert guide["clocks"]["local"]["time_zone"] == destination["timeZone"]
        assert guide["language"] == destination["phraseLabel"]
        assert guide["phrases"] == destination["phrases"]


def test_arbitrary_route_options_are_official_google_maps_urls() -> None:
    service = PlannerService()
    result = service.route_options("도쿄역", "센소지", "walking")
    assert result["from"] == "도쿄역"
    assert result["to"] == "센소지"
    assert result["suggested_mode"] == "walking"
    assert set(result["route_urls"]) == {"transit", "walking", "driving"}
    assert all(
        urlsplit(value).hostname == "www.google.com"
        for value in result["route_urls"].values()
    )
