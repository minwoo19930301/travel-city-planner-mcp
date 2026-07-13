from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from .catalog import Catalog, CatalogError
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


def _validate_web_url(value: Any, field_name: str) -> None:
    """Reject executable or credential-bearing links recovered from untrusted tokens."""
    if value in {None, ""}:
        return
    text = str(value).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TokenError(f"{field_name} contains control characters")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TokenError(f"{field_name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise TokenError(f"{field_name} must not contain credentials")


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
                if destination["id"] not in plan_day["destination_ids"]:
                    plan_day["destination_ids"].append(destination["id"])
                if len(plan_day["destination_ids"]) > 1:
                    previous = self.catalog.get(plan_day["destination_ids"][-2])
                    transfer_title = f"{previous['cityKo']} → {destination['cityKo']} 이동"
                    if not any(item["title"] == transfer_title for item in plan_day["activities"]):
                        plan_day["activities"].append(
                            self._activity(
                                destination=destination,
                                time="13:00",
                                title=transfer_title,
                                location=f"{previous['city']} to {destination['city']}",
                                icon="train-front",
                                memo="다도시 구간 이동일",
                                source="generated-transfer",
                            )
                        )
                    plan_day["title"] = transfer_title
                plan_day["activities"].extend(city_day["activities"])
            global_offset += nights

        days = [day_map[index] for index in sorted(day_map)]
        for plan_day in days:
            plan_day["activities"].sort(key=lambda activity: activity["time"])
            plan_day["route_summary"] = " → ".join(
                activity["title"] for activity in plan_day["activities"]
            )
            plan_day["route_map_url"] = _route_url(plan_day["activities"])

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
                    "activities": [
                        {
                            "time": activity["time"],
                            "title": activity["title"],
                            "map_url": activity["map_url"],
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
        self._validate_plan(plan)
        return plan

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

    def _validate_plan(self, plan: dict[str, Any]) -> None:
        if plan.get("schema_version") != 1:
            raise TokenError("unsupported plan schema")
        if not isinstance(plan.get("days"), list) or not plan["days"]:
            raise TokenError("plan contains no days")
        for segment in plan.get("segments", []):
            self.catalog.get(segment["destination_id"])
        for day in plan["days"]:
            _validate_web_url(day.get("route_map_url"), "route_map_url")
            for activity in day.get("activities", []):
                if activity.get("icon") not in self.allowed_icons:
                    raise TokenError(f"invalid activity icon: {activity.get('icon')}")
                _validate_web_url(activity.get("map_url"), "map_url")


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
