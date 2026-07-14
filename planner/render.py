from __future__ import annotations

import html
import json
import re
from base64 import b64encode
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .catalog import Catalog
from .tokens import encode_legacy_v3


TRAVEL_MODES = ("transit", "walking", "driving")
MODE_LABELS = {
    "transit": "대중교통",
    "walking": "도보",
    "driving": "차량",
}
ROOT = Path(__file__).resolve().parents[1]
VIEWER_ROOT = ROOT / "viewer"


def _local_asset_data_uri(relative_path: Any) -> str:
    """Embed only checked-in viewer files for the default portable export."""
    relative = str(relative_path or "").strip().lstrip("/")
    if not relative or _has_control_characters(relative) or any(part == ".." for part in relative.split("/")):
        return ""
    path = (VIEWER_ROOT / relative).resolve()
    try:
        path.relative_to(VIEWER_ROOT.resolve())
    except ValueError:
        return ""
    media_type = {".jpg": "image/jpeg", ".woff2": "font/woff2"}.get(path.suffix.lower())
    if not media_type or not path.is_file():
        return ""
    return f"data:{media_type};base64,{b64encode(path.read_bytes()).decode('ascii')}"


def _export_asset(asset_base_url: str, relative_path: Any) -> str:
    if asset_base_url == "/viewer/":
        return _local_asset_data_uri(relative_path)
    return _safe_asset_url(asset_base_url, relative_path)


def legacy_v3_payload(plan: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "v": 3,
        "g": [
            {
                "d": segment["destination_id"],
                "s": segment.get("start_date") or "",
                "e": segment.get("end_date") or "",
            }
            for segment in plan["segments"]
        ],
        "i": [],
    }
    for day in plan["days"]:
        payload["i"].append(
            {
                "a": [
                    {
                        "d": activity["destination_id"],
                        "h": activity["time"],
                        "l": activity["location"] or activity["title"],
                        "k": activity["icon"],
                        "m": activity.get("memo") or "",
                    }
                    for activity in day["activities"]
                ]
            }
        )
    return payload


def legacy_share_url(plan: dict[str, Any], legacy_base_url: str) -> str:
    return f"{legacy_base_url.rstrip('/')}/#plan={encode_legacy_v3(legacy_v3_payload(plan))}"


def _safe_hex(value: Any, fallback: str = "#e85d2a") -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else fallback


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _safe_google_maps_url(value: Any) -> str | None:
    """Allow only official, non-credentialed Google Maps links in exported anchors."""
    if not value:
        return None
    text = str(value).strip()
    if _has_control_characters(text):
        return None
    parsed = urlsplit(text)
    try:
        has_non_default_port = parsed.port is not None
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.google.com"
        or has_non_default_port
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/maps/")
    ):
        return None
    return text


def _validated_google_map_search_url(value: Any, query: str) -> str | None:
    safe = _safe_google_maps_url(value)
    if not safe:
        return None
    parsed = urlsplit(safe)
    expected = {"api": ["1"], "query": [query]}
    if parsed.path != "/maps/search/" or parsed.fragment:
        return None
    return safe if parse_qs(parsed.query, keep_blank_values=True) == expected else None


def _validated_google_directions_url(
    value: Any,
    origin: str,
    destination: str,
    mode: str,
) -> str | None:
    safe = _safe_google_maps_url(value)
    if not safe:
        return None
    parsed = urlsplit(safe)
    expected = {
        "api": ["1"],
        "origin": [origin],
        "destination": [destination],
        "travelmode": [mode],
    }
    if parsed.path != "/maps/dir/" or parsed.fragment:
        return None
    return safe if parse_qs(parsed.query, keep_blank_values=True) == expected else None


def _safe_asset_url(asset_base_url: str, relative_path: Any) -> str:
    """Build an image URL while rejecting executable or credential-bearing bases."""
    relative = str(relative_path or "").strip().lstrip("/")
    if (
        not relative
        or _has_control_characters(relative)
        or any(part == ".." for part in relative.split("/"))
        or ":" in relative.split("/", 1)[0]
    ):
        return ""

    base = str(asset_base_url or "").strip().rstrip("/")
    candidate = f"{base}/{relative}" if base else relative
    if _has_control_characters(candidate) or any(
        character in candidate for character in {'"', "'", "<", ">", "\\"}
    ):
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme:
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return ""
    elif candidate.startswith("//"):
        return ""
    return candidate


