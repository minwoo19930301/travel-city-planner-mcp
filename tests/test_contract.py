from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server import (
    TRANSPORT_SECURITY,
    get_city_guide,
    get_route_options,
    mcp,
    plan_trip,
    service,
)


class UndatedLiveData:
    async def collect(self, _segments, _catalog, enabled=True):
        assert enabled is True
        return {
            "weather": {
                "status": "date_required",
                "message": "날짜 미정 — 현재 날씨를 여행 날씨처럼 표시하지 않았습니다.",
                "segments": [],
            },
            "exchange": {"status": "skipped", "rates": []},
        }


def test_mcp_tool_contracts_are_titled_annotated_and_structured() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 9
    for tool in tools:
        assert tool.title
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is not None
        assert tool.annotations.destructiveHint is not None
        assert tool.annotations.idempotentHint is not None
        assert tool.annotations.openWorldHint is not None
        assert '"ok"' in json.dumps(tool.outputSchema, sort_keys=True)


def test_transport_security_is_enabled_with_local_defaults() -> None:
    assert TRANSPORT_SECURITY.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in TRANSPORT_SECURITY.allowed_hosts
    assert "http://127.0.0.1:*" in TRANSPORT_SECURITY.allowed_origins


def test_default_undated_live_data_response_matches_output_schema(monkeypatch) -> None:
    monkeypatch.setattr(service, "live_data", UndatedLiveData())
    output = asyncio.run(plan_trip("도쿄 4박 여행"))
    assert output.ok is True
    assert output.weather is not None
    assert output.weather.status == "date_required"
    assert "현재 날씨" in (output.weather.message or "")


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


def test_city_guide_and_route_tools_match_concrete_output_contracts(monkeypatch) -> None:
    monkeypatch.setattr(service, "live_data", GuideLiveData())
    guide = asyncio.run(get_city_guide("빈", "감사"))
    assert guide.ok is True
    assert guide.destination is not None
    assert guide.destination.id == "austria"
    assert guide.clocks is not None
    assert guide.clocks.seoul.time_zone == "Asia/Seoul"
    assert guide.exchange is not None
    assert guide.exchange.currency == "EUR"

    routes = get_route_options("빈 중앙역", "쇤브룬 궁전", "transit")
    assert routes.ok is True
    assert routes.from_ == "빈 중앙역"
    assert routes.to == "쇤브룬 궁전"
    assert routes.route_urls is not None
    assert routes.route_urls.walking.startswith("https://www.google.com/maps/dir/")


def test_deployment_contract_is_pinned_and_non_root() -> None:
    root = Path(__file__).resolve().parents[1]
    requirements = [
        line.strip()
        for line in (root / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements and all("==" in requirement for requirement in requirements)
    dockerfile = (root / "Dockerfile").read_text()
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PORT=8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "USER app" in dockerfile
