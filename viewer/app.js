const app = document.getElementById("app");
const notice = document.getElementById("notice");
const copyButton = document.getElementById("copy-link");
const dialog = document.getElementById("token-dialog");
const openLoader = document.getElementById("open-loader");
const closeLoader = document.getElementById("close-loader");
const tokenForm = document.getElementById("token-form");
const tokenInput = document.getElementById("token-input");
const tokenError = document.getElementById("token-error");

let catalog;
let currentPlan;
const MAX_TOKEN_CHARS = 350_000;
const MAX_PLAN_BYTES = 2_000_000;

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function base64UrlBytes(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function decodeContentToken(token) {
  if (!token.startsWith("tp1.")) throw new Error("tp1. 형식의 content token이 아닙니다.");
  if (token.length > MAX_TOKEN_CHARS) throw new Error("content token이 허용 크기를 초과했습니다.");
  if (!("DecompressionStream" in window)) {
    throw new Error("이 브라우저는 압축 토큰 복원을 지원하지 않습니다.");
  }
  const bytes = base64UrlBytes(token.slice(4));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_PLAN_BYTES) {
      await reader.cancel();
      throw new Error("복원된 일정이 허용 크기를 초과했습니다.");
    }
    chunks.push(value);
  }
  const decoded = new Uint8Array(total);
  let offset = 0;
  chunks.forEach((chunk) => {
    decoded.set(chunk, offset);
    offset += chunk.byteLength;
  });
  const text = new TextDecoder().decode(decoded);
  const plan = JSON.parse(text);
  if (plan.schema_version !== 1 || !Array.isArray(plan.days)) {
    throw new Error("지원하지 않는 plan schema입니다.");
  }
  return plan;
}

