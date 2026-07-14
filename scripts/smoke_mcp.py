#!/usr/bin/env python3
"""Real Streamable HTTP contract smoke test for the travel MCP server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
import zlib

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "list_destinations",
    "get_city_guide",
    "get_route_options",
    "plan_trip",
    "create_plan",
    "mutate_plan",
    "get_plan",
    "export_plan",
    "validate_plan",
}

ROUTE_MODES = ("transit", "walking", "driving")
GMARKET_FONT_ASSETS = (
    "GmarketSansLight.woff2",
    "GmarketSansMedium.woff2",
    "GmarketSansBold.woff2",
)


def decode_smoke_token(token: str) -> dict[str, Any]:
    """Decode only a token returned by this smoke run so it can be tampered with."""

    encoded = token.removeprefix("tp1.")
    compressed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return json.loads(zlib.decompress(compressed).decode("utf-8"))


def encode_smoke_token(plan: dict[str, Any]) -> str:
    raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "tp1." + base64.urlsafe_b64encode(zlib.compress(raw)).decode("ascii").rstrip("=")


def choose_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_health(base_url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}/health")
                response.raise_for_status()
                payload = response.json()
                if payload.get("ok") is True:
                    return payload
            except Exception as exc:  # server may still be binding
                last_error = exc
            await asyncio.sleep(0.1)
    raise AssertionError(f"server did not become healthy: {last_error}")


def structured(result: types.CallToolResult) -> dict[str, Any]:
    assert result.isError is not True, result
    assert isinstance(result.structuredContent, dict), "tool omitted structuredContent"
    assert "result" not in result.structuredContent, "tool output was wrapped instead of using its object schema"
    return result.structuredContent


def assert_directions_url(
    value: str,
    *,
    origin: str,
    destination: str,
    travelmode: str,
    waypoints: list[str] | None = None,
) -> None:
    """Verify an official Google directions URL without relying on string escaping."""

    parsed = urlsplit(value)
    assert parsed.scheme == "https", value
    assert parsed.netloc == "www.google.com", value
    assert parsed.path == "/maps/dir/", value
    expected_query = {
        "api": ["1"],
        "origin": [origin],
        "destination": [destination],
        "travelmode": [travelmode],
    }
    if waypoints:
        expected_query["waypoints"] = ["|".join(waypoints)]
    assert parse_qs(parsed.query, keep_blank_values=True) == expected_query, value


def assert_plan_routes(plan: dict[str, Any]) -> None:
    """Check every adjacent activity and all three route modes in a plan response."""

    checked_legs = 0
    for day in plan["itinerary"]:
        activities = day["activities"]
        legs = day["legs"]
        assert len(legs) == max(0, len(activities) - 1)

        if len(activities) > 1:
            map_queries = [activity["map_query"] for activity in activities]
            assert_directions_url(
                day["route_map_url"],
                origin=map_queries[0],
                destination=map_queries[-1],
                travelmode="transit",
                waypoints=map_queries[1:-1],
            )

        for index, leg in enumerate(legs):
            origin = activities[index]
            destination = activities[index + 1]
            assert leg["from"]["activity_id"] == origin["activity_id"]
            assert leg["from"]["map_query"] == origin["map_query"]
            assert leg["to"]["activity_id"] == destination["activity_id"]
            assert leg["to"]["map_query"] == destination["map_query"]
            assert set(leg["route_urls"]) == set(ROUTE_MODES)
            for mode in ROUTE_MODES:
                assert_directions_url(
                    leg["route_urls"][mode],
                    origin=origin["map_query"],
                    destination=destination["map_query"],
                    travelmode=mode,
                )
            checked_legs += 1
    assert checked_legs > 0, "plan smoke did not exercise any route leg"


def assert_paris_vienna_boundary(plan: dict[str, Any]) -> None:
    """Keep the transfer-day contract visible at the public MCP boundary."""

    boundary = next(
        day for day in plan["itinerary"]
        if day["destinations"] == ["paris", "austria"]
    )
    activities = boundary["activities"]
    assert len(activities) == 3
    assert [activity["destination_id"] for activity in activities] == [
        "paris", "austria", "austria"
    ]
    assert activities[0]["time"] < "12:00"
    assert activities[1]["time"] == "13:00"
    assert activities[1]["title"] == "파리 → 빈 이동"
    assert activities[1]["memo"] == "다도시 구간 이동일"
    assert activities[2]["time"] >= "17:00"
    assert sum(
        left["destination_id"] != right["destination_id"]
        for left, right in zip(activities, activities[1:])
    ) == 1


async def exercise_server(base_url: str) -> None:
    health = await wait_for_health(base_url, timeout=15.0)
    assert health["service"] == "travelCityPlanner"
    assert health["destinations"] == 69

    async with httpx.AsyncClient(timeout=5.0) as security_client:
        rejected = await security_client.get(
            f"{base_url}/mcp",
            headers={"Host": "attacker.invalid", "Accept": "text/event-stream"},
        )
        assert rejected.status_code == 421, "DNS rebinding protection accepted an unknown Host"

    async with streamable_http_client(f"{base_url}/mcp") as (read_stream, write_stream, _session_id):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "travelCityPlanner"

            tools = (await session.list_tools()).tools
            by_name = {tool.name: tool for tool in tools}
            assert set(by_name) == EXPECTED_TOOLS
            for name, tool in by_name.items():
                assert tool.title, f"{name} is missing a human-readable title"
                assert tool.annotations is not None, f"{name} is missing ToolAnnotations"
                assert tool.annotations.readOnlyHint is not None
                assert tool.annotations.destructiveHint is not None
                assert tool.annotations.idempotentHint is not None
                assert tool.annotations.openWorldHint is not None
                schema_text = json.dumps(tool.outputSchema or {}, sort_keys=True)
                assert '"ok"' in schema_text, f"{name} outputSchema is not a concrete object contract"

            full_catalog = structured(
                await session.call_tool("list_destinations", {"limit": 100})
            )
            assert full_catalog["ok"] is True
            assert full_catalog["count"] == 69
            assert full_catalog["catalog_total"] == 69
            catalog_ids = [item["id"] for item in full_catalog["destinations"]]
            assert len(catalog_ids) == len(set(catalog_ids)) == 69
            catalog_by_id = {
                item["id"]: item for item in full_catalog["destinations"]
            }
            assert {"tokyo", "taipei"} <= set(catalog_by_id)

            destinations = structured(
                await session.call_tool("list_destinations", {"search": "도쿄", "limit": 5})
            )
            assert destinations["ok"] is True
            assert destinations["catalog_total"] == 69
            assert [item["id"] for item in destinations["destinations"]] == ["tokyo"]
            assert destinations["destinations"][0]["hero_url"].startswith(
                f"{base_url}/viewer/assets/heroes/"
            )

            guide = structured(
                await session.call_tool(
                    "get_city_guide",
                    {"destination_id": "괌", "phrase_query": "감사"},
                )
            )
            assert guide["ok"] is True
            assert guide["destination"]["id"] == "guam"
            assert guide["destination"]["hero_url"].startswith(base_url)
            assert guide["clocks"]["seoul"]["time_zone"] == "Asia/Seoul"
            assert guide["clocks"]["local"]["time_zone"] == guide["destination"]["time_zone"]
            assert guide["exchange"]["currency"] == "USD"
            assert guide["exchange"]["fetched_at"]
            assert guide["exchange"]["source"]

            route_options = structured(
                await session.call_tool(
                    "get_route_options",
                    {
                        "from_place": "도쿄역",
                        "to_place": "센소지",
                        "suggested_mode": "walking",
                    },
                )
            )
            assert route_options["ok"] is True
            assert route_options["from"] == "도쿄역"
            assert route_options["to"] == "센소지"
            assert route_options["suggested_mode"] == "walking"
            assert set(route_options["route_urls"]) == set(ROUTE_MODES)
            for mode in ROUTE_MODES:
                assert_directions_url(
                    route_options["route_urls"][mode],
                    origin="도쿄역",
                    destination="센소지",
                    travelmode=mode,
                )

            planned = structured(
                await session.call_tool(
                    "plan_trip",
                    {
                        "query": "도쿄로 4-5박 정도 머무를건데 애니와 맛집 위주로 추천 플래너 짜줘",
                        "include_live_data": False,
                    },
                )
            )
            assert planned["ok"] is True
            assert planned["duration"]["selected_nights"] == 5
            assert planned["duration"]["selected_days"] == 6
            assert planned["shorter_variant"]["nights"] == 4
            assert len(planned["itinerary"]) == 6
            first_day = planned["itinerary"][0]
            assert len(first_day["legs"]) == len(first_day["activities"]) - 1
            assert first_day["activities"][0]["activity_id"].startswith("act-")
            assert first_day["activities"][0]["destination_id"] == "tokyo"
            assert first_day["activities"][0]["location"]
            assert_plan_routes(planned)
            assert "html" not in planned or planned["html"] is None
            assert planned["content_token"].startswith("tp1.")
            assert planned["viewer_url"].startswith(f"{base_url}/viewer#plan=tp1.")
            token = planned["content_token"]

            reopened = structured(await session.call_tool("get_plan", {"content_token": token}))
            assert reopened["plan_id"] == planned["plan_id"]
            assert reopened["revision"] == 1

            conflict = structured(
                await session.call_tool(
                    "mutate_plan",
                    {"content_token": token, "expected_revision": 0, "operations_json": "[]"},
                )
            )
            assert conflict["ok"] is False
            assert conflict["code"] == "REVISION_CONFLICT"

            updated = structured(
                await session.call_tool(
                    "mutate_plan",
                    {
                        "content_token": token,
                        "expected_revision": 1,
                        "operations_json": json.dumps(
                            [{"op": "rename_day", "day": 1, "title": "스모크 테스트 Day 1"}],
                            ensure_ascii=False,
                        ),
                    },
                )
            )
            assert updated["ok"] is True and updated["revision"] == 2
            assert updated["content_token"] != token
            updated_token = updated["content_token"]

            stale = structured(
                await session.call_tool(
                    "mutate_plan",
                    {"content_token": token, "expected_revision": 1, "operations_json": "[]"},
                )
            )
            assert stale["ok"] is False
            assert stale["code"] == "REVISION_CONFLICT"
            assert stale["head_revision"] == 2

            validation = structured(
                await session.call_tool("validate_plan", {"content_token": updated_token})
            )
            assert validation["ok"] is True
            assert validation["revision"] == 2
            assert validation["days"] == 6
            assert validation["token_survives_restart"] is True

            html_export = structured(
                await session.call_tool(
                    "export_plan", {"content_token": updated_token, "format": "html"}
                )
            )
            assert html_export["ok"] is True
            assert html_export["filename"].endswith(".html")
            assert "<!doctype html>" in html_export["html"].lower()

            legacy = structured(
                await session.call_tool(
                    "export_plan", {"content_token": updated_token, "format": "legacy_v3"}
                )
            )
            assert legacy["ok"] is True
            assert legacy["payload"]["v"] == 3
            assert legacy["share_url"].startswith("https://")

            multi_created = structured(
                await session.call_tool(
                    "create_plan",
                    {
                        "segments_json": json.dumps(
                            [
                                {"destination_id": "paris", "nights": 2},
                                {"destination_id": "austria", "nights": 2},
                            ]
                        ),
                        "preferences": "시장, 야경, 로컬 음식",
                        "pace": "balanced",
                        "include_live_data": False,
                    },
                )
            )
            assert multi_created["ok"] is True
            assert multi_created["revision"] == 1
            assert multi_created["title"] == "파리 · 빈 4박 5일"
            assert multi_created["duration"]["selected_nights"] == 4
            assert multi_created["duration"]["selected_days"] == 5
            assert [
                segment["destination_id"] for segment in multi_created["segments"]
            ] == ["paris", "austria"]
            assert len(multi_created["itinerary"]) == 5
            itinerary_destination_ids = {
                destination_id
                for day in multi_created["itinerary"]
                for destination_id in day["destinations"]
            }
            assert itinerary_destination_ids == {
                "paris",
                "austria",
            }
            assert_plan_routes(multi_created)
            assert_paris_vienna_boundary(multi_created)
            multi_token = multi_created["content_token"]
            assert multi_token.startswith("tp1.")

            multi_reopened = structured(
                await session.call_tool(
                    "get_plan",
                    {"content_token": multi_token, "include_itinerary": True},
                )
            )
            assert multi_reopened["ok"] is True
            assert multi_reopened["plan_id"] == multi_created["plan_id"]
            assert multi_reopened["revision"] == multi_created["revision"]
            assert multi_reopened["content_token"] == multi_token
            assert [
                segment["destination_id"] for segment in multi_reopened["segments"]
            ] == ["paris", "austria"]
            assert_plan_routes(multi_reopened)
            assert_paris_vienna_boundary(multi_reopened)

            multi_validation = structured(
                await session.call_tool(
                    "validate_plan", {"content_token": multi_token}
                )
            )
            assert multi_validation["ok"] is True
            assert multi_validation["plan_id"] == multi_created["plan_id"]
            assert multi_validation["revision"] == 1
            assert multi_validation["days"] == 5
            assert multi_validation["segments"] == 2
            assert multi_validation["catalog_digest"] == full_catalog["catalog_digest"]
            assert multi_validation["token_survives_restart"] is True

            nested_attack = decode_smoke_token(multi_token)
            nested_attack["days"][0]["legs"][0]["route_urls"]["transit"] = {}
            malicious_validation = structured(
                await session.call_tool(
                    "validate_plan",
                    {"content_token": encode_smoke_token(nested_attack)},
                )
            )
            assert malicious_validation["ok"] is False
            assert malicious_validation["error"] == "TokenError"
            # A malformed token is contained as a structured MCP result and
            # cannot take down the server or poison a valid token session.
            assert (await session.call_tool(
                "get_plan", {"content_token": multi_token}
            )).isError is not True

            multi_html_export = structured(
                await session.call_tool(
                    "export_plan", {"content_token": multi_token, "format": "html"}
                )
            )
            assert multi_html_export["ok"] is True
            assert multi_html_export["filename"].endswith(".html")
            multi_html = multi_html_export["html"]
            assert "<!doctype html>" in multi_html.lower()
            assert 'data-city-id="paris"' in multi_html
            assert 'data-city-id="austria"' in multi_html
            assert "Gmarket" in multi_html
            assert "data:font/woff2;base64," in multi_html
            assert "data:image/jpeg;base64," in multi_html

            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                viewer = await client.get(f"{base_url}/viewer")
                viewer.raise_for_status()
                assert viewer.headers["content-type"].startswith("text/html")

                catalog = await client.get(f"{base_url}/data/destinations.json")
                catalog.raise_for_status()
                catalog_payload = catalog.json()
                assert catalog_payload["destinationCount"] == 69
                assert len(catalog_payload["destinations"]) == 69
                assert set(catalog_payload["destinations"]) == set(catalog_ids)

                for font_name in GMARKET_FONT_ASSETS:
                    font = await client.get(
                        f"{base_url}/viewer/assets/fonts/{font_name}"
                    )
                    font.raise_for_status()
                    assert font.status_code == 200
                    assert font.content.startswith(b"wOF2"), font_name

                hero_urls = [item["hero_url"] for item in catalog_by_id.values()]
                assert len(hero_urls) == len(set(hero_urls)) == 69
                for destination_id, destination in catalog_by_id.items():
                    async with client.stream("GET", destination["hero_url"]) as hero:
                        hero.raise_for_status()
                        assert hero.status_code == 200
                        assert hero.headers["content-type"].startswith("image/jpeg")
                        prefix = bytearray()
                        async for chunk in hero.aiter_bytes():
                            prefix.extend(chunk)
                            if len(prefix) >= 3:
                                break
                        assert bytes(prefix[:3]) == b"\xff\xd8\xff", destination_id

                city_guide = await client.get(
                    f"{base_url}/api/city-guide/tokyo",
                    params={"phrase_query": "계산"},
                )
                city_guide.raise_for_status()
                guide_payload = city_guide.json()
                assert guide_payload["ok"] is True
                assert guide_payload["destination"]["id"] == "tokyo"
                assert guide_payload["destination"]["hero_url"].startswith(base_url)
                assert all("계산" in item["meaning"] for item in guide_payload["phrases"])

                missing_guide = await client.get(
                    f"{base_url}/api/city-guide/not-a-real-city"
                )
                assert missing_guide.status_code == 404

                redirect = await client.get(f"{base_url}/view/{updated_token}")
                assert redirect.status_code == 307
                assert redirect.headers["location"].startswith("/viewer#plan=tp1.")


async def async_main(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    port = args.port or choose_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PUBLIC_BASE_URL": base_url,
            "MCP_TRANSPORT": "streamable-http",
            "LIVE_DATA_TIMEOUT": "0.5",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )

    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            [args.python, "server.py"],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            await exercise_server(base_url)
            assert process.poll() is None, "server exited during smoke test"
        except Exception:
            log.seek(0)
            print("--- server log ---", file=sys.stderr)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    print(f"PASS travel MCP Streamable HTTP smoke ({base_url})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable, help="Python executable used to launch server.py")
    parser.add_argument("--port", type=int, default=0, help="Port to verify; 0 chooses an unused local port")
    asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
