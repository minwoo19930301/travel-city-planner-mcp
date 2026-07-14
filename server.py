from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from planner import PlannerService
from planner.catalog import CatalogError
from planner.engine import PlannerError, parse_json_array
from planner.live_data import LiveDataProvider
from planner.render import legacy_share_url, legacy_v3_payload, render_export_html
from planner.tokens import TokenError, encode_content_token


ROOT = Path(__file__).resolve().parent
VIEWER_ROOT = ROOT / "viewer"
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
LEGACY_VIEWER_URL = os.environ.get(
    "LEGACY_VIEWER_URL",
    "https://minwoo19930301.github.io/tour-city-planner/",
)

service = PlannerService(
    public_base_url=PUBLIC_BASE_URL,
    legacy_viewer_url=LEGACY_VIEWER_URL,
    live_data=LiveDataProvider(
        timeout=max(0.25, min(float(os.environ.get("LIVE_DATA_TIMEOUT", "5")), 30.0))
    ),
)


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


_public_url = urlparse(PUBLIC_BASE_URL)
_public_host = _public_url.netloc
_public_origin = f"{_public_url.scheme}://{_public_url.netloc}" if _public_url.scheme and _public_url.netloc else ""
# PlayMCP in KC serves on *.playmcp-endpoint.kakaocloud.io.
# Default DNS-rebinding allowlist is localhost-only, which makes /mcp return
# "Invalid Host header" (HTTP 421) and PlayMCP shows zero tools.
# Remote public MCP on Kakao Cloud must accept the endpoint Host header.
_extra_hosts = _csv_env("ALLOWED_HOSTS")
_extra_origins = _csv_env("ALLOWED_ORIGINS")
_kc_hosts = [
    "travel-city-planner-mcp.playmcp-endpoint.kakaocloud.io",
    "*.playmcp-endpoint.kakaocloud.io",
    "playmcp-endpoint.kakaocloud.io",
]
_kc_origins = [
    "https://playmcp.kakao.com",
    "https://*.playmcp.kakao.com",
    "https://*.playmcp-endpoint.kakaocloud.io",
]
_disable_rebinding = os.environ.get("DISABLE_DNS_REBINDING", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=not _disable_rebinding,
    allowed_hosts=[
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        *([_public_host] if _public_host else []),
        *_kc_hosts,
        *_extra_hosts,
    ],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *([_public_origin] if _public_origin else []),
        *_kc_origins,
        *_extra_origins,
    ],
)


