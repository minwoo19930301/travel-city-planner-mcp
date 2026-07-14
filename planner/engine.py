from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .catalog import Catalog, CatalogError, normalize_text
from .icons import normalize_icon
from .live_data import LiveDataProvider
from .tokens import TokenError, decode_content_token, encode_content_token


NIGHT_RANGE_RE = re.compile(r"(\d+)\s*박?\s*(?:[-~–—]|에서)\s*(\d+)\s*박")
SINGLE_NIGHT_RE = re.compile(r"(\d+)\s*박")
SINGLE_DAY_RE = re.compile(r"(\d+)\s*일")
ISO_DATE_RE = re.compile(r"(20\d{2})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})(?:일)?")

PREFERENCE_KEYWORDS = (
    "맛집",
    "쇼핑",
    "애니",
    "야경",
    "미술관",
    "박물관",
    "카페",
    "테마파크",
    "디즈니",
    "유니버설",
    "자연",
    "휴식",
    "온천",
    "시장",
    "사진",
    "가족",
    "아이",
    "혼자",
)


class PlannerError(ValueError):
    pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    match = ISO_DATE_RE.search(value.strip())
    if not match:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value.strip()):
            try:
                return date.fromisoformat(value.strip()).isoformat()
            except ValueError as exc:
                raise PlannerError(f"invalid start_date: {value}") from exc
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError as exc:
        raise PlannerError(f"invalid date in query: {match.group(0)}") from exc


def _duration_from_text(value: str) -> dict[str, Any] | None:
    range_match = NIGHT_RANGE_RE.search(value)
    if range_match:
        low, high = sorted((int(range_match.group(1)), int(range_match.group(2))))
        low = max(1, min(low, 30))
        high = max(low, min(high, 30))
        return {
            "requested_nights": [low, high],
            "selected_nights": high,
            "selected_days": high + 1,
            "selection_reason": f"{low}-{high}박 중 여유 있는 {high}박을 기본안으로 선택했습니다.",
        }
    night_match = SINGLE_NIGHT_RE.search(value)
    if night_match:
        nights = max(1, min(int(night_match.group(1)), 30))
        return {
            "requested_nights": [nights, nights],
            "selected_nights": nights,
            "selected_days": nights + 1,
            "selection_reason": f"{nights}박 {nights + 1}일 일정입니다.",
        }
    day_match = SINGLE_DAY_RE.search(value)
    if day_match:
        days = max(2, min(int(day_match.group(1)), 31))
        return {
            "requested_nights": [days - 1, days - 1],
            "selected_nights": days - 1,
            "selected_days": days,
            "selection_reason": f"{days}일을 {days - 1}박으로 해석했습니다.",
        }
    return None


def _map_search_url(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": 1, "query": query}
    )


