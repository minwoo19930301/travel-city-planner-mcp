from __future__ import annotations

import asyncio

import httpx

from planner.catalog import Catalog
from planner.live_data import LiveDataProvider


def run(coro):
    return asyncio.run(coro)


def test_exchange_includes_fetch_and_source_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/JPY")
        return httpx.Response(
            200,
            json={
                "rates": {"KRW": 9.125},
                "time_last_update_utc": "Tue, 14 Jul 2026 00:02:31 +0000",
            },
        )

    async def exercise():
        provider = LiveDataProvider(timeout=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider._exchange(
                [{"destination_id": "tokyo"}], Catalog(), client
            )

    exchange = run(exercise())
    assert exchange["status"] == "live"
    assert exchange["fetched_at"]
    assert exchange["source"]
    rate = exchange["rates"][0]
    assert rate["currency"] == "JPY"
    assert rate["krw_per_unit"] == 9.125
    assert rate["fetched_at"]
    assert rate["source"].endswith("/JPY")
    assert rate["updated_at"] == "Tue, 14 Jul 2026 00:02:31 +0000"


def test_exchange_failure_returns_bounded_fallback_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async def exercise():
        provider = LiveDataProvider(timeout=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider._exchange_rate("EUR", client)

    rate = run(exercise())
    assert rate["status"] == "unavailable"
    assert rate["currency"] == "EUR"
    assert rate["krw_per_unit"] is None
    assert rate["fetched_at"]
    assert rate["source"].endswith("/EUR")
    assert rate["updated_at"] is None