class ToolOutput(BaseModel):
    """Strict base so MCP clients receive useful structured output schemas."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CurrencyOutput(ToolOutput):
    code: str
    symbol: str
    locale: str


class DestinationOutput(ToolOutput):
    id: str
    city: str
    city_ko: str
    country: str
    country_ko: str
    region: str
    currency: CurrencyOutput
    time_zone: str
    default_days: int
    summary: str
    hero_image: str
    hero_url: str


class DestinationListOutput(ToolOutput):
    ok: bool
    count: int
    catalog_total: int
    catalog_digest: str
    destinations: list[DestinationOutput]


class DurationOutput(ToolOutput):
    selected_nights: int
    selected_days: int
    requested_range: list[int]


class SegmentOutput(ToolOutput):
    id: str
    destination_id: str
    city: str
    city_ko: str
    country_ko: str
    nights: int
    days: int
    day_start: int
    day_end: int
    start_date: str | None
    end_date: str | None
    currency: str
    time_zone: str


class ActivityOutput(ToolOutput):
    activity_id: str
    destination_id: str
    time: str
    title: str
    location: str
    map_query: str
    map_url: str
    memo: str


class RouteUrlsOutput(ToolOutput):
    transit: str
    walking: str
    driving: str


class LegPlaceOutput(ToolOutput):
    activity_id: str
    title: str
    location: str
    map_query: str


class LegOutput(ToolOutput):
    from_: LegPlaceOutput = Field(alias="from", serialization_alias="from")
    to: LegPlaceOutput
    suggested_mode: str
    route_urls: RouteUrlsOutput


class ItineraryDayOutput(ToolOutput):
    day: int
    date: str | None
    title: str
    destinations: list[str]
    route_map_url: str
    legs: list[LegOutput]
    activities: list[ActivityOutput]


class ShorterVariantOutput(ToolOutput):
    nights: int
    days: int
    hint: str


class WeatherOutput(ToolOutput):
    status: str
    message: str | None = None
    segments: list[dict[str, Any]]


class ExchangeOutput(ToolOutput):
    status: str
    base: str | None = None
    rates: list[dict[str, Any]]
    fetched_at: str | None = None
    source: str | None = None
    updated_at: str | None = None


class ClockValueOutput(ToolOutput):
    time_zone: str
    iso: str
    date: str
    time: str


class ClocksOutput(ToolOutput):
    fetched_at: str
    seoul: ClockValueOutput
    local: ClockValueOutput


class PhraseOutput(ToolOutput):
    text: str
    pron: str
    meaning: str


class GuideDestinationOutput(ToolOutput):
    id: str
    city: str
    city_ko: str
    country_ko: str
    hero_image: str
    hero_url: str
    time_zone: str
    currency: CurrencyOutput


class GuideExchangeOutput(ToolOutput):
    status: str
    currency: str
    krw_per_unit: float | None = None
    fetched_at: str
    source: str
    updated_at: str | None = None
    message: str | None = None


class CityGuideOutput(ToolOutput):
    ok: bool
    error: str | None = None
    message: str | None = None
    destination: GuideDestinationOutput | None = None
    clocks: ClocksOutput | None = None
    language: str | None = None
    phrases: list[PhraseOutput] | None = None
    map_url: str | None = None
    exchange: GuideExchangeOutput | None = None


class RouteOptionsOutput(ToolOutput):
    ok: bool
    error: str | None = None
    message: str | None = None
    from_: str | None = Field(default=None, alias="from", serialization_alias="from")
    to: str | None = None
    suggested_mode: str | None = None
    route_urls: RouteUrlsOutput | None = None
    note: str | None = None


class PlanOutput(ToolOutput):
    ok: bool
    error: str | None = None
    code: str | None = None
    message: str | None = None
    plan_id: str | None = None
    revision: int | None = None
    title: str | None = None
    summary: str | None = None
    duration: DurationOutput | None = None
    segments: list[SegmentOutput] | None = None
    pace: str | None = None
    preferences: list[str] | None = None
    weather: WeatherOutput | None = None
    exchange: ExchangeOutput | None = None
    shorter_variant: ShorterVariantOutput | None = None
    content_token: str | None = None
    viewer_url: str | None = None
    catalog_digest: str | None = None
    note: str | None = None
    itinerary: list[ItineraryDayOutput] | None = None
    html: str | None = None
    expected_revision: int | None = None
    token_revision: int | None = None
    head_revision: int | None = None


class ExportOutput(ToolOutput):
    ok: bool
    error: str | None = None
    message: str | None = None
    format: str | None = None
    filename: str | None = None
    html: str | None = None
    plan: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    share_url: str | None = None
    warning: str | None = None
    content_token: str | None = None


class ValidationOutput(ToolOutput):
    ok: bool
    error: str | None = None
    message: str | None = None
    plan_id: str | None = None
    revision: int | None = None
    days: int | None = None
    segments: int | None = None
    catalog_digest: str | None = None
    token_survives_restart: bool | None = None


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
READ_ONLY_LIVE = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
CREATE_WITH_LIVE_DATA = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
MUTATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

mcp = FastMCP(
    name="travelCityPlanner",
    instructions=(
        "원본 Tour City Planner의 69개 도시 템플릿을 사용해 여행을 만듭니다. "
        "사용자가 '도쿄 4-5박'처럼 말하면 plan_trip을 즉시 호출하세요. "
        "4-5박은 5박6일 기본안과 4박 단축 힌트를 함께 반환합니다. "
        "기본 응답은 summary이며 HTML은 export_plan(format='html')로 분리합니다. "
        "수정할 때는 mutate_plan에 최신 content_token과 expected_revision을 함께 전달하세요."
    ),
    host=os.environ.get("HOST", "0.0.0.0"),
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=TRANSPORT_SECURITY,
)


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": exc.__class__.__name__, "message": str(exc)}


def _export_filename(plan_id: Any) -> str:
    """Derive a portable basename without changing the plan identity itself."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(plan_id or "")).strip(".-")
    return f"{safe or 'travel-plan'}.html"


