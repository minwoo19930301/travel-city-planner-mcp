from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

from .catalog import Catalog
from .tokens import encode_legacy_v3


TRAVEL_MODES = ("transit", "walking", "driving")
MODE_LABELS = {
    "transit": "대중교통",
    "walking": "도보",
    "driving": "차량",
}


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
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.google.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/maps/")
    ):
        return None
    return text


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
    if _has_control_characters(candidate):
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
        safe_url = _safe_google_maps_url(backend_routes.get(mode))
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


def render_export_html(
    plan: dict[str, Any],
    catalog: Catalog,
    asset_base_url: str = "/viewer/",
) -> str:
    esc = lambda value, quote=False: html.escape(str(value), quote=quote)
    first_destination = catalog.get(plan["segments"][0]["destination_id"])
    accent = _safe_hex(first_destination.get("accent"))
    hero = _safe_asset_url(asset_base_url, first_destination["heroImage"])

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
    for index, (segment, destination) in enumerate(destination_rows, start=1):
        city_name = str(destination["cityKo"])
        time_zone = str(destination["timeZone"])
        currency = str(destination["currency"]["code"])
        city_map_url = _google_map_search_url(
            f"{destination['city']}, {destination['country']}"
        )
        city_hero = _safe_asset_url(asset_base_url, destination["heroImage"])
        clock_rows.append(
            f"""
            <div class="clock-row">
              <span>{esc(destination['countryKo'])} · {esc(city_name)}</span>
              <time data-live-clock data-time-zone="{esc(time_zone, quote=True)}">시간 계산 중</time>
              <small>{esc(time_zone)}</small>
            </div>
            """
        )
        rate = rate_by_currency.get(currency.upper())
        rate_summary = _format_rate(rate) if rate else f"{currency} · 조회하지 않음"
        destination_cards.append(
            f"""
            <article class="destination-card">
              <a class="city-photo" href="{esc(city_map_url, quote=True)}" target="_blank" rel="noopener noreferrer">
                <img src="{esc(city_hero, quote=True)}" alt="{esc(city_name)} 도시 이미지" loading="lazy">
                <span>GOOGLE MAPS에서 도시 열기</span>
              </a>
              <div class="city-copy">
                <div class="city-index">CITY {index:02d} / {esc(destination['countryKo'])}</div>
                <h2>{esc(city_name)} <i>{esc(destination['city'])}</i></h2>
                <p>{esc(destination['summary'])}</p>
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
            <article class="phrase-city">
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
                <div class="fx-row">
                  <span>{esc(destination['cityKo'])} · {esc(currency)}</span>
                  <strong>{esc(_format_rate(rate))}</strong>
                  <small>{esc(rate.get('status') or exchange.get('status') or 'unknown')} · {esc(timestamp)}</small>
                </div>
                """
            )
        else:
            exchange_cards.append(
                f"""
                <div class="fx-row">
                  <span>{esc(destination['cityKo'])} · {esc(currency)}</span>
                  <strong>환율 snapshot 없음</strong>
                  <small>include_live_data=true로 계획을 다시 생성하면 조회합니다.</small>
                </div>
                """
            )

    day_sections = []
    for day in plan["days"]:
        activities = day["activities"]
        activity_rows = []
        for index, activity in enumerate(activities):
            map_url = _safe_google_maps_url(activity.get("map_url"))
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

        route_url = _safe_google_maps_url(day.get("route_map_url"))
        route_url = route_url or _whole_day_route_url(activities)
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
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(plan['title'])}</title>
<style>
:root{{--ink:#101410;--paper:#f2f1ea;--line:#c9c8bf;--accent:{accent};--muted:#64675f;--wash:#e5e4dc}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ink);color:var(--ink);font-family:"IBM Plex Sans KR","Apple SD Gothic Neo",sans-serif}}
main{{width:min(100%,880px);margin:auto;background:var(--paper);min-height:100vh}}
.cover{{display:grid;grid-template-columns:42% 58%;border-bottom:8px solid var(--accent)}}
.cover img{{width:100%;height:100%;min-height:300px;object-fit:cover;filter:saturate(.76) contrast(1.04)}}
.intro{{padding:28px 24px;display:flex;flex-direction:column;justify-content:space-between}}
.eyebrow,.facts,code,time,.route,.city-index,.clock-row,.fx-row,.leg,.destination-card dt,.phrase-city header{{font-family:"IBM Plex Mono",monospace}}
.eyebrow{{font-size:11px;letter-spacing:.16em;text-transform:uppercase}}
h1{{font-size:clamp(34px,8vw,62px);line-height:.96;margin:20px 0;letter-spacing:-.05em}}
.facts{{font-size:12px;line-height:1.8;border-top:1px solid;padding-top:12px}}
.operations{{display:grid;grid-template-columns:1fr 1fr;background:var(--ink);color:var(--paper);border-bottom:1px solid #353a35}}
.operations>section{{padding:22px 24px}}.operations>section+section{{border-left:1px solid #353a35}}
.section-label{{margin:0 0 16px;font:10px/1.2 "IBM Plex Mono",monospace;letter-spacing:.17em;color:var(--muted)}}
.operations .section-label{{color:#aeb3aa}}
.clock-row,.fx-row{{display:grid;grid-template-columns:1fr auto;gap:3px 14px;padding:10px 0;border-top:1px solid #353a35}}
.clock-row span,.fx-row span{{font-size:10px;color:#aeb3aa}}.clock-row time,.fx-row strong{{font-size:14px;font-weight:700}}
.clock-row small,.fx-row small{{grid-column:1/-1;font-size:9px;color:#858b82;overflow-wrap:anywhere}}
.destination-index{{padding:28px 24px 4px}}
.destination-card{{display:grid;grid-template-columns:minmax(210px,37%) 1fr;border-top:1px solid var(--ink);padding:16px 0 24px}}
.city-photo{{position:relative;display:block;min-height:176px;color:white;background:var(--ink);overflow:hidden}}
.city-photo img{{width:100%;height:100%;position:absolute;inset:0;object-fit:cover;filter:saturate(.78) contrast(1.05)}}
.city-photo span{{position:absolute;left:10px;bottom:9px;background:var(--ink);padding:5px 7px;font:9px/1 "IBM Plex Mono",monospace;letter-spacing:.08em}}
.city-copy{{padding:1px 0 0 22px}}.city-index{{font-size:9px;letter-spacing:.12em;color:var(--muted)}}
.city-copy h2{{font-size:28px;margin:9px 0 8px;line-height:1}}.city-copy h2 i{{font:12px/1 "IBM Plex Mono",monospace;color:var(--muted)}}
.city-copy p{{font-size:13px;line-height:1.55;margin:0 0 16px;max-width:54ch}}
.city-copy dl{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0}}
.city-copy dl div{{border-top:1px solid var(--line);padding-top:7px;min-width:0}}.city-copy dt{{font-size:8px;color:var(--muted);text-transform:uppercase}}
.city-copy dd{{font-size:10px;margin:4px 0 0;overflow-wrap:anywhere}}
.phrase-guide{{padding:24px;background:var(--wash);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}
.phrase-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}}
.phrase-city header{{display:flex;justify-content:space-between;border-bottom:2px solid var(--ink);padding-bottom:8px;font-size:10px}}
.phrase-city ul{{list-style:none;padding:0;margin:0}}.phrase-city li{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.8fr);gap:2px 10px;border-bottom:1px solid var(--line);padding:9px 0}}
.phrase-city li strong{{font-size:13px}}.phrase-city li span{{font-size:10px;text-align:right;color:var(--muted)}}.phrase-city li small{{grid-column:1/-1;font-size:10px;color:var(--muted)}}
.day{{display:grid;grid-template-columns:132px 1fr;border-bottom:1px solid var(--line);padding:24px}}
.day>header{{padding-right:18px}}.day>header b,.day>header span{{display:block;font:11px/1.5 "IBM Plex Mono",monospace}}
.day h2{{font-size:20px;line-height:1.1;margin:12px 0}}
ol{{list-style:none;margin:0;padding:0}}
.stop{{display:grid;grid-template-columns:58px 1fr 38px;gap:10px;border-top:1px solid var(--line);padding:14px 0}}
.stop time{{font-size:12px;font-weight:700}}.stop strong,.stop span,.stop small{{display:block}}
.stop span{{font-size:13px;margin-top:3px}}.stop small{{color:var(--muted);font-size:11px;margin-top:5px}}
.stop a,.route{{font-size:10px;color:var(--ink);font-weight:700;text-decoration-thickness:2px}}
.leg{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:-3px 0 10px 58px;padding:8px 10px;background:var(--wash);font-size:9px}}
.leg>span{{overflow-wrap:anywhere}}.leg nav{{display:flex;gap:4px;flex:none}}.leg a{{color:var(--ink);border:1px solid var(--line);padding:4px 5px;text-decoration:none}}
.leg a.suggested{{border-color:var(--ink);background:var(--ink);color:var(--paper)}}
.route{{display:inline-block;margin:16px 0 0 58px}}
footer{{padding:22px 24px 40px;font-size:11px;color:var(--muted)}}
code{{word-break:break-all}}
@media(max-width:680px){{
  .cover{{grid-template-columns:1fr}}.cover img{{height:38vh;min-height:220px}}.operations{{grid-template-columns:1fr}}.operations>section+section{{border-left:0;border-top:1px solid #353a35}}
  .destination-card{{grid-template-columns:1fr}}.city-photo{{min-height:210px}}.city-copy{{padding:16px 0 0}}.city-copy dl{{grid-template-columns:1fr 1fr}}
  .phrase-grid{{grid-template-columns:1fr}}.day{{grid-template-columns:1fr;padding:22px 18px}}.day>header{{padding:0 0 14px}}
  .leg{{align-items:flex-start;flex-direction:column;margin-left:0}}.leg nav{{width:100%;display:grid;grid-template-columns:repeat(3,1fr)}}.leg a{{text-align:center}}.route{{margin-left:0}}
}}
</style>
</head>
<body><main>
<section class="cover">
  <img src="{esc(hero, quote=True)}" alt="{esc(first_destination['cityKo'])}">
  <div class="intro">
    <div><div class="eyebrow">ROUTE / 69 · REV {esc(plan['revision'])}</div><h1>{esc(plan['title'])}</h1><p>{esc(first_destination['summary'])}</p></div>
    <div class="facts">DATE {esc(date_label)}<br>WEATHER {esc(weather_label)}<br>PACE {esc(str(plan['pace']).upper())}</div>
  </div>
</section>
<section class="operations" aria-label="여행 실시간 정보">
  <section><h2 class="section-label">LIVE CLOCKS · 1초마다 기기에서 갱신</h2>{''.join(clock_rows)}</section>
  <section><h2 class="section-label">FX SNAPSHOT · KRW 기준</h2>{''.join(exchange_cards)}</section>
</section>
<section class="destination-index"><h2 class="section-label">DESTINATION INDEX · 도시별 이미지와 정보</h2>{''.join(destination_cards)}</section>
<section class="phrase-guide"><h2 class="section-label">POCKET PHRASES · 도시별 기본 회화</h2><div class="phrase-grid">{''.join(phrase_sections)}</div></section>
{''.join(day_sections)}
<footer>환율은 계획 생성 시점 snapshot이며 결제 전 재확인하세요. 경로 링크는 Google Maps에서 실시간 운행·소요시간을 확인합니다.<br>Canonical catalog {esc(plan['catalog']['digest'])} · Plan {esc(plan['plan_id'])}<details><summary>JSON</summary><code>{compact_plan}</code></details></footer>
</main>
<script>
(function () {{
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
}}());
</script>
</body></html>"""