function safeHttpUrl(value) {
  try {
    const parsed = new URL(String(value));
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

function planTokenFromLocation() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  return params.get("plan") || "";
}

function destinationFor(id) {
  return catalog.destinations[id] || catalog.destinations.tokyo;
}

function setTheme(destination) {
  const root = document.documentElement;
  const accent = destination.accent || "#d44928";
  root.style.setProperty("--accent", accent);
  const hex = accent.replace("#", "");
  const rgb = hex.length === 6
    ? [0, 2, 4].map((index) => parseInt(hex.slice(index, index + 2), 16)).join(", ")
    : "212, 73, 40";
  root.style.setProperty("--accent-rgb", rgb);
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.toggle("visible", Boolean(message));
}

function weatherSummary(weather) {
  if (!weather) return { title: "날씨 없음", detail: "예보 데이터가 없습니다." };
  if (weather.status === "date_required") {
    return { title: "날짜 미정", detail: weather.message };
  }
  if (weather.status === "live") {
    const liveDays = (weather.segments || []).flatMap((segment) => segment.days || []);
    if (liveDays.length) {
      const lows = liveDays.map((day) => day.min_c).filter(Number.isFinite);
      const highs = liveDays.map((day) => day.max_c).filter(Number.isFinite);
      const low = lows.length ? Math.round(Math.min(...lows)) : "—";
      const high = highs.length ? Math.round(Math.max(...highs)) : "—";
      return { title: `${low}° — ${high}°`, detail: "선택한 여행 날짜의 예보 범위" };
    }
    return { title: "예보 연결", detail: "선택 날짜의 예보를 포함합니다." };
  }
  if (weather.status === "skipped") return { title: "조회 생략", detail: "live data 옵션이 꺼져 있습니다." };
  const segment = (weather.segments || [])[0];
  return { title: "예보 대기", detail: segment?.message || weather.message || "예보 가능 범위 밖입니다." };
}

function exchangeSummary(exchange) {
  const rates = (exchange?.rates || []).filter((rate) => Number.isFinite(rate.krw_per_unit));
  if (!rates.length) {
    return { title: exchange?.status === "skipped" ? "조회 생략" : "환율 없음", detail: "KRW 환산값을 불러오지 못했습니다." };
  }
  return {
    title: rates.map((rate) => `${rate.currency} ${Number(rate.krw_per_unit).toLocaleString("ko-KR")}원`).join(" · "),
    detail: "1 현지 통화 단위 기준 · 실시간 조회값",
  };
}

function renderCover(plan, firstDestination) {
  const cover = element("section", "cover");
  const imageWrap = element("div", "cover-image");
  const image = document.createElement("img");
  image.src = `/viewer/${firstDestination.heroImage}`;
  image.alt = `${firstDestination.cityKo} 여행 풍경`;
  imageWrap.append(image, element("span", "", `${firstDestination.countryKo} / ${firstDestination.city}`));

  const copy = element("div", "cover-copy");
  const upper = element("div");
  upper.append(
    element("p", "kicker", `PLAN ${plan.plan_id.toUpperCase()} · REV ${plan.revision}`),
    element("h1", "", plan.title),
    element("p", "cover-summary", firstDestination.summary),
  );
  const facts = element("div", "cover-facts");
  const dateValue = plan.segments[0]?.start_date
    ? `${plan.segments[0].start_date} — ${plan.segments.at(-1).end_date}`
    : "DATE OPEN";
  const rows = [
    ["DATE", dateValue],
    ["PACE", String(plan.pace || "balanced").toUpperCase()],
    ["SOURCE", `CATALOG ${plan.catalog?.digest || "—"}`],
  ];
  rows.forEach(([label, value]) => {
    const row = element("div");
    row.append(element("span", "", label), element("span", "", value));
    facts.append(row);
  });
  copy.append(upper, facts);
  cover.append(imageWrap, copy);
  return cover;
}

function renderSegments(plan) {
  const board = element("section", "segment-board");
  plan.segments.forEach((segment, index) => {
    const row = element("div", "segment-row");
    row.append(element("div", "segment-code", `SEG ${String(index + 1).padStart(2, "0")}`));
    const city = element("div", "segment-city");
    city.append(element("strong", "", segment.city_ko), element("span", "", `${segment.country_ko} · ${segment.city}`));
    const duration = element("div", "segment-duration");
    duration.append(`${segment.nights} NIGHTS`, document.createElement("br"), `DAY ${segment.day_start}—${segment.day_end}`);
    row.append(city, duration);
    board.append(row);
  });
  return board;
}

function renderStatus(plan) {
  const board = element("section", "status-board");
  const weather = weatherSummary(plan.live_data?.weather);
  const exchange = exchangeSummary(plan.live_data?.exchange);
  [["TRIP WEATHER", weather], ["CURRENCY / KRW", exchange]].forEach(([label, value]) => {
    const item = element("article", "status-item");
    item.append(element("span", "status-label", label), element("strong", "", value.title), element("p", "", value.detail));
    board.append(item);
  });
  return board;
}

function renderActivity(activity) {
  const item = element("li", "stop");
  const time = document.createElement("time");
  time.textContent = activity.time;
  const marker = element("span", "stop-mark");
  marker.setAttribute("aria-hidden", "true");
  const copy = element("div", "stop-copy");
  copy.append(element("strong", "", activity.title), element("span", "", activity.location));
  if (activity.memo) copy.append(element("small", "", activity.memo));
  const mapUrl = safeHttpUrl(activity.map_url);
  const map = element(mapUrl ? "a" : "span", "map-link", mapUrl ? "MAP" : "NO MAP");
  if (mapUrl) {
    map.href = mapUrl;
    map.target = "_blank";
    map.rel = "noreferrer";
    map.setAttribute("aria-label", `${activity.title} 지도 열기`);
  } else {
    map.setAttribute("aria-disabled", "true");
  }
  item.append(time, marker, copy, map);
  return item;
}

function renderItinerary(plan) {
  const fragment = document.createDocumentFragment();
  const heading = element("header", "itinerary-heading");
  heading.append(element("h2", "", "일정표"), element("span", "", `${plan.days.length} DAYS / ${plan.days.reduce((sum, day) => sum + day.activities.length, 0)} STOPS`));
  fragment.append(heading);
  plan.days.forEach((day) => {
    const section = element("section", "day");
    const head = element("header", "day-head");
    head.append(element("span", "day-code", `DAY ${String(day.day).padStart(2, "0")}`), element("span", "day-date", day.date || "DATE OPEN"), element("h3", "", day.title));
    const body = element("div");
    const stops = element("ol", "stops");
    day.activities.forEach((activity) => stops.append(renderActivity(activity)));
    const routeUrl = safeHttpUrl(day.route_map_url);
    const route = element(routeUrl ? "a" : "span", "route-link", routeUrl ? "OPEN DAY ROUTE" : "ROUTE UNAVAILABLE");
    if (routeUrl) {
      route.href = routeUrl;
      route.target = "_blank";
      route.rel = "noreferrer";
    } else {
      route.setAttribute("aria-disabled", "true");
    }
    body.append(stops, route);
    section.append(head, body);
    fragment.append(section);
  });
  return fragment;
}

function renderSource(plan) {
  const panel = element("details", "source-panel");
  panel.append(element("summary", "", "PLAN DATA / JSON"));
  const pre = element("pre", "", JSON.stringify(plan, null, 2));
  panel.append(pre);
  return panel;
}

function renderPlan(plan, { demo = false } = {}) {
  currentPlan = plan;
  const firstDestination = destinationFor(plan.segments[0].destination_id);
  setTheme(firstDestination);
  document.title = `${plan.title} — Route / 69`;
  app.replaceChildren();
  app.setAttribute("aria-busy", "false");
  app.append(renderCover(plan, firstDestination), renderSegments(plan), renderStatus(plan));
  if (plan.shorter_variant) {
    const note = element("aside", "variant-note");
    note.append(element("b", "", "SHORTER OPTION"), document.createTextNode(plan.shorter_variant.hint));
    app.append(note);
  }
  app.append(renderItinerary(plan), renderSource(plan));
  showNotice(demo ? "FICTIONAL DEMO · MCP content token을 열면 이 자리에 실제 일정이 표시됩니다." : "");
}

async function bootstrap() {
  try {
    const response = await fetch("/data/destinations.json");
    if (!response.ok) throw new Error("canonical catalog를 불러오지 못했습니다.");
    catalog = await response.json();
    const token = planTokenFromLocation();
    if (token) {
      tokenInput.value = token;
      renderPlan(await decodeContentToken(token));
      return;
    }
    const demoResponse = await fetch("/examples/demo-plan.json");
    if (!demoResponse.ok) throw new Error("demo plan을 불러오지 못했습니다.");
    renderPlan(await demoResponse.json(), { demo: true });
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    app.replaceChildren(element("section", "loading", error.message));
    showNotice("일정을 열 수 없습니다. TOKEN OPEN에서 토큰을 다시 확인해 주세요.");
  }
}

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    copyButton.textContent = "COPIED";
    window.setTimeout(() => { copyButton.textContent = "LINK COPY"; }, 1400);
  } catch {
    showNotice("주소 복사가 차단되었습니다. 주소창의 링크를 직접 복사해 주세요.");
  }
});

openLoader.addEventListener("click", () => dialog.showModal());
closeLoader.addEventListener("click", () => dialog.close());
tokenForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  tokenError.textContent = "";
  try {
    const token = tokenInput.value.trim();
    const plan = await decodeContentToken(token);
    history.replaceState({}, "", `#plan=${token}`);
    renderPlan(plan);
    dialog.close();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    tokenError.textContent = error.message;
  }
});

window.addEventListener("hashchange", bootstrap);
bootstrap();