def _with_optional_html(
    payload: dict[str, Any],
    plan: dict[str, Any],
    include_html: bool,
) -> dict[str, Any]:
    if include_html:
        payload["html"] = render_export_html(plan, service.catalog)
    return payload


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> RedirectResponse:
    return RedirectResponse("/viewer", status_code=307)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "travelCityPlanner",
            "mcp": "/mcp",
            "viewer": "/viewer",
            "destinations": len(service.catalog.destinations),
            "catalog_digest": service.catalog.digest,
        }
    )


@mcp.custom_route("/viewer", methods=["GET"])
async def viewer(_: Request) -> FileResponse:
    return FileResponse(VIEWER_ROOT / "index.html")


@mcp.custom_route("/viewer/{path:path}", methods=["GET"])
async def viewer_asset(request: Request) -> FileResponse | HTMLResponse:
    relative = request.path_params["path"] or "index.html"
    target = (VIEWER_ROOT / relative).resolve()
    try:
        target.relative_to(VIEWER_ROOT.resolve())
    except ValueError:
        return HTMLResponse("not found", status_code=404)
    if not target.is_file():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(target)


@mcp.custom_route("/data/destinations.json", methods=["GET"])
async def catalog_file(_: Request) -> FileResponse:
    return FileResponse(service.catalog.path, media_type="application/json")


@mcp.custom_route("/api/city-guide/{destination_id}", methods=["GET"])
async def city_guide_api(request: Request) -> JSONResponse:
    """Same-origin JSON bridge used by a reopened viewer token."""
    destination_id = request.path_params["destination_id"]
    phrase_query = request.query_params.get("phrase_query", "")
    try:
        payload = await service.city_guide(destination_id, phrase_query=phrase_query)
        return JSONResponse(payload)
    except CatalogError as exc:
        return JSONResponse(_error_payload(exc), status_code=404)
    except (PlannerError, TokenError, ValueError) as exc:
        return JSONResponse(_error_payload(exc), status_code=400)


@mcp.custom_route("/examples/demo-plan.json", methods=["GET"])
async def demo_file(_: Request) -> FileResponse | JSONResponse:
    target = ROOT / "examples" / "demo-plan.json"
    if not target.exists():
        return JSONResponse({"ok": False, "message": "demo not built"}, status_code=404)
    return FileResponse(target, media_type="application/json")


@mcp.custom_route("/view/{token:path}", methods=["GET"])
async def token_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/viewer#plan={request.path_params['token']}", status_code=307)


@mcp.tool(title="Search the canonical destination catalog", annotations=READ_ONLY)
def list_destinations(
    search: str = "", region: str = "", limit: int = 20
) -> DestinationListOutput:
    """69개 canonical 도시를 검색합니다.

    Args:
        search: 도시/국가의 한글 또는 영문 검색어.
        region: asia, north-america, europe, africa, resort, south-america 또는 한글 지역명.
        limit: 1~100.
    """
    rows = [
        {
            **row,
            "hero_url": f"{PUBLIC_BASE_URL}/viewer/{row['hero_image']}",
        }
        for row in service.catalog.search(query=search, region=region, limit=limit)
    ]
    return DestinationListOutput(
        ok=True,
        count=len(rows),
        catalog_total=len(service.catalog.destinations),
        catalog_digest=service.catalog.digest,
        destinations=rows,
    )