def _route_url(activities: list[dict[str, Any]]) -> str:
    queries = [
        activity.get("map_query") or activity.get("location") or activity.get("title")
        for activity in activities
        if activity.get("map_query") or activity.get("location") or activity.get("title")
    ]
    if not queries:
        return ""
    if len(queries) == 1:
        return _map_search_url(queries[0])
    params: dict[str, Any] = {
        "api": 1,
        "origin": queries[0],
        "destination": queries[-1],
        "travelmode": "transit",
    }
    if len(queries) > 2:
        params["waypoints"] = "|".join(queries[1:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(params)


TRAVEL_MODES = ("transit", "walking", "driving")


def _directions_url(origin: str, destination: str, travelmode: str) -> str:
    if travelmode not in TRAVEL_MODES:
        raise PlannerError(f"unsupported travel mode: {travelmode}")
    return "https://www.google.com/maps/dir/?" + urlencode(
        {
            "api": 1,
            "origin": origin,
            "destination": destination,
            "travelmode": travelmode,
        }
    )


def _route_options(origin: str, destination: str) -> dict[str, str]:
    return {
        mode: _directions_url(origin, destination, mode)
        for mode in TRAVEL_MODES
    }


def _validate_web_url(value: Any, field_name: str) -> None:
    """Reject executable or credential-bearing links recovered from untrusted tokens."""
    if value is None or value == "":
        return
    if not isinstance(value, str):
        raise TokenError(f"{field_name} must be a URL string")
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TokenError(f"{field_name} contains control characters")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise TokenError(f"{field_name} is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TokenError(f"{field_name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise TokenError(f"{field_name} must not contain credentials")


def _validate_google_maps_url(value: Any, field_name: str) -> None:
    _validate_web_url(value, field_name)
    if value is None or value == "":
        return
    parsed = urlsplit(value.strip())
    try:
        has_non_default_port = parsed.port is not None
    except ValueError as exc:
        raise TokenError(f"{field_name} contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.google.com"
        or has_non_default_port
    ):
        raise TokenError(f"{field_name} must use the official Google Maps host")
    if not parsed.path.startswith("/maps/"):
        raise TokenError(f"{field_name} must be a Google Maps URL")


class PlannerService:
    def __init__(
        self,
        catalog: Catalog | None = None,
        public_base_url: str = "http://127.0.0.1:8000",
        legacy_viewer_url: str = "https://minwoo19930301.github.io/tour-city-planner/",
        live_data: LiveDataProvider | None = None,
    ) -> None:
        self.catalog = catalog or Catalog()
        self.public_base_url = public_base_url.rstrip("/")
        self.legacy_viewer_url = legacy_viewer_url.rstrip("/") + "/"
        self.live_data = live_data or LiveDataProvider()
        self._head_revisions: dict[str, int] = {}
        self.allowed_icons = frozenset(self.catalog.allowed_icons)

    def parse_query(self, query: str, start_date: str = "") -> dict[str, Any]:
        if not query or not query.strip():
            raise PlannerError("query is required")
        mentions = self.catalog.find_mentions(query)
        if not mentions:
            raise PlannerError(
                "여행 도시를 찾지 못했습니다. list_destinations로 도시 이름을 확인해 주세요."
            )
        parsed_start = _parse_date(start_date) or _parse_date(query)
        pace = "relaxed" if re.search(r"느긋|여유|천천히|휴식", query) else "balanced"
        if re.search(r"빡빡|알차|많이|촘촘", query):
            pace = "packed"
        preferences = [keyword for keyword in PREFERENCE_KEYWORDS if keyword in query]

        segments = []
        global_duration = _duration_from_text(query)
        for index, mention in enumerate(mentions):
            span_end = mentions[index + 1]["start"] if index + 1 < len(mentions) else len(query)
            local_text = query[mention["start"] : span_end]
            duration = _duration_from_text(local_text) or global_duration
            destination = self.catalog.get(mention["destination_id"])
            if duration is None:
                default_days = len(destination["itineraryTemplate"])
                duration = {
                    "requested_nights": [default_days - 1, default_days - 1],
                    "selected_nights": default_days - 1,
                    "selected_days": default_days,
                    "selection_reason": f"원본 {default_days}일 템플릿 길이를 사용했습니다.",
                }
            segments.append(
                {
                    "destination_id": mention["destination_id"],
                    "nights": duration["selected_nights"],
                    "duration": duration,
                }
            )
        return {
            "query": query.strip(),
            "segments": segments,
            "start_date": parsed_start,
            "pace": pace,
            "preferences": preferences,
        }

    async def plan_from_query(
        self,
        query: str,
        start_date: str = "",
        include_live_data: bool = True,
    ) -> dict[str, Any]:
        parsed = self.parse_query(query, start_date=start_date)
        return await self.create_plan(
            segments=parsed["segments"],
            start_date=parsed["start_date"],
            preferences=parsed["preferences"],
            pace=parsed["pace"],
            source_query=parsed["query"],
            include_live_data=include_live_data,
        )

    async def create_plan(
        self,
        segments: list[dict[str, Any]],
        start_date: str | None = None,
        preferences: list[str] | str | None = None,
        pace: str = "balanced",
        source_query: str = "",
        include_live_data: bool = True,
    ) -> dict[str, Any]:
        if not segments:
            raise PlannerError("at least one segment is required")
        if isinstance(preferences, str):
            preference_list = [item.strip() for item in re.split(r"[,/|]", preferences) if item.strip()]
        else:
            preference_list = [str(item).strip() for item in (preferences or []) if str(item).strip()]
        pace = pace if pace in {"relaxed", "balanced", "packed"} else "balanced"
        start_value = _parse_date(start_date)
        start_day = date.fromisoformat(start_value) if start_value else None

        normalized_segments: list[dict[str, Any]] = []
        day_map: dict[int, dict[str, Any]] = {}
        global_offset = 0
        selected_range: tuple[int, int] | None = None

        for segment_index, raw_segment in enumerate(segments):
            destination = self.catalog.get(str(raw_segment.get("destination_id", "")))
            duration = raw_segment.get("duration") or {}
            nights = int(raw_segment.get("nights") or duration.get("selected_nights") or max(1, len(destination["itineraryTemplate"]) - 1))
            nights = max(1, min(nights, 30))
            requested = duration.get("requested_nights")
            if requested and len(requested) == 2 and requested[0] != requested[1]:
                selected_range = (int(requested[0]), int(requested[1]))
            segment_start = start_day + timedelta(days=global_offset) if start_day else None
            segment_end = segment_start + timedelta(days=nights) if segment_start else None
            segment = {
                "id": f"segment-{segment_index + 1}",
                "destination_id": destination["id"],
                "city": destination["city"],
                "city_ko": destination["cityKo"],
                "country_ko": destination["countryKo"],
                "nights": nights,
                "days": nights + 1,
                "day_start": global_offset + 1,
                "day_end": global_offset + nights + 1,
                "start_date": segment_start.isoformat() if segment_start else None,
                "end_date": segment_end.isoformat() if segment_end else None,
                "currency": destination["currency"]["code"],
                "time_zone": destination["timeZone"],
            }
            normalized_segments.append(segment)
            city_days = self._build_city_days(destination, nights + 1, preference_list, pace)
            for local_index, city_day in enumerate(city_days):
                global_index = global_offset + local_index
                plan_day = day_map.setdefault(
                    global_index,
                    {
                        "day": global_index + 1,
                        "date": (
                            (start_day + timedelta(days=global_index)).isoformat()
                            if start_day
                            else None
                        ),
                        "title": city_day["title"],
                        "destination_ids": [],
                        "activities": [],
                    },
                )
                if plan_day["destination_ids"] and destination["id"] not in plan_day["destination_ids"]:
                    previous = self.catalog.get(plan_day["destination_ids"][-1])
                    transfer_title = f"{previous['cityKo']} → {destination['cityKo']} 이동"
                    origin_activities = plan_day["activities"]
                    morning_options = [
                        activity for activity in origin_activities
                        if activity["time"] < "12:00"
                    ]
                    origin_morning = copy.deepcopy(
                        (morning_options or origin_activities)[-1]
                    )
                    if origin_morning["time"] >= "12:00":
                        origin_morning["time"] = "10:00"

                    destination_activities = city_day["activities"]
                    evening_options = [
                        activity for activity in destination_activities
                        if activity["time"] >= "17:00"
                    ]
                    destination_evening = copy.deepcopy(
                        (evening_options or destination_activities)[-1]
                    )
                    if destination_evening["time"] < "17:00":
                        destination_evening["time"] = "18:00"

                    transfer = self._activity(
                        destination=destination,
                        time="13:00",
                        title=transfer_title,
                        location=f"{previous['city']} to {destination['city']}",
                        icon="train-front",
                        memo="다도시 구간 이동일",
                        source="generated-transfer",
                    )
                    plan_day["destination_ids"].append(destination["id"])
                    plan_day["activities"] = [
                        origin_morning,
                        transfer,
                        destination_evening,
                    ]
                    plan_day["title"] = transfer_title
                else:
                    if destination["id"] not in plan_day["destination_ids"]:
                        plan_day["destination_ids"].append(destination["id"])
                    plan_day["activities"].extend(city_day["activities"])
            global_offset += nights

        days = [day_map[index] for index in sorted(day_map)]
        self._rebuild_routes({"days": days})

        total_nights = sum(segment["nights"] for segment in normalized_segments)
        total_days = total_nights + 1
        city_names = " · ".join(segment["city_ko"] for segment in normalized_segments)
        plan_id = uuid.uuid4().hex[:12]
        plan: dict[str, Any] = {
            "schema_version": 1,
            "plan_id": plan_id,
            "revision": 1,
            "created_at": _iso_now(),
            "updated_at": _iso_now(),
            "title": f"{city_names} {total_nights}박 {total_days}일",
            "source_query": source_query,
            "duration": {
                "selected_nights": total_nights,
                "selected_days": total_days,
                "requested_range": list(selected_range) if selected_range else [total_nights, total_nights],
            },
            "pace": pace,
            "preferences": preference_list,
            "segments": normalized_segments,
            "days": days,
            "live_data": {},
            "shorter_variant": self._shorter_variant(days, selected_range),
            "catalog": {
                "digest": self.catalog.digest,
                "source": self.catalog.source,
            },
        }
        plan["live_data"] = await self.live_data.collect(
            normalized_segments,
            self.catalog,
            enabled=include_live_data,
        )
        self._head_revisions[plan_id] = 1
        return plan

    async def city_guide(
        self,
        destination_id: str,
        phrase_query: str = "",
    ) -> dict[str, Any]:
        """Return canonical city media/language data plus fresh clocks and FX."""
        destination = self.catalog.get(destination_id)
        if len(phrase_query) > 200:
            raise PlannerError("phrase_query must be at most 200 characters")
        query = normalize_text(phrase_query)
        phrases = []
        for phrase in destination.get("phrases", []):
            haystack = normalize_text(
                " ".join(
                    str(phrase.get(key, ""))
                    for key in ("text", "pron", "meaning")
                )
            )
            if query and query not in haystack:
                continue
            phrases.append(
                {
                    "text": str(phrase.get("text", "")),
                    "pron": str(phrase.get("pron", "")),
                    "meaning": str(phrase.get("meaning", "")),
                }
            )

        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        currency = copy.deepcopy(destination["currency"])
        exchange = await self.live_data.exchange_for_currency(currency["code"])
        return {
            "ok": True,
            "destination": {
                "id": destination["id"],
                "city": destination["city"],
                "city_ko": destination["cityKo"],
                "country_ko": destination["countryKo"],
                "hero_image": destination["heroImage"],
                "hero_url": f"{self.public_base_url}/viewer/{destination['heroImage']}",
                "time_zone": destination["timeZone"],
                "currency": currency,
            },
            "clocks": {
                "fetched_at": now_utc.isoformat(),
                "seoul": self._clock_value("Asia/Seoul", now_utc),
                "local": self._clock_value(destination["timeZone"], now_utc),
            },
            "language": destination.get("phraseLabel", ""),
            "phrases": phrases,
            "map_url": _map_search_url(
                f"{destination['city']}, {destination['country']}"
            ),
            "exchange": exchange,
        }

    def route_options(
        self,
        from_place: str,
        to_place: str,
        suggested_mode: str = "transit",
    ) -> dict[str, Any]:
        """Build official Google Maps route URLs without claiming live ETAs."""
        origin = str(from_place or "").strip()
        destination = str(to_place or "").strip()
        if not origin or not destination:
            raise PlannerError("from_place and to_place are required")
        if len(origin) > 500 or len(destination) > 500:
            raise PlannerError("route place text must be at most 500 characters")
        mode = str(suggested_mode or "transit").strip().lower()
        if mode not in TRAVEL_MODES:
            raise PlannerError("suggested_mode must be transit, walking, or driving")
        return {
            "ok": True,
            "from": origin,
            "to": destination,
            "suggested_mode": mode,
            "route_urls": _route_options(origin, destination),
            "note": "실시간 소요시간·운행 여부는 Google Maps를 열어 확인하세요.",
        }

    @staticmethod
    def _clock_value(zone_name: str, now_utc: datetime) -> dict[str, str]:
        local = now_utc.astimezone(ZoneInfo(zone_name))
        return {
            "time_zone": zone_name,
            "iso": local.isoformat(),
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M:%S"),
        }

    def _build_city_days(
        self,
        destination: dict[str, Any],
        day_count: int,
        preferences: list[str],
        pace: str,
    ) -> list[dict[str, Any]]:
        templates = destination["itineraryTemplate"]
        if day_count <= len(templates):
            selected = templates[:day_count] if day_count == 1 else templates[: day_count - 1] + [templates[-1]]
        else:
            flex_count = day_count - len(templates)
            flex_days = [
                {
                    "title": f"여유일 {index + 1} · 취향 보강",
                    "activities": [
                        {
                            "time": "11:00",
                            "title": f"{destination['cityKo']} 동네 자유 탐방",
                            "location": f"{destination['city']} local neighborhood",
                            "type": "map",
                            "memo": "원본 일정 사이에 넣은 컨디션 조절용 자유 일정",
                            "source": "generated-flex",
                        },
                        {
                            "time": "16:00",
                            "title": "예약·휴식 버퍼",
                            "location": f"{destination['city']} cafe",
                            "type": "coffee",
                            "memo": "밀린 일정, 카페, 쇼핑 중 현지에서 선택",
                            "source": "generated-flex",
                        },
                    ],
                }
                for index in range(flex_count)
            ]
            selected = templates[:-1] + flex_days + [templates[-1]]

        max_activities = {"relaxed": 2, "balanced": 3, "packed": 4}[pace]
        result = []
        for template_index, template in enumerate(selected):
            raw_activities = template["activities"][:max_activities]
            activities = []
            for raw in raw_activities:
                source = raw.get("source") or "canonical-template"
                memo = raw.get("memo", "")
                matched = self._preference_matches(raw, preferences)
                if matched:
                    suffix = f"요청 취향 매칭: {', '.join(matched)}"
                    memo = f"{memo} · {suffix}" if memo else suffix
                activities.append(
                    self._activity(
                        destination=destination,
                        time=raw.get("time", "10:00"),
                        title=raw.get("title") or raw.get("location") or "일정",
                        location=raw.get("location") or raw.get("title") or destination["city"],
                        icon=raw.get("type"),
                        memo=memo,
                        source=source,
                        template_day=template_index,
                    )
                )
            result.append({"title": template["title"], "activities": activities})
        return result

    def _activity(
        self,
        destination: dict[str, Any],
        time: str,
        title: str,
        location: str,
        icon: str | None,
        memo: str,
        source: str,
        template_day: int | None = None,
    ) -> dict[str, Any]:
        query = f"{location}, {destination['city']}"
        return {
            "id": f"act-{uuid.uuid4().hex[:10]}",
            "destination_id": destination["id"],
            "time": time,
            "title": title,
            "location": location,
            "map_query": query,
            "map_url": _map_search_url(query),
            "icon": normalize_icon(icon, self.allowed_icons),
            "memo": memo,
            "source": source,
            "template_day": template_day,
        }

    def _preference_matches(
        self,
        activity: dict[str, Any],
        preferences: list[str],
    ) -> list[str]:
        haystack = " ".join(
            str(activity.get(key, "")) for key in ("title", "location", "memo", "type")
        ).casefold()
        synonyms = {
            "맛집": ("food", "utensils", "시장", "라멘", "카페", "restaurant"),
            "애니": ("akihabara", "아키하바라", "character", "캐릭터"),
            "쇼핑": ("shopping", "market", "시장", "몰", "긴자"),
            "야경": ("night", "야경", "moon", "전망", "sky"),
            "미술관": ("museum", "미술관", "palette", "art"),
            "박물관": ("museum", "박물관", "building-2"),
            "자연": ("park", "공원", "trees", "beach", "sun"),
        }
        matched = []
        for preference in preferences:
            needles = synonyms.get(preference, (preference,))
            if any(needle.casefold() in haystack for needle in needles):
                matched.append(preference)
        return matched

    def _shorter_variant(
        self,
        days: list[dict[str, Any]],
        selected_range: tuple[int, int] | None,
    ) -> dict[str, Any] | None:
        if not selected_range or selected_range[0] == selected_range[1]:
            return None
        low, high = selected_range
        removable = next(
            (day for day in days if "여유일" in day["title"]),
            days[-2] if len(days) > 2 else days[-1],
        )
        return {
            "nights": low,
            "days": low + 1,
            "hint": (
                f"{low}박 {low + 1}일로 줄일 때는 Day {removable['day']} "
                f"‘{removable['title']}’을 빼고 마지막 이동일을 앞당기세요."
            ),
        }

    def summarize(self, plan: dict[str, Any], include_itinerary: bool = True) -> dict[str, Any]:
        token = encode_content_token(plan)
        result: dict[str, Any] = {
            "ok": True,
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "title": plan["title"],
            "summary": self.summary_text(plan),
            "duration": plan["duration"],
            "segments": plan["segments"],
            "pace": plan["pace"],
            "preferences": plan["preferences"],
            "weather": plan["live_data"].get("weather"),
            "exchange": plan["live_data"].get("exchange"),
            "shorter_variant": plan.get("shorter_variant"),
            "content_token": token,
            "viewer_url": f"{self.public_base_url}/viewer#plan={token}",
            "catalog_digest": plan["catalog"]["digest"],
            "note": "기본 응답에는 HTML을 넣지 않습니다. export_plan(format='html')로 분리해 받으세요.",
        }
        if include_itinerary:
            result["itinerary"] = [
                {
                    "day": day["day"],
                    "date": day["date"],
                    "title": day["title"],
                    "destinations": day["destination_ids"],
                    "route_map_url": day["route_map_url"],
                    "legs": day["legs"],
                    "activities": [
                        {
                            "activity_id": activity["id"],
                            "destination_id": activity["destination_id"],
                            "time": activity["time"],
                            "title": activity["title"],
                            "location": activity["location"],
                            "map_query": activity["map_query"],
                            "map_url": activity["map_url"],
                            "memo": activity.get("memo", ""),
                        }
                        for activity in day["activities"]
                    ],
                }
                for day in plan["days"]
            ]
        return result

    def summary_text(self, plan: dict[str, Any]) -> str:
        lines = [plan["title"]]
        for day in plan["days"]:
            activity_text = " → ".join(item["title"] for item in day["activities"])
            date_label = f" · {day['date']}" if day.get("date") else ""
            lines.append(f"Day {day['day']}{date_label} | {day['title']} | {activity_text}")
        if plan.get("shorter_variant"):
            lines.append(f"단축안: {plan['shorter_variant']['hint']}")
        return "\n".join(lines)

    def decode(self, token: str) -> dict[str, Any]:
        plan = decode_content_token(token)
        self._migrate_legacy_v1(plan)
        self._validate_plan(plan)
        return plan

    def _migrate_legacy_v1(self, plan: dict[str, Any]) -> None:
        """Add deterministic navigation legs to already-issued v1 tokens.

        The schema version remains 1 because this is an additive field. Present
        legs are never rewritten here, so malformed/tampered new tokens still fail
        validation instead of being silently repaired.
        """
        days = plan.get("days")
        if not isinstance(days, list):
            return
        for day in days:
            if not isinstance(day, dict):
                continue
            activities = day.get("activities")
            if (
                "legs" not in day
                and isinstance(activities, list)
                and all(
                    isinstance(activity, dict)
                    and all(
                        isinstance(activity.get(field), str)
                        for field in ("id", "title", "location")
                    )
                    for activity in activities
                )
            ):
                day["legs"] = [
                    self._build_leg(origin, destination)
                    for origin, destination in zip(
                        activities, activities[1:]
                    )
                ]

    def mutate(
        self,
        token: str,
        expected_revision: int,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        plan = self.decode(token)
        token_revision = plan["revision"]
        known_head = self._head_revisions.get(plan["plan_id"], token_revision)
        if expected_revision != token_revision or expected_revision != known_head:
            return {
                "ok": False,
                "code": "REVISION_CONFLICT",
                "message": "기대 revision과 현재 plan head가 다릅니다. 최신 content_token으로 다시 시도하세요.",
                "expected_revision": expected_revision,
                "token_revision": token_revision,
                "head_revision": known_head,
            }
        next_plan = copy.deepcopy(plan)
        for operation in operations:
            self._apply_operation(next_plan, operation)
        next_plan["revision"] = token_revision + 1
        next_plan["updated_at"] = _iso_now()
        self._rebuild_routes(next_plan)
        self._validate_plan(next_plan)
        self._head_revisions[next_plan["plan_id"]] = next_plan["revision"]
        return self.summarize(next_plan)

    def _apply_operation(self, plan: dict[str, Any], operation: dict[str, Any]) -> None:
        kind = operation.get("op")
        if kind == "rename_day":
            day = self._get_day(plan, operation.get("day"))
            day["title"] = str(operation.get("title") or day["title"]).strip()
            return
        if kind == "set_preferences":
            values = operation.get("preferences") or []
            plan["preferences"] = [str(item).strip() for item in values if str(item).strip()]
            return
        if kind == "add_activity":
            day = self._get_day(plan, operation.get("day"))
            raw = operation.get("activity") or {}
            destination_id = raw.get("destination_id") or day["destination_ids"][0]
            destination = self.catalog.get(destination_id)
            title = str(raw.get("title") or raw.get("location") or "새 일정").strip()
            location = str(raw.get("location") or title).strip()
            activity = self._activity(
                destination=destination,
                time=str(raw.get("time") or "10:00"),
                title=title,
                location=location,
                icon=raw.get("icon"),
                memo=str(raw.get("memo") or ""),
                source="user",
            )
            if raw.get("map_query"):
                activity["map_query"] = str(raw["map_query"])
                activity["map_url"] = _map_search_url(activity["map_query"])
            day["activities"].append(activity)
            if destination["id"] not in day["destination_ids"]:
                day["destination_ids"].append(destination["id"])
            return
        if kind in {"update_activity", "remove_activity", "move_activity"}:
            source_day = self._get_day(plan, operation.get("day") or operation.get("from_day"))
            index = self._activity_index(source_day, operation)
            if kind == "remove_activity":
                source_day["activities"].pop(index)
                return
            activity = source_day["activities"][index]
            if kind == "move_activity":
                target_day = self._get_day(plan, operation.get("to_day"))
                source_day["activities"].pop(index)
                if operation.get("time"):
                    activity["time"] = str(operation["time"])
                target_day["activities"].append(activity)
                if activity["destination_id"] not in target_day["destination_ids"]:
                    target_day["destination_ids"].append(activity["destination_id"])
                return
            changes = operation.get("changes") or {}
            for key in ("time", "title", "location", "memo"):
                if key in changes:
                    activity[key] = str(changes[key])
            if "icon" in changes:
                activity["icon"] = normalize_icon(changes["icon"], self.allowed_icons)
            if "destination_id" in changes:
                destination = self.catalog.get(str(changes["destination_id"]))
                activity["destination_id"] = destination["id"]
            if "map_query" in changes:
                activity["map_query"] = str(changes["map_query"])
            elif "location" in changes or "title" in changes:
                destination = self.catalog.get(activity["destination_id"])
                activity["map_query"] = f"{activity['location']}, {destination['city']}"
            activity["map_url"] = _map_search_url(activity["map_query"])
            return
        raise PlannerError(f"unsupported mutation op: {kind}")

    def _get_day(self, plan: dict[str, Any], day_number: Any) -> dict[str, Any]:
        try:
            number = int(day_number)
        except (TypeError, ValueError) as exc:
            raise PlannerError("operation requires an integer day") from exc
        for day in plan["days"]:
            if day["day"] == number:
                return day
        raise PlannerError(f"day not found: {number}")

    def _activity_index(self, day: dict[str, Any], operation: dict[str, Any]) -> int:
        activity_id = operation.get("activity_id")
        if activity_id:
            for index, activity in enumerate(day["activities"]):
                if activity["id"] == activity_id:
                    return index
            raise PlannerError(f"activity_id not found: {activity_id}")
        try:
            index = int(operation.get("activity_index"))
        except (TypeError, ValueError) as exc:
            raise PlannerError("operation requires activity_id or activity_index") from exc
        if index < 0 or index >= len(day["activities"]):
            raise PlannerError("activity_index out of range")
        return index

    def _rebuild_routes(self, plan: dict[str, Any]) -> None:
        for day in plan["days"]:
            day["activities"].sort(key=lambda item: item["time"])
            day["route_summary"] = " → ".join(item["title"] for item in day["activities"])
            day["route_map_url"] = _route_url(day["activities"])
            day["legs"] = [
                self._build_leg(origin, destination)
                for origin, destination in zip(
                    day["activities"], day["activities"][1:]
                )
            ]

    def _build_leg(
        self,
        origin: dict[str, Any],
        destination: dict[str, Any],
    ) -> dict[str, Any]:
        origin_query = str(
            origin.get("map_query") or origin.get("location") or origin.get("title")
        )
        destination_query = str(
            destination.get("map_query")
            or destination.get("location")
            or destination.get("title")
        )
        # Catalog activities do not carry reliable coordinates/distances. Transit is
        # the conservative default; clients still receive all three route choices.
        suggested_mode = "transit"
        return {
            "from": {
                "activity_id": origin["id"],
                "title": origin["title"],
                "location": origin["location"],
                "map_query": origin_query,
            },
            "to": {
                "activity_id": destination["id"],
                "title": destination["title"],
                "location": destination["location"],
                "map_query": destination_query,
            },
            "suggested_mode": suggested_mode,
            "route_urls": _route_options(origin_query, destination_query),
        }

    def _validate_plan(self, plan: dict[str, Any]) -> None:
        if plan.get("schema_version") != 1:
            raise TokenError("unsupported plan schema")
        if not isinstance(plan.get("segments"), list) or not plan["segments"]:
            raise TokenError("plan contains no segments")
        if not isinstance(plan.get("days"), list) or not plan["days"]:
            raise TokenError("plan contains no days")
        if not isinstance(plan.get("catalog"), dict) or not isinstance(plan["catalog"].get("digest"), str):
            raise TokenError("plan catalog metadata is missing")
        if not isinstance(plan.get("live_data"), dict):
            raise TokenError("plan live-data metadata is missing")
        if not isinstance(plan.get("title"), str) or not plan["title"].strip():
            raise TokenError("plan title is missing")
        if not isinstance(plan.get("duration"), dict):
            raise TokenError("plan duration is missing")
        if not isinstance(plan.get("pace"), str) or not isinstance(plan.get("preferences"), list):
            raise TokenError("plan preferences are missing")
        for segment in plan["segments"]:
            if not isinstance(segment, dict) or not isinstance(segment.get("destination_id"), str):
                raise TokenError("segment destination is missing")
            required_segment_fields = (
                "id", "city", "city_ko", "country_ko", "currency", "time_zone",
            )
            numeric_segment_fields = ("nights", "days", "day_start", "day_end")
            if any(not isinstance(segment.get(key), str) or not segment[key].strip() for key in required_segment_fields):
                raise TokenError("segment is missing required fields")
            if any(not isinstance(segment.get(key), int) or isinstance(segment[key], bool) for key in numeric_segment_fields):
                raise TokenError("segment duration is invalid")
            if any(segment.get(key) is not None and not isinstance(segment[key], str) for key in ("start_date", "end_date")):
                raise TokenError("segment date is invalid")
            try:
                self.catalog.get(segment["destination_id"])
            except CatalogError as exc:
                raise TokenError("segment destination is unknown") from exc
        for day in plan["days"]:
            if not isinstance(day, dict):
                raise TokenError("plan day must be an object")
            if not isinstance(day.get("day"), int) or isinstance(day["day"], bool):
                raise TokenError("plan day number is invalid")
            if not isinstance(day.get("title"), str):
                raise TokenError("plan day title is missing")
            if not isinstance(day.get("destination_ids"), list) or not all(
                isinstance(destination_id, str) for destination_id in day["destination_ids"]
            ):
                raise TokenError("plan day destinations are invalid")
            for destination_id in day["destination_ids"]:
                try:
                    self.catalog.get(destination_id)
                except CatalogError as exc:
                    raise TokenError("plan day destination is unknown") from exc
            if not isinstance(day.get("activities"), list):
                raise TokenError("plan day activities are invalid")
            _validate_google_maps_url(day.get("route_map_url"), "route_map_url")
            activities = day["activities"]
            for activity in activities:
                if not isinstance(activity, dict):
                    raise TokenError("activity must be an object")
                required_text = ("id", "destination_id", "time", "title", "location", "map_query")
                if any(not isinstance(activity.get(key), str) or not activity[key].strip() for key in required_text):
                    raise TokenError("activity is missing required fields")
                if activity["destination_id"] not in day["destination_ids"]:
                    raise TokenError("activity destination does not match its day")
                try:
                    self.catalog.get(activity["destination_id"])
                except CatalogError as exc:
                    raise TokenError("activity destination is unknown") from exc
                if activity.get("icon") not in self.allowed_icons:
                    raise TokenError(f"invalid activity icon: {activity.get('icon')}")
                _validate_google_maps_url(activity.get("map_url"), "map_url")
                if activity.get("map_url") != _map_search_url(str(activity.get("map_query", ""))):
                    raise TokenError("map_url does not match activity map_query")
            if day.get("route_map_url") != _route_url(activities):
                raise TokenError("route_map_url does not match day activities")
            expected_leg_count = max(0, len(activities) - 1)
            legs = day.get("legs")
            if not isinstance(legs, list) or len(legs) != expected_leg_count:
                raise TokenError("day legs do not match adjacent activities")
            for index, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    raise TokenError("leg must be an object")
                if leg.get("suggested_mode") not in TRAVEL_MODES:
                    raise TokenError("invalid leg suggested_mode")
                if not isinstance(leg.get("from"), dict) or not isinstance(leg.get("to"), dict):
                    raise TokenError("leg endpoints are missing")
                origin = activities[index]
                destination = activities[index + 1]
                if (
                    leg["from"].get("activity_id") != origin.get("id")
                    or leg["to"].get("activity_id") != destination.get("id")
                    or leg["from"].get("map_query") != origin.get("map_query")
                    or leg["to"].get("map_query") != destination.get("map_query")
                ):
                    raise TokenError("leg endpoints do not match adjacent activities")
                route_urls = leg.get("route_urls")
                if not isinstance(route_urls, dict) or set(route_urls) != set(TRAVEL_MODES):
                    raise TokenError("leg route_urls must include transit, walking, and driving")
                for mode in TRAVEL_MODES:
                    _validate_google_maps_url(
                        route_urls.get(mode),
                        f"leg.route_urls.{mode}",
                    )
                expected_urls = _route_options(
                    str(origin.get("map_query", "")),
                    str(destination.get("map_query", "")),
                )
                if route_urls != expected_urls:
                    raise TokenError("leg route URLs do not match adjacent activities")


def parse_json_array(value: str, field_name: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise PlannerError(f"{field_name} must be a JSON array of objects")
    return parsed


__all__ = [
    "CatalogError",
    "PlannerError",
    "PlannerService",
    "parse_json_array",
    "_map_search_url",
]
