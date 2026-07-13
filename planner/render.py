from __future__ import annotations

import html
import json
from typing import Any

from .catalog import Catalog
from .tokens import encode_legacy_v3


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


def render_export_html(
    plan: dict[str, Any],
    catalog: Catalog,
    asset_base_url: str = "/viewer/",
) -> str:
    first_destination = catalog.get(plan["segments"][0]["destination_id"])
    accent = first_destination.get("accent") or "#e85d2a"
    hero = asset_base_url.rstrip("/") + "/" + first_destination["heroImage"]
    date_label = (
        f"{plan['segments'][0]['start_date']} — {plan['segments'][-1]['end_date']}"
        if plan["segments"][0].get("start_date")
        else "날짜 미정"
    )
    weather = plan.get("live_data", {}).get("weather", {})
    weather_label = weather.get("message") or {
        "live": "선택 날짜 예보 포함",
        "skipped": "실시간 정보 생략",
    }.get(weather.get("status"), "예보 범위 밖 또는 이용 불가")
    day_sections = []
    for day in plan["days"]:
        activity_rows = []
        for activity in day["activities"]:
            activity_rows.append(
                """
                <li class="stop">
                  <time>{time}</time>
                  <div>
                    <strong>{title}</strong>
                    <span>{location}</span>
                    {memo}
                  </div>
                  <a href="{map_url}" target="_blank" rel="noreferrer">MAP</a>
                </li>
                """.format(
                    time=html.escape(str(activity["time"])),
                    title=html.escape(str(activity["title"])),
                    location=html.escape(str(activity["location"])),
                    memo=(
                        f"<small>{html.escape(str(activity['memo']))}</small>"
                        if activity.get("memo")
                        else ""
                    ),
                    map_url=html.escape(str(activity["map_url"]), quote=True),
                )
            )
        day_sections.append(
            """
            <section class="day">
              <header><b>DAY {day}</b><span>{date}</span><h2>{title}</h2></header>
              <ol>{activities}</ol>
              <a class="route" href="{route}" target="_blank" rel="noreferrer">이 날의 전체 동선</a>
            </section>
            """.format(
                day=day["day"],
                date=html.escape(str(day.get("date") or "DATE OPEN")),
                title=html.escape(str(day["title"])),
                activities="".join(activity_rows),
                route=html.escape(str(day["route_map_url"]), quote=True),
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
<title>{html.escape(plan['title'])}</title>
<style>
:root{{--ink:#101410;--paper:#f2f1ea;--line:#c9c8bf;--accent:{html.escape(accent)};--muted:#64675f}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ink);color:var(--ink);font-family:"IBM Plex Sans KR","Apple SD Gothic Neo",sans-serif}}
main{{width:min(100%,760px);margin:auto;background:var(--paper);min-height:100vh}}
.cover{{display:grid;grid-template-columns:42% 58%;border-bottom:8px solid var(--accent)}}
.cover img{{width:100%;height:100%;min-height:260px;object-fit:cover;filter:saturate(.76) contrast(1.04)}}
.intro{{padding:28px 24px;display:flex;flex-direction:column;justify-content:space-between}}
.eyebrow,.facts,code,time,.route{{font-family:"IBM Plex Mono",monospace}}
.eyebrow{{font-size:11px;letter-spacing:.16em;text-transform:uppercase}}
h1{{font-size:clamp(34px,8vw,62px);line-height:.96;margin:20px 0;letter-spacing:-.05em}}
.facts{{font-size:12px;line-height:1.8;border-top:1px solid;padding-top:12px}}
.day{{display:grid;grid-template-columns:118px 1fr;border-bottom:1px solid var(--line);padding:24px}}
.day header{{padding-right:18px}}.day header b,.day header span{{display:block;font:11px/1.5 "IBM Plex Mono",monospace}}
.day h2{{font-size:20px;line-height:1.1;margin:12px 0}}
ol{{list-style:none;margin:0;padding:0}}
.stop{{display:grid;grid-template-columns:58px 1fr 38px;gap:10px;border-top:1px solid var(--line);padding:14px 0}}
.stop time{{font-size:12px;font-weight:700}}.stop strong,.stop span,.stop small{{display:block}}
.stop span{{font-size:13px;margin-top:3px}}.stop small{{color:var(--muted);font-size:11px;margin-top:5px}}
.stop a,.route{{font-size:10px;color:var(--ink);font-weight:700;text-decoration-thickness:2px}}
.route{{display:inline-block;margin-top:16px}}
footer{{padding:22px 24px 40px;font-size:11px;color:var(--muted)}}
code{{word-break:break-all}}
@media(max-width:600px){{.cover{{grid-template-columns:1fr}}.cover img{{height:38vh;min-height:220px}}.day{{grid-template-columns:1fr;padding:22px 18px}}.day header{{padding:0 0 14px}}}}
</style>
</head>
<body><main>
<section class="cover">
  <img src="{html.escape(hero, quote=True)}" alt="{html.escape(first_destination['cityKo'])}">
  <div class="intro">
    <div><div class="eyebrow">ROUTE / 69 · REV {plan['revision']}</div><h1>{html.escape(plan['title'])}</h1><p>{html.escape(first_destination['summary'])}</p></div>
    <div class="facts">DATE {html.escape(date_label)}<br>WEATHER {html.escape(str(weather_label))}<br>PACE {html.escape(plan['pace'].upper())}</div>
  </div>
</section>
{''.join(day_sections)}
<footer>Canonical catalog {html.escape(plan['catalog']['digest'])} · Plan {html.escape(plan['plan_id'])}<details><summary>JSON</summary><code>{compact_plan}</code></details></footer>
</main></body></html>"""