@mcp.tool(title="Get a city's live guide, clocks, phrases, map, and exchange rate", annotations=READ_ONLY_LIVE)
async def get_city_guide(
    destination_id: str,
    phrase_query: str = "",
) -> CityGuideOutput:
    """도시별 이미지·현지/서울 시각·기본 회화·지도·최신 원화 환율을 반환합니다.

    Args:
        destination_id: canonical id 또는 한글/영문 도시명. 예: tokyo, 도쿄, 빈, 괌.
        phrase_query: 선택 사항. 원문·발음·한국어 뜻에서 검색해 회화만 필터합니다.
    """
    try:
        return CityGuideOutput.model_validate(
            await service.city_guide(destination_id, phrase_query=phrase_query)
        )
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return CityGuideOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Build Google Maps route options between two places", annotations=READ_ONLY)
def get_route_options(
    from_place: str,
    to_place: str,
    suggested_mode: str = "transit",
) -> RouteOptionsOutput:
    """두 장소 사이의 대중교통·도보·자동차 Google Maps 공식 경로 URL을 만듭니다.

    Args:
        from_place: 출발 장소명 또는 주소.
        to_place: 도착 장소명 또는 주소.
        suggested_mode: transit, walking, driving 중 기본으로 보여줄 이동수단.
    """
    try:
        return RouteOptionsOutput.model_validate(
            service.route_options(from_place, to_place, suggested_mode)
        )
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return RouteOptionsOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Plan a trip from a natural-language request", annotations=CREATE_WITH_LIVE_DATA)
async def plan_trip(
    query: str,
    start_date: str = "",
    include_live_data: bool = True,
    include_html: bool = False,
) -> PlanOutput:
    """한국어 자연어 요청을 즉시 여행 플랜으로 만듭니다.

    예: '도쿄로 4-5박 정도 머무를 건데 맛집과 애니 위주로 추천 플래너 짜줘'.
    날짜가 없으면 현재 날씨를 여행 날씨로 표시하지 않습니다.

    Args:
        query: 도시, 기간, 취향을 포함한 자연어 요청.
        start_date: 선택 사항 YYYY-MM-DD. query 안의 날짜보다 우선합니다.
        include_live_data: 환율 및 날짜 범위 내 실제 여행일 예보를 조회합니다.
        include_html: 기본 false. true면 큰 HTML도 응답에 포함합니다.
    """
    try:
        plan = await service.plan_from_query(
            query=query,
            start_date=start_date,
            include_live_data=include_live_data,
        )
        payload = service.summarize(plan)
        return PlanOutput.model_validate(_with_optional_html(payload, plan, include_html))
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return PlanOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Create a structured single-city or multi-city trip", annotations=CREATE_WITH_LIVE_DATA)
async def create_plan(
    segments_json: str,
    start_date: str = "",
    preferences: str = "",
    pace: str = "balanced",
    include_live_data: bool = True,
    include_html: bool = False,
) -> PlanOutput:
    """구조화 입력으로 단일 또는 다도시 여행을 만듭니다.

    Args:
        segments_json: 예: [{"destination_id":"tokyo","nights":3},{"destination_id":"taipei","nights":2}]
        start_date: 전체 여정 첫날 YYYY-MM-DD. 비우면 날짜 미정 상태를 유지합니다.
        preferences: 쉼표로 구분한 취향.
        pace: relaxed, balanced, packed.
        include_live_data: 환율과 여행 날짜 예보 조회 여부.
        include_html: 큰 HTML을 함께 받을지 여부. 기본 false.
    """
    try:
        segments = parse_json_array(segments_json, "segments_json")
        normalized = [
            {
                **segment,
                "destination_id": segment.get("destination_id") or segment.get("city_id"),
            }
            for segment in segments
        ]
        plan = await service.create_plan(
            segments=normalized,
            start_date=start_date or None,
            preferences=preferences,
            pace=pace,
            include_live_data=include_live_data,
        )
        payload = service.summarize(plan)
        return PlanOutput.model_validate(_with_optional_html(payload, plan, include_html))
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return PlanOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Revise a trip with optimistic concurrency", annotations=MUTATE)
def mutate_plan(
    content_token: str,
    expected_revision: int,
    operations_json: str,
    include_html: bool = False,
) -> PlanOutput:
    """revision 충돌을 검사하며 일정 활동을 추가/수정/삭제/이동합니다.

    Args:
        content_token: 최신 plan_trip/create_plan/mutate_plan 응답의 압축 토큰.
        expected_revision: 호출자가 마지막으로 본 revision.
        operations_json: add_activity, update_activity, remove_activity, move_activity, rename_day, set_preferences 작업 배열.
        include_html: 큰 HTML을 함께 받을지 여부. 기본 false.
    """
    try:
        operations = parse_json_array(operations_json, "operations_json")
        result = service.mutate(content_token, expected_revision, operations)
        if include_html and result.get("ok"):
            result["html"] = render_export_html(service.decode(result["content_token"]), service.catalog)
        return PlanOutput.model_validate(result)
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return PlanOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Open a trip from its content token", annotations=READ_ONLY)
def get_plan(
    content_token: str,
    include_itinerary: bool = True,
    include_html: bool = False,
) -> PlanOutput:
    """압축 content token을 복원합니다. 서버 재시작 뒤에도 동작합니다."""
    try:
        plan = service.decode(content_token)
        service._head_revisions.setdefault(plan["plan_id"], plan["revision"])
        payload = service.summarize(plan, include_itinerary=include_itinerary)
        return PlanOutput.model_validate(_with_optional_html(payload, plan, include_html))
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return PlanOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Export a trip as HTML, JSON, legacy v3, or token", annotations=READ_ONLY)
def export_plan(content_token: str, format: str = "html") -> ExportOutput:
    """큰 결과를 기본 응답과 분리해 HTML, JSON, legacy_v3, token으로 내보냅니다."""
    try:
        plan = service.decode(content_token)
        export_format = format.strip().lower()
        if export_format == "html":
            return ExportOutput(
                ok=True,
                format="html",
                filename=_export_filename(plan["plan_id"]),
                html=render_export_html(plan, service.catalog),
            )
        if export_format == "json":
            return ExportOutput(ok=True, format="json", plan=plan)
        if export_format == "legacy_v3":
            undated = any(not segment.get("start_date") for segment in plan["segments"])
            return ExportOutput(
                ok=True,
                format="legacy_v3",
                payload=legacy_v3_payload(plan),
                share_url=legacy_share_url(plan, LEGACY_VIEWER_URL),
                warning=(
                    "날짜 미정 플랜을 원본 viewer가 열면 원본 기본 날짜를 제안합니다."
                    if undated
                    else None
                ),
            )
        if export_format in {"token", "content_token"}:
            return ExportOutput(
                ok=True,
                format="content_token",
                content_token=encode_content_token(plan),
            )
        raise PlannerError("format must be html, json, legacy_v3, or content_token")
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return ExportOutput.model_validate(_error_payload(exc))


@mcp.tool(title="Validate a trip content token", annotations=READ_ONLY)
def validate_plan(content_token: str) -> ValidationOutput:
    """content token, canonical catalog, icon, revision 구조를 검증합니다."""
    try:
        plan = service.decode(content_token)
        return ValidationOutput(
            ok=True,
            plan_id=plan["plan_id"],
            revision=plan["revision"],
            days=len(plan["days"]),
            segments=len(plan["segments"]),
            catalog_digest=plan["catalog"]["digest"],
            token_survives_restart=True,
        )
    except (PlannerError, CatalogError, TokenError, ValueError) as exc:
        return ValidationOutput.model_validate(_error_payload(exc))


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
