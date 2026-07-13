import asyncio

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