def _activity_query(activity: dict[str, Any]) -> str:
    return str(
        activity.get("map_query")
        or activity.get("location")
        or activity.get("title")
        or ""
    ).strip()


def _google_map_search_url(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": 1, "query": query}
    )


def _google_directions_url(origin: str, destination: str, mode: str) -> str:
    return "https://www.google.com/maps/dir/?" + urlencode(
        {
            "api": 1,
            "origin": origin,
            "destination": destination,
            "travelmode": mode,
        }
    )


def _whole_day_route_url(activities: list[dict[str, Any]]) -> str | None:
    queries = [_activity_query(activity) for activity in activities]
    queries = [query for query in queries if query]
    if not queries:
        return None
    if len(queries) == 1:
        return _google_map_search_url(queries[0])
    params: dict[str, Any] = {
        "api": 1,
        "origin": queries[0],
        "destination": queries[-1],
        "travelmode": "transit",
    }
    if len(queries) > 2:
        params["waypoints"] = "|".join(queries[1:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def _route_options_for_leg(
    day: dict[str, Any],
    index: int,
    origin: dict[str, Any],
    destination: dict[str, Any],
) -> tuple[dict[str, str], str]:
    """Prefer validated backend legs, then fill gaps with official URL templates."""
    origin_query = _activity_query(origin)
    destination_query = _activity_query(destination)
    backend_leg: dict[str, Any] = {}
    legs = day.get("legs")
    if isinstance(legs, list) and index < len(legs) and isinstance(legs[index], dict):
        backend_leg = legs[index]
    backend_routes = backend_leg.get("route_urls")
    if not isinstance(backend_routes, dict):
        backend_routes = {}

    routes: dict[str, str] = {}
    for mode in TRAVEL_MODES:
        safe_url = _validated_google_directions_url(
            backend_routes.get(mode),
            origin_query,
            destination_query,
            mode,
        )
        routes[mode] = safe_url or _google_directions_url(
            origin_query,
            destination_query,
            mode,
        )
    suggested = str(backend_leg.get("suggested_mode") or "transit")
    if suggested not in TRAVEL_MODES:
        suggested = "transit"
    return routes, suggested


def _format_rate(rate: dict[str, Any]) -> str:
    currency = str(rate.get("currency") or "---").upper()
    value = rate.get("krw_per_unit")
    try:
        formatted = f"{float(value):,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return f"1 {currency} = 조회 불가"
    return f"1 {currency} = ₩{formatted}"


def _json_for_html_script(value: Any) -> str:
    """Serialize inert JSON without allowing an embedded script end tag."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_export_html(
    plan: dict[str, Any],
    catalog: Catalog,
    asset_base_url: str = "/viewer/",
) -> str:
    esc = lambda value, quote=False: html.escape(str(value), quote=quote)
    first_destination = catalog.get(plan["segments"][0]["destination_id"])
    accent = _safe_hex(first_destination.get("accent"))
    font_light = _export_asset(asset_base_url, "assets/fonts/GmarketSansLight.woff2")
    font_medium = _export_asset(asset_base_url, "assets/fonts/GmarketSansMedium.woff2")
    font_bold = _export_asset(asset_base_url, "assets/fonts/GmarketSansBold.woff2")

    first_start = plan["segments"][0].get("start_date")
    last_end = plan["segments"][-1].get("end_date")
    date_label = f"{first_start} — {last_end}" if first_start and last_end else "날짜 미정"
    weather = plan.get("live_data", {}).get("weather", {})
    weather_label = weather.get("message") or {
        "live": "선택 날짜 예보 포함",
        "skipped": "실시간 정보 생략",
    }.get(weather.get("status"), "예보 범위 밖 또는 이용 불가")

    destination_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_destination_ids: set[str] = set()
    for segment in plan["segments"]:
        destination_id = str(segment["destination_id"])
        if destination_id in seen_destination_ids:
            continue
        seen_destination_ids.add(destination_id)
        destination_rows.append((segment, catalog.get(destination_id)))

    exchange = plan.get("live_data", {}).get("exchange", {})
    exchange_rates = exchange.get("rates") if isinstance(exchange, dict) else []
    if not isinstance(exchange_rates, list):
        exchange_rates = []
    rate_by_currency = {
        str(rate.get("currency") or "").upper(): rate
        for rate in exchange_rates
        if isinstance(rate, dict)
    }

    clock_rows = [
        """
        <div class="clock-row">
          <span>한국 · 서울</span>
          <time data-live-clock data-time-zone="Asia/Seoul">시간 계산 중</time>
          <small>Asia/Seoul</small>
        </div>
        """
    ]
    destination_cards = []
    phrase_sections = []
    city_index_buttons = []
    hero_assets: dict[str, str] = {}
    for index, (segment, destination) in enumerate(destination_rows, start=1):
        city_name = str(destination["cityKo"])
        time_zone = str(destination["timeZone"])
        currency = str(destination["currency"]["code"])
        city_map_url = _google_map_search_url(
            f"{destination['city']}, {destination['country']}"
        )
        city_hero = _export_asset(asset_base_url, destination["heroImage"])
        hero_assets[str(destination["id"])] = city_hero
        clock_rows.append(
            f"""
            <div class="clock-row" data-city-id="{esc(destination['id'], quote=True)}">
              <span>{esc(destination['countryKo'])} · {esc(city_name)}</span>
              <time data-live-clock data-time-zone="{esc(time_zone, quote=True)}">시간 계산 중</time>
              <small>{esc(time_zone)}</small>
            </div>
            """
        )
        rate = rate_by_currency.get(currency.upper())
        city_index_buttons.append(
            f'<button type="button" data-city-select="{esc(destination["id"], quote=True)}" aria-pressed="false">{esc(city_name)}</button>'
        )
        rate_summary = _format_rate(rate) if rate else f"{currency} · 조회하지 않음"
        destination_cards.append(
            f"""
            <article class="destination-card" data-city-id="{esc(destination['id'], quote=True)}" data-currency="{esc(currency, quote=True)}" data-rate="{esc(rate.get('krw_per_unit') if rate else '', quote=True)}">
              <div class="city-copy">
                <div class="city-index">CITY {index:02d} / {esc(destination['countryKo'])}</div>
                <h2>{esc(city_name)} <i>{esc(destination['city'])}</i></h2>
                <p>{esc(destination['summary'])}</p>
                <a class="city-map-link" href="{esc(city_map_url, quote=True)}" target="_blank" rel="noopener noreferrer">GOOGLE MAPS에서 도시 열기</a>
                <dl>
                  <div><dt>구간</dt><dd>DAY {esc(segment['day_start'])}—{esc(segment['day_end'])}</dd></div>
                  <div><dt>통화</dt><dd>{esc(rate_summary)}</dd></div>
                  <div><dt>언어</dt><dd>{esc(destination.get('phraseLabel') or '기본 회화')}</dd></div>
                </dl>
              </div>
            </article>
            """
        )

        phrase_rows = []
        for phrase in destination.get("phrases", []):
            phrase_rows.append(
                f"""
                <li>
                  <strong>{esc(phrase.get('text') or '')}</strong>
                  <span>{esc(phrase.get('pron') or '')}</span>
                  <small>{esc(phrase.get('meaning') or '')}</small>
                </li>
                """
            )
        phrase_sections.append(
            f"""
            <article class="phrase-city" data-city-id="{esc(destination['id'], quote=True)}">
              <header><b>{esc(city_name)}</b><span>{esc(destination.get('phraseLabel') or '기본 회화')}</span></header>
              <ul>{''.join(phrase_rows)}</ul>
            </article>
            """
        )

    exchange_cards = []
    for _, destination in destination_rows:
        currency = str(destination["currency"]["code"]).upper()
        rate = rate_by_currency.get(currency)
        if rate:
            timestamp = (
                rate.get("updated_at")
                or rate.get("fetched_at")
                or exchange.get("updated_at")
                or exchange.get("fetched_at")
                or "조회시각 없음"
            )
            exchange_cards.append(
                f"""
                <div class="fx-row" data-fx-row data-city-id="{esc(destination['id'], quote=True)}" data-snapshot-at="{esc(timestamp, quote=True)}">
                  <span>{esc(destination['cityKo'])} · {esc(currency)}</span>
                  <strong data-fx-value>{esc(_format_rate(rate))}</strong>
                  <small data-fx-meta>{esc(rate.get('status') or exchange.get('status') or 'unknown')} · {esc(timestamp)}</small>
                </div>
                """
            )
        else:
            exchange_cards.append(
                f"""
                <div class="fx-row" data-fx-row data-city-id="{esc(destination['id'], quote=True)}" data-snapshot-at="없음">
                  <span>{esc(destination['cityKo'])} · {esc(currency)}</span>
                  <strong data-fx-value>환율 snapshot 없음</strong>
                  <small data-fx-meta>include_live_data=true로 계획을 다시 생성하면 조회합니다.</small>
                </div>
                """
            )

    day_sections = []
    for day in plan["days"]:
        activities = day["activities"]
        activity_rows = []
        for index, activity in enumerate(activities):
            map_url = _validated_google_map_search_url(
                activity.get("map_url"),
                _activity_query(activity),
            )
            map_url = map_url or _google_map_search_url(_activity_query(activity))
            activity_rows.append(
                """
                <li class="stop">
                  <time>{time}</time>
                  <div>
                    <strong>{title}</strong>
                    <span>{location}</span>
                    {memo}
                  </div>
                  <a href="{map_url}" target="_blank" rel="noopener noreferrer">MAP</a>
                </li>
                """.format(
                    time=esc(activity["time"]),
                    title=esc(activity["title"]),
                    location=esc(activity.get("location") or activity["title"]),
                    memo=(
                        f"<small>{esc(activity['memo'])}</small>"
                        if activity.get("memo")
                        else ""
                    ),
                    map_url=esc(map_url, quote=True),
                )
            )
            if index < len(activities) - 1:
                next_activity = activities[index + 1]
                routes, suggested = _route_options_for_leg(
                    day,
                    index,
                    activity,
                    next_activity,
                )
                mode_links = "".join(
                    f'<a class="{"suggested" if mode == suggested else ""}" '
                    f'href="{esc(routes[mode], quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer">{MODE_LABELS[mode]}</a>'
                    for mode in TRAVEL_MODES
                )
                activity_rows.append(
                    f"""
                    <li class="leg">
                      <span>{esc(activity['title'])} → {esc(next_activity['title'])}</span>
                      <nav aria-label="{esc(activity['title'], quote=True)}에서 {esc(next_activity['title'], quote=True)}까지 이동 경로">{mode_links}</nav>
                    </li>
                    """
                )

        route_url = _whole_day_route_url(activities)
        route_link = (
            f'<a class="route" href="{esc(route_url, quote=True)}" target="_blank" '
            'rel="noopener noreferrer">이 날의 전체 동선</a>'
            if route_url
            else ""
        )
        day_sections.append(
            """
            <section class="day">
              <header><b>DAY {day}</b><span>{date}</span><h2>{title}</h2></header>
              <div><ol>{activities}</ol>{route_link}</div>
            </section>
            """.format(
                day=esc(day["day"]),
                date=esc(day.get("date") or "DATE OPEN"),
                title=esc(day["title"]),
                activities="".join(activity_rows),
                route_link=route_link,
            )
        )

    compact_plan = html.escape(
        json.dumps(plan, ensure_ascii=False, separators=(",", ":")), quote=False
    )
    hero_assets_json = _json_for_html_script(hero_assets)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(plan['title'])}</title>
<style>
@font-face{{font-family:Gmarket;src:url("{font_light}") format("woff2");font-weight:300}}@font-face{{font-family:Gmarket;src:url("{font_medium}") format("woff2");font-weight:500}}@font-face{{font-family:Gmarket;src:url("{font_bold}") format("woff2");font-weight:700}}
:root{{--paper:#f8f7f1;--muted:#c7cdc3;--line:rgba(248,247,241,.28);--accent:{accent};--hero:none}}*{{box-sizing:border-box}}html,body{{margin:0;min-width:320px;background:#111611;color:var(--paper);font-family:Gmarket,"Apple SD Gothic Neo",sans-serif;font-weight:500;font-variant-numeric:tabular-nums;overflow-x:hidden}}body:before,body:after{{content:"";position:fixed;inset:0;z-index:-2;background:#111611 var(--hero) center/cover no-repeat}}body:after{{z-index:-1;background:linear-gradient(90deg,rgba(6,9,6,.87),rgba(7,10,7,.74) 60%,rgba(7,10,7,.84))}}button,input{{font:inherit;font-variant-numeric:tabular-nums}}button{{min-height:44px;cursor:pointer}}a{{color:inherit;text-underline-offset:.22em}}:focus-visible{{outline:3px solid #fff;outline-offset:3px}}main{{width:min(1180px,calc(100% - 32px));margin:auto;padding:clamp(38px,8vw,95px) 0 60px}}.cover{{max-width:850px;padding-bottom:76px;border-bottom:1px solid var(--line)}}.eyebrow,.section-label,.city-index,.facts,.day>header b,.day>header span,footer{{font-size:11px;letter-spacing:.1em;color:var(--muted)}}h1{{max-width:11ch;margin:16px 0;font-size:clamp(48px,8vw,100px);line-height:.98;letter-spacing:-.065em}}.intro p{{max-width:48ch;font-size:18px;font-weight:300;line-height:1.7}}.facts{{padding-top:12px;border-top:1px solid var(--line);line-height:1.9}}.operations{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:clamp(28px,6vw,84px);padding:54px 0;border-bottom:1px solid var(--line)}}.operations>section{{min-width:0}}.section-label{{margin:0 0 16px}}.clock-row,.fx-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 14px;padding:12px 0;border-top:1px solid var(--line)}}.clock-row[data-city-id],.fx-row[data-city-id]{{display:none}}.clock-row.active,.fx-row.active{{display:grid}}.clock-row span,.fx-row span{{font-size:13px}}.clock-row time,.fx-row strong{{font-size:18px;font-weight:700}}.clock-row small,.fx-row small{{grid-column:1/-1;color:var(--muted);font-size:11px;overflow-wrap:anywhere}}.fx-refresh-note{{min-height:2.8em;margin:0 0 8px;color:var(--muted);font-size:11px;line-height:1.4}}.fx-refresh-note[data-state=error],.fx-refresh-note[data-state=offline]{{color:#ffd0bd}}.fx-calculator{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}}.fx-calculator input{{width:100%;min-height:44px;border:1px solid var(--line);background:rgba(5,8,5,.55);color:var(--paper);padding:10px}}.destination-index{{padding:54px 0 0}}.city-selector{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 24px}}.city-selector button{{border:1px solid var(--line);background:rgba(5,8,5,.35);color:var(--paper);padding:8px 13px}}.city-selector button[aria-pressed=true]{{border-color:var(--accent);background:var(--accent);color:#111611;font-weight:700}}.destination-card{{display:none;border-top:1px solid var(--line);padding:22px 0 34px}}.destination-card.active{{display:block}}.city-copy{{min-width:0}}.city-copy h2{{margin:7px 0;font-size:38px;letter-spacing:-.045em}}.city-copy h2 i{{color:var(--muted);font-size:15px;font-style:normal}}.city-copy p{{max-width:52ch;font-size:18px;font-weight:300;line-height:1.65;overflow-wrap:anywhere}}.city-map-link,.stop a,.leg a,.route{{display:inline-flex;align-items:center;min-height:44px;overflow-wrap:anywhere}}.city-map-link{{margin:1px 0 20px;font-size:11px;font-weight:700}}.city-copy dl{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.city-copy dl div{{min-width:0;padding-top:8px;border-top:1px solid var(--line)}}.city-copy dt{{color:var(--muted);font-size:11px}}.city-copy dd{{margin:5px 0 0;font-size:12px;overflow-wrap:anywhere}}.phrase-guide{{padding:38px 0;border-bottom:1px solid var(--line)}}.phrase-search{{width:min(100%,360px);min-height:44px;border:1px solid var(--line);background:rgba(5,8,5,.55);color:var(--paper);padding:10px;margin-bottom:14px}}.phrase-city{{display:none}}.phrase-city.active{{display:block}}.phrase-city header{{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;padding-bottom:9px;border-bottom:1px solid var(--line)}}.phrase-city ul,ol{{margin:0;padding:0;list-style:none}}.phrase-city li{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.8fr);gap:3px 12px;padding:11px 0;border-bottom:1px solid var(--line)}}.phrase-city li span,.phrase-city li small{{color:var(--muted);font-size:12px;overflow-wrap:anywhere}}.phrase-city li small{{grid-column:1/-1}}.day{{display:grid;grid-template-columns:155px minmax(0,1fr);gap:clamp(24px,4vw,62px);padding:28px 0;border-bottom:1px solid var(--line)}}.day h2{{margin:15px 0 0;font-size:23px;line-height:1.3;overflow-wrap:anywhere}}.stop{{display:grid;grid-template-columns:56px minmax(0,1fr) 42px;gap:12px;padding:11px 0}}.stop time{{font-weight:700}}.stop strong,.stop span,.stop small{{display:block;overflow-wrap:anywhere}}.stop span,.stop small{{margin-top:3px;color:var(--muted);font-size:13px}}.stop a,.route{{font-size:11px;font-weight:700}}.leg{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;margin:3px 0 8px 68px;padding:10px 0;border-top:1px solid var(--line);color:var(--muted);font-size:12px}}.leg nav{{display:flex;flex-wrap:wrap;gap:10px}}.leg a{{color:var(--paper);font-size:11px}}.route{{margin:14px 0 0 68px}}footer{{padding:28px 0 0;line-height:1.7;overflow-wrap:anywhere}}footer details{{display:none}}@media(max-width:680px){{main{{width:min(100% - 24px,1180px);padding-top:50px}}h1{{font-size:clamp(46px,15vw,66px)}}.operations{{grid-template-columns:1fr}}.day{{grid-template-columns:1fr;gap:13px}}.city-copy dl{{grid-template-columns:1fr 1fr}}.phrase-city li{{grid-template-columns:1fr;gap:3px}}.phrase-city li small{{grid-column:auto}}.leg{{grid-template-columns:1fr;margin-left:0}}.route{{margin-left:0}}}}@media(prefers-reduced-motion:reduce){{*,*:before,*:after{{animation-duration:0s!important;transition-duration:0s!important;scroll-behavior:auto!important}}}}
body{{position:relative;isolation:isolate;min-height:100vh}}body:before{{z-index:0;pointer-events:none}}body:after{{z-index:1;pointer-events:none}}main{{position:relative;z-index:2}}
:root{{--control-bg:#f8f7f1;--control-text:#080b08}}body:after{{background:rgba(5,8,5,.8)}}h1{{max-width:13ch;overflow-wrap:anywhere;word-break:keep-all}}.city-selector button[aria-pressed=true]{{border-color:var(--accent);background:var(--control-bg);color:var(--control-text);box-shadow:inset 0 -3px 0 var(--accent)}}@media(max-width:680px){{h1{{max-width:100%;font-size:clamp(38px,12.5vw,58px)}}}}
</style>
</head>
<body><main>
<section class="cover">
  <div class="intro">
    <div><div class="eyebrow">ROUTE / 69 · REV {esc(plan['revision'])}</div><h1>{esc(plan['title'])}</h1><p data-active-city-summary>{esc(first_destination['summary'])}</p></div>
    <div class="facts">DATE {esc(date_label)}<br>WEATHER {esc(weather_label)}<br>PACE {esc(str(plan['pace']).upper())}</div>
  </div>
</section>
<section class="operations" aria-label="여행 실시간 정보">
  <section><h2 class="section-label">LIVE CLOCKS · 1초마다 기기에서 갱신</h2>{''.join(clock_rows)}</section>
  <section><h2 class="section-label">FX SNAPSHOT · KRW 기준</h2><p class="fx-refresh-note" data-fx-refresh-status data-state="snapshot" role="status" aria-live="polite">저장된 snapshot을 먼저 표시합니다. 서버 연결 시 최신값을 확인합니다.</p>{''.join(exchange_cards)}<div class="fx-calculator"><input data-local-amount type="number" inputmode="decimal" value="100" aria-label="현지 통화 금액"><input data-krw-amount type="number" inputmode="numeric" aria-label="원화 금액"></div></section>
</section>
<section class="destination-index"><h2 class="section-label">DESTINATION INDEX · 도시별 이미지와 정보</h2><nav class="city-selector" aria-label="여행 도시 선택">{''.join(city_index_buttons)}</nav>{''.join(destination_cards)}</section>
<section class="phrase-guide"><h2 class="section-label">POCKET PHRASES · 도시별 기본 회화</h2><input class="phrase-search" type="search" placeholder="한국어 의미·현지어·발음 검색" aria-label="기본 회화 검색"><div class="phrase-grid">{''.join(phrase_sections)}</div></section>
{''.join(day_sections)}
<footer>환율은 저장된 snapshot에서 시작해 서버 연결 시 최신값을 확인하며, 결제 전에는 다시 확인하세요. 경로 링크는 Google Maps에서 실시간 운행·소요시간을 확인합니다.<br>Canonical catalog {esc(plan['catalog']['digest'])} · Plan {esc(plan['plan_id'])}<details><summary>JSON</summary><code>{compact_plan}</code></details></footer>
</main>
<script type="application/json" id="city-hero-assets">{hero_assets_json}</script>
<script>
(function () {{
  let heroAssets = {{}};
  try {{ heroAssets = JSON.parse(document.getElementById("city-hero-assets")?.textContent || "{{}}"); }} catch (error) {{ /* a missing background never blocks the itinerary */ }}
  const formatters = new Map();
  function updateClocks() {{
    const now = new Date();
    document.querySelectorAll("[data-live-clock]").forEach((node) => {{
      const zone = node.dataset.timeZone;
      try {{
        let formatter = formatters.get(zone);
        if (!formatter) {{
          formatter = new Intl.DateTimeFormat("ko-KR", {{
            timeZone: zone,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            weekday: "short",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false
          }});
          formatters.set(zone, formatter);
        }}
        node.textContent = formatter.format(now);
        node.dateTime = now.toISOString();
      }} catch (error) {{
        node.textContent = "시간대 확인 필요";
      }}
    }});
  }}
  updateClocks();
  window.setInterval(updateClocks, 1000);
  const localAmount = document.querySelector("[data-local-amount]");
  const krwAmount = document.querySelector("[data-krw-amount]");
  const refreshStatus = document.querySelector("[data-fx-refresh-status]");
  let activeRate = 0;
  function setRefreshStatus(message, state) {{
    refreshStatus.textContent = message;
    refreshStatus.dataset.state = state;
  }}
  function formatRate(currency, value) {{
    const formatted = new Intl.NumberFormat("ko-KR", {{ maximumFractionDigits: 4 }}).format(value);
    return `1 ${{currency}} = ₩${{formatted}}`;
  }}
  function calculateFromLocal() {{
    if (activeRate > 0) krwAmount.value = String(Math.round(Number(localAmount.value || 0) * activeRate));
  }}
  function calculateFromKrw() {{
    if (activeRate > 0) localAmount.value = String(Math.round((Number(krwAmount.value || 0) / activeRate) * 100) / 100);
  }}
  localAmount.addEventListener("input", calculateFromLocal);
  krwAmount.addEventListener("input", calculateFromKrw);
  function selectCity(id) {{
    const card = document.querySelector(`.destination-card[data-city-id="${{CSS.escape(id)}}"]`);
    if (!card) return;
    document.querySelectorAll("[data-city-select]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.citySelect === id)));
    document.querySelectorAll(".destination-card,.phrase-city,.clock-row[data-city-id],.fx-row[data-city-id]").forEach((node) => node.classList.toggle("active", node.dataset.cityId === id));
    const hero = heroAssets[id];
    if (hero) document.body.style.setProperty("--hero", `url(${{JSON.stringify(hero)}})`);
    else document.body.style.removeProperty("--hero");
    const summary = document.querySelector("[data-active-city-summary]");
    const citySummary = card.querySelector(".city-copy p");
    if (summary && citySummary) summary.textContent = citySummary.textContent;
    activeRate = Number(card.dataset.rate) || 0;
    localAmount.disabled = !activeRate;
    krwAmount.disabled = !activeRate;
    calculateFromLocal();
    const hasSnapshot = activeRate > 0;
    if (location.protocol === "file:") {{
      setRefreshStatus(hasSnapshot ? "독립 HTML(file://)에서는 환율 실시간 갱신을 사용할 수 없습니다. 저장된 snapshot을 표시합니다." : "독립 HTML(file://)에서는 환율 실시간 갱신을 사용할 수 없고 정상 snapshot도 없습니다. 계산기를 비활성화했습니다.", "offline");
    }} else if (!navigator.onLine) {{
      setRefreshStatus(hasSnapshot ? "오프라인입니다. 저장된 snapshot을 유지합니다." : "오프라인이며 정상 snapshot이 없습니다. 계산기를 비활성화했습니다.", "offline");
    }} else {{
      refreshRate(id, card);
    }}
  }}
  async function refreshRate(id, card) {{
    const row = document.querySelector(`.fx-row[data-city-id="${{CSS.escape(id)}}"]`);
    const valueNode = row?.querySelector("[data-fx-value]");
    const metaNode = row?.querySelector("[data-fx-meta]");
    const attemptedAt = new Date().toISOString();
    if (card.classList.contains("active")) setRefreshStatus(`${{id}} 환율을 갱신하는 중입니다…`, "loading");
    try {{
      const response = await fetch(`/api/city-guide/${{encodeURIComponent(id)}}`, {{ cache: "no-store" }});
      const payload = await response.json();
      if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
      const exchange = payload.exchange || {{}};
      const matchedRate = (exchange.rates || []).find((item) => item.currency === card.dataset.currency);
      const nextRate = matchedRate || exchange;
      const rate = Number(nextRate.krw_per_unit);
      if (!(rate > 0)) throw new Error(nextRate.message || "환율 값이 없습니다.");
      const timestamp = nextRate.updated_at || nextRate.fetched_at || attemptedAt;
      const source = nextRate.source ? ` · ${{nextRate.source}}` : "";
      card.dataset.rate = String(rate);
      row.dataset.snapshotAt = timestamp;
      row.dataset.refreshState = "live";
      valueNode.textContent = formatRate(card.dataset.currency, rate);
      metaNode.textContent = `${{nextRate.status || "live"}} · ${{timestamp}}${{source}}`;
      if (card.classList.contains("active")) {{
        activeRate = rate;
        calculateFromLocal();
        setRefreshStatus(`${{id}} 환율 갱신 완료 · ${{timestamp}}`, "live");
      }}
    }} catch (error) {{
      if (row) row.dataset.refreshState = navigator.onLine ? "error" : "offline";
      const hasSnapshot = Number(card.dataset.rate) > 0;
      if (metaNode) metaNode.textContent = hasSnapshot
        ? `${{navigator.onLine ? "갱신 실패" : "오프라인"}} · snapshot ${{row?.dataset.snapshotAt || "저장 시점 불명"}} 유지 · 시도 ${{attemptedAt}}`
        : `${{navigator.onLine ? "갱신 실패" : "오프라인"}} · 정상 snapshot 없음 · 계산기 비활성화 · 시도 ${{attemptedAt}}`;
      if (!hasSnapshot) {{
        activeRate = 0;
        localAmount.disabled = true;
        krwAmount.disabled = true;
      }}
      if (card.classList.contains("active")) setRefreshStatus(hasSnapshot
        ? `${{navigator.onLine ? "환율 갱신에 실패했습니다" : "오프라인입니다"}}. 저장된 snapshot을 유지합니다.`
        : `${{navigator.onLine ? "환율 갱신에 실패했습니다" : "오프라인입니다"}}. 정상 snapshot이 없어 계산기를 비활성화했습니다.`, navigator.onLine ? "error" : "offline");
    }}
  }}
  document.querySelectorAll("[data-city-select]").forEach((button) => button.addEventListener("click", () => selectCity(button.dataset.citySelect)));
  const phraseSearch = document.querySelector(".phrase-search");
  phraseSearch.addEventListener("input", () => {{
    const query = phraseSearch.value.trim().toLocaleLowerCase("ko-KR");
    document.querySelectorAll(".phrase-city.active li").forEach((row) => row.hidden = query && !row.textContent.toLocaleLowerCase("ko-KR").includes(query));
  }});
  selectCity(document.querySelector("[data-city-select]")?.dataset.citySelect);
  window.setInterval(() => {{ const active = document.querySelector(".destination-card.active"); if (active && location.protocol !== "file:") refreshRate(active.dataset.cityId, active); }}, 15 * 60 * 1000);
  window.addEventListener("offline", () => {{
    const active = document.querySelector(".destination-card.active");
    const hasSnapshot = Number(active?.dataset.rate) > 0;
    if (!hasSnapshot) {{
      activeRate = 0;
      localAmount.disabled = true;
      krwAmount.disabled = true;
    }}
    setRefreshStatus(hasSnapshot ? "오프라인입니다. 저장된 snapshot을 유지합니다." : "오프라인이며 정상 snapshot이 없습니다. 계산기를 비활성화했습니다.", "offline");
  }});
  window.addEventListener("online", () => {{ const active = document.querySelector(".destination-card.active"); if (active && location.protocol !== "file:") refreshRate(active.dataset.cityId, active); }});
}}());
</script>
</body></html>"""
