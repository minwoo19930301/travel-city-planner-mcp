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
let activeDestinationId = "";
let clockTimer;
let liveRefreshTimer;
let guideRequest;
let guideContent;
let cityButtons = [];
const MAX_TOKEN_CHARS = 350_000;
const MAX_PLAN_BYTES = 2_000_000;
const LIVE_REFRESH_MS = 15 * 60 * 1000;

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

function hasValidPlanIdentity(plan) {
  return (
    typeof plan?.plan_id === "string"
    && Boolean(plan.plan_id.trim())
    && Number.isInteger(plan.revision)
    && plan.revision > 0
  );
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
  if (
    !plan || plan.schema_version !== 1 || !hasValidPlanIdentity(plan)
    || !Array.isArray(plan.segments) || !plan.segments.length
    || !Array.isArray(plan.days) || !plan.days.length
    || !plan.segments.every((segment) => segment && typeof segment.destination_id === "string")
    || !plan.days.every((day) => day && Array.isArray(day.activities))
  ) {
    throw new Error("지원하지 않는 plan schema입니다.");
  }
  return plan;
}

function safeGoogleMapsUrl(value) {
  try {
    const parsed = new URL(String(value));
    if (
      parsed.protocol !== "https:"
      || parsed.hostname !== "www.google.com"
      || parsed.port
      || /^https:\/\/www\.google\.com:443(?:[/?#]|$)/i.test(String(value).trim())
      || parsed.username
      || parsed.password
      || !parsed.pathname.startsWith("/maps/")
    ) return "";
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
  const destination = catalog.destinations[id];
  if (!destination) throw new Error(`알 수 없는 목적지입니다: ${id}`);
  return destination;
}

function validatePlanForViewer(plan) {
  const knownDestination = (id) => typeof id === "string" && Boolean(catalog?.destinations?.[id]);
  if (
    !plan || !hasValidPlanIdentity(plan)
    || !Array.isArray(plan.segments) || !plan.segments.length
    || !plan.segments.every((segment) => knownDestination(segment?.destination_id))
    || !Array.isArray(plan.days) || !plan.days.length
    || !plan.days.every((day) => Array.isArray(day?.activities) && day.activities.every((activity) => (
      activity && knownDestination(activity.destination_id)
      && ["time", "title", "location"].every((key) => typeof activity[key] === "string")
    )))
  ) throw new Error("content token의 도시 또는 일정 구조가 올바르지 않습니다.");
}

function uniqueDestinationIds(plan) {
  return [...new Set(plan.segments.map((segment) => segment.destination_id))];
}

function directionsUrl(origin, destination, travelmode = "transit") {
  const params = new URLSearchParams({ api: "1", origin, destination, travelmode });
  return `https://www.google.com/maps/dir/?${params}`;
}

function mapsSearchUrl(query) {
  const params = new URLSearchParams({ api: "1", query });
  return `https://www.google.com/maps/search/?${params}`;
}

function dayRouteUrl(activities) {
  const queries = (activities || []).map(routeQuery).filter(Boolean);
  if (!queries.length) return "";
  if (queries.length === 1) return mapsSearchUrl(queries[0]);
  const params = new URLSearchParams({
    api: "1",
    origin: queries[0],
    destination: queries.at(-1),
    travelmode: "transit",
  });
  if (queries.length > 2) params.set("waypoints", queries.slice(1, -1).join("|"));
  return `https://www.google.com/maps/dir/?${params}`;
}

function activityMapUrl(activity) {
  return safeGoogleMapsUrl(activity?.map_url) || mapsSearchUrl(routeQuery(activity));
}

function routeQuery(activity) {
  return String(activity.map_query || activity.location || activity.title || "").trim();
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
  root.style.setProperty("--hero-image", `url("/viewer/${destination.heroImage}")`);
  root.dataset.activeDestination = destination.id;
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
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
    detail: "1 현지 통화 단위 기준 · 일정 생성 시 조회값",
  };
}

function renderCover(plan, firstDestination) {
  const cover = element("section", "cover");
  const copy = element("div", "cover-copy");
  const upper = element("div");
  const summary = element("p", "cover-summary", firstDestination.summary);
  summary.dataset.activeCitySummary = "";
  upper.append(
    element("p", "kicker", `PLAN ${plan.plan_id.toUpperCase()} · REV ${plan.revision}`),
    element("h1", "", plan.title),
    summary,
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
  cover.append(copy);
  return cover;
}

function renderSegments(plan) {
  const board = element("nav", "segment-board");
  board.setAttribute("aria-label", "여정 도시 선택");
  plan.segments.forEach((segment, index) => {
    const destination = destinationFor(segment.destination_id);
    const row = element("div", "segment-row");
    const city = element("div", "segment-city");
    const button = element("button", "", segment.city_ko);
    button.type = "button";
    button.dataset.destinationId = segment.destination_id;
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", `${segment.country_ko} ${segment.city_ko} 여행 도구와 배경 보기`);
    button.addEventListener("click", () => setActiveDestination(plan, button.dataset.destinationId));
    city.append(button, element("span", "", `${segment.country_ko} · ${segment.city}`));
    row.append(city);
    cityButtons.push({ id: segment.destination_id, button });
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

function formatClock(timeZone, mode) {
  const options = mode === "time"
    ? { timeZone, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
    : { timeZone, month: "short", day: "numeric", weekday: "short" };
  return new Intl.DateTimeFormat("ko-KR", options).format(new Date()).replace("24:", "00:");
}

function updateClocks(root = document) {
  root.querySelectorAll("[data-clock-zone]").forEach((node) => {
    const zone = node.dataset.clockZone;
    const time = node.querySelector("[data-clock-time]");
    const date = node.querySelector("[data-clock-date]");
    if (time) time.textContent = formatClock(zone, "time");
    if (date) date.textContent = formatClock(zone, "date");
  });
}

function clockRow(label, timeZone) {
  const row = element("div", "clock-row");
  row.dataset.clockZone = timeZone;
  const copy = element("div");
  copy.append(element("strong", "", label), element("span", "clock-zone", timeZone));
  const value = element("div", "clock-value");
  const time = element("time", "", "--:--:--");
  time.dataset.clockTime = "";
  const date = element("span", "", "—");
  date.dataset.clockDate = "";
  value.append(time, date);
  row.append(copy, value);
  return row;
}

function initialRate(plan, currency) {
  return (plan.live_data?.exchange?.rates || []).find(
    (rate) => rate.currency === currency && Number.isFinite(rate.krw_per_unit),
  );
}

function setExchangeRate(panel, destination, rate, meta = {}) {
  const numericRate = Number(rate);
  const localInput = panel.querySelector("[data-local-amount]");
  const krwInput = panel.querySelector("[data-krw-amount]");
  const rateText = panel.querySelector("[data-rate-text]");
  const metaText = panel.querySelector("[data-rate-meta]");
  if (!Number.isFinite(numericRate) || numericRate <= 0) {
    panel.dataset.exchangeState = meta.state || "unavailable";
    delete panel.dataset.rate;
    rateText.textContent = "환율 조회 불가";
    metaText.textContent = meta.message || "정상 snapshot이 없어 계산기를 사용할 수 없습니다.";
    localInput.disabled = true;
    krwInput.disabled = true;
    return;
  }
  panel.dataset.exchangeState = meta.state || "snapshot";
  panel.dataset.rate = String(numericRate);
  localInput.disabled = false;
  krwInput.disabled = false;
  rateText.textContent = `1 ${destination.currency.code} = ${numericRate.toLocaleString("ko-KR", { maximumFractionDigits: 4 })} KRW`;
  const fetched = meta.fetched_at ? new Date(meta.fetched_at).toLocaleString("ko-KR") : "일정 생성 시점";
  metaText.textContent = `${fetched} 조회 · ${meta.source || "open.er-api.com"}`;
  localInput.dispatchEvent(new Event("input"));
}

function renderExchangePanel(plan, destination) {
  const panel = element("section", "guide-panel exchange-panel");
  panel.append(element("span", "panel-label", "LIVE EXCHANGE / KRW"));
  const rateText = element("strong", "rate-text", "환율 확인 중");
  rateText.dataset.rateText = "";
  const metaText = element("p", "rate-meta", "저장된 조회값을 확인합니다.");
  metaText.dataset.rateMeta = "";
  metaText.setAttribute("role", "status");
  metaText.setAttribute("aria-live", "polite");
  const fields = element("div", "exchange-fields");
  const localLabel = document.createElement("label");
  localLabel.append(element("span", "", destination.currency.code));
  const localInput = document.createElement("input");
  localInput.type = "number";
  localInput.inputMode = "decimal";
  localInput.min = "0";
  localInput.value = "100";
  localInput.dataset.localAmount = "";
  localLabel.append(localInput);
  const krwLabel = document.createElement("label");
  krwLabel.append(element("span", "", "KRW"));
  const krwInput = document.createElement("input");
  krwInput.type = "number";
  krwInput.inputMode = "numeric";
  krwInput.min = "0";
  krwInput.dataset.krwAmount = "";
  krwLabel.append(krwInput);
  fields.append(localLabel, krwLabel);
  localInput.addEventListener("input", () => {
    const rate = Number(panel.dataset.rate);
    const amount = Number(localInput.value);
    if (Number.isFinite(rate) && Number.isFinite(amount)) krwInput.value = String(Math.round(amount * rate));
  });
  krwInput.addEventListener("input", () => {
    const rate = Number(panel.dataset.rate);
    const amount = Number(krwInput.value);
    if (Number.isFinite(rate) && rate > 0 && Number.isFinite(amount)) localInput.value = String(Math.round((amount / rate) * 100) / 100);
  });
  panel.append(rateText, metaText, fields);
  const saved = initialRate(plan, destination.currency.code);
  setExchangeRate(panel, destination, saved?.krw_per_unit, saved || {});
  return panel;
}

function renderPhrasePanel(destination) {
  const panel = element("section", "guide-panel phrase-panel");
  const head = element("div", "phrase-head");
  head.append(element("span", "panel-label", `BASIC PHRASES / ${destination.phraseLabel || "LOCAL"}`));
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "한국어 의미·현지어·발음 검색";
  search.setAttribute("aria-label", `${destination.cityKo} 기본 회화 검색`);
  head.append(search);
  const list = element("div", "phrase-list");
  const renderPhrases = () => {
    const query = search.value.trim().toLocaleLowerCase("ko-KR");
    const phrases = (destination.phrases || []).filter((phrase) =>
      !query || [phrase.text, phrase.pron, phrase.meaning].some((value) => String(value).toLocaleLowerCase("ko-KR").includes(query)),
    );
    list.replaceChildren();
    phrases.forEach((phrase) => {
      const row = element("div", "phrase-row");
      const local = element("div");
      local.append(element("strong", "", phrase.text), element("span", "", phrase.pron));
      row.append(local, element("p", "", phrase.meaning));
      list.append(row);
    });
    if (!phrases.length) list.append(element("p", "phrase-empty", "일치하는 기본 회화가 없습니다."));
  };
  search.addEventListener("input", renderPhrases);
  renderPhrases();
  panel.append(head, list);
  return panel;
}

async function refreshCityGuide(destinationId, panel) {
  guideRequest?.abort();
  guideRequest = new AbortController();
  const destination = destinationFor(destinationId);
  const setFailure = (state, detail) => {
    const hasSnapshot = Number(panel.dataset.rate) > 0;
    panel.dataset.exchangeState = state;
    const meta = panel.querySelector("[data-rate-meta]");
    if (hasSnapshot) {
      if (meta) meta.textContent = `${detail} 마지막 정상 snapshot을 유지합니다.`;
      return;
    }
    setExchangeRate(panel, destination, undefined, {
      state,
      message: `${detail} 정상 snapshot이 없어 계산기를 비활성화했습니다.`,
    });
  };
  if (!navigator.onLine) {
    setFailure("offline", "오프라인입니다.");
    return;
  }
  panel.dataset.exchangeState = "loading";
  const meta = panel.querySelector("[data-rate-meta]");
  if (meta) meta.textContent = "환율을 갱신하는 중입니다…";
  try {
    const response = await fetch(`/api/city-guide/${encodeURIComponent(destinationId)}`, { cache: "no-store", signal: guideRequest.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.message || "city guide unavailable");
    const exchange = payload.exchange || {};
    const rate = Number.isFinite(exchange.krw_per_unit)
      ? exchange
      : (exchange.rates || []).find((item) => item.currency === destination.currency.code);
    if (!Number.isFinite(rate?.krw_per_unit)) throw new Error(exchange.message || "환율 값이 없습니다.");
    if (destinationId === activeDestinationId) {
      setExchangeRate(panel, destination, rate.krw_per_unit, { ...rate, state: "live" });
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    setFailure(navigator.onLine ? "error" : "offline", `${navigator.onLine ? "환율 갱신에 실패했습니다." : "오프라인입니다."} (${error.message})`);
  }
}

function renderGuideContent(plan, destinationId, content) {
  const destination = destinationFor(destinationId);
  content.dataset.destinationId = destinationId;
  const clockPanel = element("section", "guide-panel clock-panel");
  clockPanel.append(
    element("span", "panel-label", "LIVE CLOCKS"),
    clockRow("한국 · 서울", "Asia/Seoul"),
    clockRow(`${destination.countryKo} · ${destination.cityKo}`, destination.timeZone),
  );
  const exchangePanel = renderExchangePanel(plan, destination);
  const phrasePanel = renderPhrasePanel(destination);
  content.replaceChildren(clockPanel, exchangePanel, phrasePanel);
  updateClocks(clockPanel);
  window.clearInterval(clockTimer);
  window.clearInterval(liveRefreshTimer);
  clockTimer = window.setInterval(updateClocks, 1000);
  refreshCityGuide(destinationId, exchangePanel);
  liveRefreshTimer = window.setInterval(() => refreshCityGuide(destinationId, exchangePanel), LIVE_REFRESH_MS);
}

function cityMapSelection(plan, destinationId) {
  const activity = plan.days
    .flatMap((day) => day.activities)
    .find((item) => item.destination_id === destinationId);
  if (activity) return activity;
  const destination = destinationFor(destinationId);
  const query = `${destination.city}, ${destination.country}`;
  return {
    destination_id: destinationId,
    title: `${destination.cityKo} 도시 지도`,
    location: `${destination.countryKo} · ${destination.city}`,
    map_url: mapsSearchUrl(query),
  };
}

function setActiveDestination(plan, destinationId) {
  activeDestinationId = destinationId;
  const destination = destinationFor(destinationId);
  setTheme(destination);
  const summary = document.querySelector("[data-active-city-summary]");
  if (summary) summary.textContent = destination.summary;
  cityButtons.forEach(({ id, button }) => button.setAttribute("aria-pressed", String(id === destinationId)));
  if (guideContent) renderGuideContent(plan, destinationId, guideContent);
  showMapPreview(cityMapSelection(plan, destinationId), false);
}

function renderTravelDesk(plan) {
  const desk = element("section", "travel-desk");
  const header = element("header", "desk-head");
  header.append(element("h2", "", "현지 필드노트"), element("span", "", "선택한 도시의 시각, 환율, 회화를 한곳에서 확인하세요."));
  const content = element("div", "guide-grid");
  desk.append(header, content);
  guideContent = content;
  if (!activeDestinationId) setActiveDestination(plan, uniqueDestinationIds(plan)[0]);
  return desk;
}

function showMapPreview(activity, shouldScroll = true) {
  const desk = document.getElementById("map-desk");
  const title = document.querySelector("[data-map-title]");
  const locationText = document.querySelector("[data-map-location]");
  const open = document.querySelector("[data-map-open]");
  if (!title || !locationText || !open) return;
  if (desk) desk.dataset.destinationId = activity.destination_id || activeDestinationId;
  title.textContent = activity.title;
  locationText.textContent = activity.location || routeQuery(activity);
  const url = activityMapUrl(activity);
  if (url) {
    open.href = url;
    open.removeAttribute("aria-disabled");
  } else {
    open.removeAttribute("href");
    open.setAttribute("aria-disabled", "true");
  }
  if (shouldScroll) document.getElementById("map-desk")?.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
}

function renderMapDesk(plan) {
  const destinationId = activeDestinationId || uniqueDestinationIds(plan)[0];
  const first = cityMapSelection(plan, destinationId);
  const desk = element("section", "map-desk");
  desk.id = "map-desk";
  desk.dataset.destinationId = destinationId;
  const copy = element("div");
  copy.append(element("span", "panel-label", "LIVE MAP"));
  const title = element("h2", "", first?.title || "장소를 선택하세요");
  title.dataset.mapTitle = "";
  const locationText = element("p", "", first?.location || "일정의 VIEW 버튼을 누르면 이곳에 장소가 표시됩니다.");
  locationText.dataset.mapLocation = "";
  copy.append(title, locationText);
  const open = element("a", "map-open", "GOOGLE 지도에서 실시간 보기");
  open.dataset.mapOpen = "";
  open.target = "_blank";
  open.rel = "noreferrer";
  const firstUrl = activityMapUrl(first);
  if (firstUrl) {
    open.href = firstUrl;
  } else {
    open.setAttribute("aria-disabled", "true");
  }
  const note = element("p", "map-note", "실제 지도·교통 상황·거리·소요시간은 링크를 여는 시점의 Google Maps 결과를 사용합니다.");
  desk.append(copy, open, note);
  return desk;
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
  const actions = element("div", "stop-actions");
  const preview = element("button", "map-preview", "VIEW");
  preview.type = "button";
  preview.addEventListener("click", () => showMapPreview(activity));
  const mapUrl = activityMapUrl(activity);
  const map = element(mapUrl ? "a" : "span", "map-link", mapUrl ? "MAP" : "NO MAP");
  if (mapUrl) {
    map.href = mapUrl;
    map.target = "_blank";
    map.rel = "noreferrer";
    map.setAttribute("aria-label", `${activity.title} 지도 열기`);
  } else {
    map.setAttribute("aria-disabled", "true");
  }
  actions.append(preview, map);
  item.append(time, marker, copy, actions);
  return item;
}

function renderLeg(fromActivity, toActivity, leg = {}) {
  const from = routeQuery(fromActivity);
  const to = routeQuery(toActivity);
  const item = element("li", "travel-leg");
  const label = element("span", "leg-label", `${fromActivity.title} → ${toActivity.title}`);
  const links = element("div", "leg-links");
  const options = [
    ["TRANSIT", leg.transit_url || leg.route_urls?.transit || leg.routes?.transit || directionsUrl(from, to, "transit")],
    ["WALK", leg.walking_url || leg.route_urls?.walking || leg.routes?.walking || directionsUrl(from, to, "walking")],
    ["DRIVE", leg.driving_url || leg.route_urls?.driving || leg.routes?.driving || directionsUrl(from, to, "driving")],
  ];
  options.forEach(([name, rawUrl]) => {
    const url = safeGoogleMapsUrl(rawUrl) || directionsUrl(from, to, name === "WALK" ? "walking" : name === "DRIVE" ? "driving" : "transit");
    const link = element(url ? "a" : "span", "", name);
    if (url) {
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer";
    }
    links.append(link);
  });
  item.append(label, links);
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
    day.activities.forEach((activity, index) => {
      stops.append(renderActivity(activity));
      const next = day.activities[index + 1];
      if (next) stops.append(renderLeg(activity, next, (day.legs || day.route_legs || [])[index]));
    });
    const routeUrl = safeGoogleMapsUrl(day.route_map_url) || dayRouteUrl(day.activities);
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

function renderPlan(plan, { demo = false } = {}) {
  window.clearInterval(clockTimer);
  window.clearInterval(liveRefreshTimer);
  guideRequest?.abort();
  cityButtons = [];
  guideContent = undefined;
  activeDestinationId = "";
  validatePlanForViewer(plan);
  currentPlan = plan;
  const firstDestination = destinationFor(plan.segments[0].destination_id);
  setTheme(firstDestination);
  document.title = `${plan.title} — Route / 69`;
  app.replaceChildren();
  app.setAttribute("aria-busy", "false");
  app.append(renderCover(plan, firstDestination), renderSegments(plan), renderStatus(plan), renderTravelDesk(plan), renderMapDesk(plan));
  if (plan.shorter_variant) {
    const note = element("aside", "variant-note");
    note.append(element("b", "", "SHORTER OPTION"), document.createTextNode(plan.shorter_variant.hint));
    app.append(note);
  }
  app.append(renderItinerary(plan));
  showNotice(demo ? "FICTIONAL DEMO · MCP content token을 열면 이 자리에 실제 일정이 표시됩니다." : "");
}

async function bootstrap() {
  try {
    const response = await fetch("/data/destinations.json");
    if (!response.ok) throw new Error("canonical catalog를 불러오지 못했습니다.");
    catalog = await response.json();
    if (
      catalog?.destinationCount !== 69
      || !catalog.destinations
      || Object.keys(catalog.destinations).length !== 69
    ) throw new Error("69개 canonical catalog를 확인할 수 없습니다.");
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
    copyButton.textContent = "복사됨";
    window.setTimeout(() => { copyButton.textContent = "링크 복사"; }, 1400);
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
    window.scrollTo({ top: 0, behavior: reducedMotion() ? "auto" : "smooth" });
  } catch (error) {
    tokenError.textContent = error.message;
  }
});

window.addEventListener("offline", () => {
  const panel = document.querySelector(".exchange-panel");
  if (!panel) return;
  const destination = activeDestinationId && destinationFor(activeDestinationId);
  if (!destination) return;
  if (Number(panel.dataset.rate) > 0) {
    panel.dataset.exchangeState = "offline";
    panel.querySelector("[data-rate-meta]").textContent = "오프라인입니다. 마지막 정상 snapshot을 유지합니다.";
  } else {
    setExchangeRate(panel, destination, undefined, {
      state: "offline",
      message: "오프라인이며 정상 snapshot이 없어 계산기를 사용할 수 없습니다.",
    });
  }
});

window.addEventListener("online", () => {
  const panel = document.querySelector(".exchange-panel");
  if (panel && activeDestinationId) refreshCityGuide(activeDestinationId, panel);
});

window.addEventListener("hashchange", bootstrap);
bootstrap();
