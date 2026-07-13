from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server import TRANSPORT_SECURITY, mcp, plan_trip, service


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
    assert len(tools) == 7
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
