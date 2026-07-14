from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
        fetched_at = _iso_now()
        rates = [await self._exchange_rate(currency, client) for currency in currencies]
        status = "live" if any(item["status"] in {"live", "fixed"} for item in rates) else "unavailable"
        updated_values = [item.get("updated_at") for item in rates if item.get("updated_at")]
        return {
            "status": status,
            "base": "KRW",
            "rates": rates,
            "fetched_at": fetched_at,
            "source": "open.er-api.com / KRW identity",
            "updated_at": updated_values[0] if len(set(updated_values)) == 1 else None,
        }

    async def exchange_for_currency(self, currency: str) -> dict[str, Any]:
        """Fetch one currency's KRW value with a bounded timeout and safe fallback."""
        code = str(currency or "").strip().upper()
        if not code or len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be a three-letter ISO code")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await self._exchange_rate(code, client)

    async def _exchange_rate(
        self,
        currency: str,
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        fetched_at = _iso_now()
        if currency == "KRW":
            return {
                "status": "fixed",
                "currency": "KRW",
                "krw_per_unit": 1.0,
                "fetched_at": fetched_at,
                "source": "KRW identity",
                "updated_at": fetched_at,
            }

        source = f"https://open.er-api.com/v6/latest/{currency}"
        try:
            response = await client.get(source)
            response.raise_for_status()
            payload = response.json()
            krw = payload.get("rates", {}).get("KRW")
            if not krw:
                raise ValueError(f"KRW rate missing for {currency}")
            return {
                "status": "live",
                "currency": currency,
                "krw_per_unit": round(float(krw), 4),
                "fetched_at": fetched_at,
                "source": source,
                "updated_at": payload.get("time_last_update_utc"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "currency": currency,
                "krw_per_unit": None,
                "fetched_at": fetched_at,
                "source": source,
                "updated_at": None,
                "message": str(exc),
            }


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
