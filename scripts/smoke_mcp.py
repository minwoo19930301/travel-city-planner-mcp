#!/usr/bin/env python3
"""Real Streamable HTTP contract smoke test for the travel MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

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
            assert set(route_options["route_urls"]) == {"transit", "walking", "driving"}

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

            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                viewer = await client.get(f"{base_url}/viewer")
                viewer.raise_for_status()
                assert viewer.headers["content-type"].startswith("text/html")

                catalog = await client.get(f"{base_url}/data/destinations.json")
                catalog.raise_for_status()
                assert catalog.json()["destinationCount"] == 69

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
