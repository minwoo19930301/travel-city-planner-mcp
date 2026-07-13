from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

from .catalog import Catalog


class LiveDataProvider:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    async def collect(
        self,
        segments: list[dict[str, Any]],
        catalog: Catalog,
        enabled: bool = True,
        today: date | None = None,
    ) -> dict[str, Any]:
        if not enabled:
            return {
                "weather": {"status": "skipped", "segments": []},
                "exchange": {"status": "skipped", "rates": []},
            }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            weather = await self._weather(segments, catalog, client, today=today)
            exchange = await self._exchange(segments, catalog, client)
        return {"weather": weather, "exchange": exchange}

    async def _weather(
        self,
        segments: list[dict[str, Any]],
        catalog: Catalog,
        client: httpx.AsyncClient,
        today: date | None = None,
    ) -> dict[str, Any]:
        today = today or date.today()
        if not any(segment.get("start_date") for segment in segments):
            return {
                "status": "date_required",
                "message": "날짜 미정 — 현재 날씨를 여행 날씨처럼 표시하지 않았습니다.",
                "segments": [],
            }

        results = []
        for segment in segments:
            destination = catalog.get(segment["destination_id"])
            if not segment.get("start_date") or not segment.get("end_date"):
                results.append(
                    {
                        "destination_id": destination["id"],
                        "status": "date_required",
                        "message": "이 구간의 날짜가 정해지지 않았습니다.",
                    }
                )
                continue
            start = date.fromisoformat(segment["start_date"])
            end = date.fromisoformat(segment["end_date"])
            if start < today or end > today + timedelta(days=15):
                results.append(
                    {
                        "destination_id": destination["id"],
                        "status": "out_of_forecast_range",
                        "message": "여행 날짜가 예보 가능 범위 밖입니다. 현재 날씨로 대체하지 않았습니다.",
                    }
                )
                continue
            params = {
                "latitude": destination["weather"]["latitude"],
                "longitude": destination["weather"]["longitude"],
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": destination["timeZone"],
                "start_date": segment["start_date"],
                "end_date": segment["end_date"],
            }
            try:
                response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
                response.raise_for_status()
                payload = response.json().get("daily", {})
                days = [
                    {
                        "date": value,
                        "weather_code": payload.get("weather_code", [None] * 99)[index],
                        "max_c": payload.get("temperature_2m_max", [None] * 99)[index],
                        "min_c": payload.get("temperature_2m_min", [None] * 99)[index],
                    }
                    for index, value in enumerate(payload.get("time", []))
                ]
                results.append(
                    {"destination_id": destination["id"], "status": "live", "days": days}
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "destination_id": destination["id"],
                        "status": "unavailable",
                        "message": str(exc),
                    }
                )
        overall = "live" if any(item["status"] == "live" for item in results) else results[0]["status"]
        return {"status": overall, "segments": results}

    async def _exchange(
        self,
        segments: list[dict[str, Any]],
        catalog: Catalog,
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        currencies = []
        for segment in segments:
            currency = catalog.get(segment["destination_id"])["currency"]["code"]
            if currency not in currencies:
                currencies.append(currency)
        rates = []
        for currency in currencies:
            if currency == "KRW":
                rates.append({"currency": "KRW", "krw_per_unit": 1, "status": "fixed"})
                continue
            try:
                response = await client.get(f"https://open.er-api.com/v6/latest/{currency}")
                response.raise_for_status()
                krw = response.json().get("rates", {}).get("KRW")
                if not krw:
                    raise ValueError(f"KRW rate missing for {currency}")
                rates.append(
                    {
                        "currency": currency,
                        "krw_per_unit": round(float(krw), 4),
                        "status": "live",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                rates.append(
                    {"currency": currency, "status": "unavailable", "message": str(exc)}
                )
        status = "live" if any(item["status"] in {"live", "fixed"} for item in rates) else "unavailable"
        return {"status": status, "base": "KRW", "rates": rates}
