import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { deflateSync } from "node:zlib";

import { expect, test } from "@playwright/test";


const TEST_DIR = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(TEST_DIR, "../..");
const PLAN_PATH = resolve(TEST_DIR, "fixture-plan.json");
const FIXTURE_PLAN = JSON.parse(readFileSync(PLAN_PATH, "utf8"));
const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".jpg": "image/jpeg",
  ".woff2": "font/woff2",
};

let server;
let baseUrl;
let exportPath;
let tempDir;

function contentToken(plan) {
  return `tp1.${deflateSync(Buffer.from(JSON.stringify(plan))).toString("base64url")}`;
}

function mockGuide(destinationId) {
  const rate = {
    currency: "EUR",
    krw_per_unit: destinationId === "austria" ? 1601 : 1600,
    status: "live",
    source: "local-browser-mock",
    fetched_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
  };
  return {
    ok: true,
    destination_id: destinationId,
    exchange: { ...rate, rates: [rate] },
  };
}

function send(response, status, body, contentType) {
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Type": contentType,
  });
  response.end(body);
}

async function startFixtureServer() {
  server = createServer((request, response) => {
    try {
      const url = new URL(request.url || "/", "http://127.0.0.1");
      const pathname = decodeURIComponent(url.pathname);
      if (pathname.startsWith("/api/city-guide/")) {
        const destinationId = pathname.slice("/api/city-guide/".length);
        send(response, 200, JSON.stringify(mockGuide(destinationId)), MIME_TYPES[".json"]);
        return;
      }
      if (pathname === "/export") {
        send(response, 200, readFileSync(exportPath), MIME_TYPES[".html"]);
        return;
      }

      const routes = {
        "/viewer": "viewer/index.html",
        "/data/destinations.json": "data/destinations.json",
        "/examples/demo-plan.json": "examples/demo-plan.json",
      };
      const relative = routes[pathname] || (pathname.startsWith("/viewer/") ? pathname.slice(1) : "");
      const target = relative ? resolve(ROOT, relative) : "";
      if (!target || !(target === ROOT || target.startsWith(`${ROOT}/`))) {
        send(response, 404, "not found", "text/plain; charset=utf-8");
        return;
      }
      const body = readFileSync(target);
      send(response, 200, body, MIME_TYPES[extname(target)] || "application/octet-stream");
    } catch (error) {
      send(response, error?.code === "ENOENT" ? 404 : 500, "not found", "text/plain; charset=utf-8");
    }
  });
  await new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
}

async function stopFixtureServer() {
  if (server) await new Promise((resolveClose) => server.close(resolveClose));
}

async function localRequestsOnly(context) {
  const blockedRequests = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const localHttp = ["http:", "https:"].includes(url.protocol)
      && ["127.0.0.1", "localhost"].includes(url.hostname);
    if (localHttp || ["file:", "data:", "blob:", "about:"].includes(url.protocol)) {
      await route.continue();
      return;
    }
    blockedRequests.push(url.href);
    await route.abort("blockedbyclient");
  });
  return blockedRequests;
}

function collectRuntimeErrors(page) {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  return errors;
}

async function openViewer(page, plan = FIXTURE_PLAN) {
  await page.goto(`${baseUrl}/viewer#plan=${contentToken(plan)}`, { waitUntil: "networkidle" });
  await expect(page.locator("#app")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator(".exchange-panel")).toHaveAttribute("data-exchange-state", "live");
}

async function expectLoadedGmarket(page, family) {
  const result = await page.evaluate(async (expectedFamily) => {
    await Promise.all([300, 500, 700].map((weight) => document.fonts.load(`${weight} 16px "${expectedFamily}"`, "파리 빈 서울")));
    await document.fonts.ready;
    const faces = [...document.fonts]
      .filter((face) => face.family.replace(/["']/g, "") === expectedFamily)
      .map((face) => ({ status: face.status, weight: face.weight }));
    const computed = getComputedStyle(document.body).fontFamily.replace(/["']/g, "").split(",")[0].trim();
    const checks = [300, 500, 700].map((weight) => document.fonts.check(`${weight} 16px "${expectedFamily}"`, "파리 빈 서울"));
    return { checks, computed, faces };
  }, family);
  expect(result.computed).toBe(family);
  expect(result.checks).toEqual([true, true, true]);
  expect(result.faces).toEqual(expect.arrayContaining([
    expect.objectContaining({ status: "loaded", weight: "300" }),
    expect.objectContaining({ status: "loaded", weight: "500" }),
    expect.objectContaining({ status: "loaded", weight: "700" }),
  ]));
}

async function expectLiveClocks(page, selector) {
  await expect.poll(async () => {
    const values = await page.locator(selector).evaluateAll((nodes) => nodes.map((node) => node.textContent.trim()));
    return values.length >= 2 && values.every((value) => /\d{2}/.test(value) && !value.includes("--") && !value.includes("계산 중"));
  }).toBe(true);
}

async function expectClockTick(page, selector) {
  const initial = await page.locator(selector).evaluateAll((nodes) => nodes.map((node) => node.getAttribute("datetime") || node.textContent.trim()));
  await expect.poll(async () => page.locator(selector).evaluateAll((nodes) => nodes.map((node) => node.getAttribute("datetime") || node.textContent.trim())), {
    timeout: 3_000,
    intervals: [1_100, 500],
  }).not.toEqual(initial);
}

async function expectNoHorizontalOverflow(page) {
  const result = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    innerWidth: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(result.scrollWidth, JSON.stringify(result)).toBeLessThanOrEqual(result.clientWidth + 1);
  expect(result.clientWidth).toBe(result.innerWidth);
}

async function expectActionHeights(page, selector) {
  const actions = await page.locator(selector).evaluateAll((nodes) => nodes
    .filter((node) => {
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    })
    .map((node) => ({
      height: node.getBoundingClientRect().height,
      label: node.getAttribute("aria-label") || node.textContent.trim() || node.getAttribute("placeholder") || node.tagName,
    })));
  expect(actions.length).toBeGreaterThan(0);
  expect(actions.filter((action) => action.height < 43.5), JSON.stringify(actions, null, 2)).toEqual([]);
}

async function expectKeyboardFocusVisible(page) {
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Tab");
  const focus = await page.evaluate(() => {
    const active = document.activeElement;
    const style = getComputedStyle(active);
    return {
      tag: active?.tagName,
      outlineStyle: style.outlineStyle,
      outlineWidth: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(focus.tag).not.toBe("BODY");
  expect(focus.outlineStyle).not.toBe("none");
  expect(focus.outlineWidth).toBeGreaterThanOrEqual(2);
}

test.beforeAll(async () => {
  tempDir = mkdtempSync(resolve(tmpdir(), "travel-browser-gate-"));
  exportPath = resolve(tempDir, "travel-export.html");
  execFileSync(process.env.PYTHON || "python3", [resolve(TEST_DIR, "build_export_fixture.py"), PLAN_PATH, exportPath], {
    cwd: ROOT,
    stdio: "inherit",
  });
  await startFixtureServer();
});

test.afterAll(async () => {
  await stopFixtureServer();
  if (tempDir) rmSync(tempDir, { recursive: true, force: true });
});

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
});

test("viewer loads official Gmarket Sans and switches Paris/Vienna state without runtime errors", async ({ context, page }) => {
  const blockedRequests = await localRequestsOnly(context);
  const errors = collectRuntimeErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await openViewer(page);

  await expectLoadedGmarket(page, "Gmarket Sans");
  await expectLiveClocks(page, "[data-clock-time]");
  await expectClockTick(page, "[data-clock-time]");
  await expect(page.locator("html")).toHaveAttribute("data-active-destination", "paris");
  await expect(page.locator(".guide-grid")).toHaveAttribute("data-destination-id", "paris");
  await expect(page.locator("#map-desk")).toHaveAttribute("data-destination-id", "paris");
  let background = await page.evaluate(() => getComputedStyle(document.body, "::before").backgroundImage);
  expect(background).toContain("paris.jpg");
  expect(await page.evaluate(() => Number(getComputedStyle(document.body, "::before").zIndex))).toBeGreaterThanOrEqual(0);

  await page.getByRole("button", { name: "오스트리아 빈 여행 도구와 배경 보기" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-active-destination", "austria");
  await expect(page.locator(".guide-grid")).toHaveAttribute("data-destination-id", "austria");
  await expect(page.locator("#map-desk")).toHaveAttribute("data-destination-id", "austria");
  await expect(page.locator('[data-clock-zone="Europe/Vienna"]')).toHaveCount(1);
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.body, "::before").backgroundImage)).toContain("austria.jpg");

  await page.getByRole("button", { name: "프랑스 파리 여행 도구와 배경 보기" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-active-destination", "paris");
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  expect(await page.evaluate(() => getComputedStyle(document.body, "::before").transitionDuration)).toBe("0s");
  await expectKeyboardFocusVisible(page);
  expect(blockedRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test("viewer and standalone export stay within 390/320px and keep visible actions at least 44px high", async ({ context, page }) => {
  const blockedRequests = await localRequestsOnly(context);
  const errors = collectRuntimeErrors(page);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await openViewer(page);
    await expectNoHorizontalOverflow(page);
    await expectActionHeights(page, ".site-actions button, .segment-row button, .exchange-fields input, .phrase-head input, .map-open, .map-preview, .map-link, .leg-links a, .route-link");

    await page.goto(`${baseUrl}/export`, { waitUntil: "networkidle" });
    await expect(page.locator("main")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectActionHeights(page, ".city-selector button, .fx-calculator input, .city-map-link, .stop a, .leg a, .route");
  }

  await page.setViewportSize({ width: 320, height: 844 });
  const longTitlePlan = structuredClone(FIXTURE_PLAN);
  longTitlePlan.title = "초장문여행제목".repeat(40);
  await openViewer(page, longTitlePlan);
  await expect(page.locator(".cover h1")).toHaveText(longTitlePlan.title);
  await expectNoHorizontalOverflow(page);

  expect(blockedRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test("standalone export loads embedded Gmarket assets, clocks, and city-specific background/content", async ({ context, page }) => {
  const blockedRequests = await localRequestsOnly(context);
  const errors = collectRuntimeErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseUrl}/export`, { waitUntil: "networkidle" });

  await expectLoadedGmarket(page, "Gmarket");
  await expectLiveClocks(page, "[data-live-clock]");
  await expectClockTick(page, "[data-live-clock]");
  await expect(page.locator(".destination-card.active")).toHaveAttribute("data-city-id", "paris");
  const parisBackground = await page.evaluate(() => getComputedStyle(document.body, "::before").backgroundImage);
  expect(parisBackground).toContain("data:image/jpeg;base64");
  expect(await page.evaluate(() => Number(getComputedStyle(document.body, "::before").zIndex))).toBeGreaterThanOrEqual(0);

  await page.getByRole("button", { name: "빈", exact: true }).click();
  await expect(page.locator(".destination-card.active")).toHaveAttribute("data-city-id", "austria");
  await expect(page.locator(".phrase-city.active")).toHaveAttribute("data-city-id", "austria");
  await expect(page.locator(".clock-row.active")).toHaveAttribute("data-city-id", "austria");
  await expect(page.locator(".fx-row.active")).toHaveAttribute("data-city-id", "austria");
  const viennaBackground = await page.evaluate(() => getComputedStyle(document.body, "::before").backgroundImage);
  expect(viennaBackground).not.toBe(parisBackground);
  await expectKeyboardFocusVisible(page);
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  expect(blockedRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test("offline mode is explicit and file export retains its saved FX snapshot", async ({ context, page }) => {
  const blockedRequests = await localRequestsOnly(context);
  const errors = collectRuntimeErrors(page);
  await openViewer(page);
  await context.setOffline(true);
  await expect(page.locator(".exchange-panel")).toHaveAttribute("data-exchange-state", "offline");
  await expect(page.locator("[data-rate-meta]")).toContainText("오프라인");
  await context.setOffline(false);

  await page.goto(`${baseUrl}/export`, { waitUntil: "networkidle" });
  await expect(page.locator("[data-fx-refresh-status]")).toHaveAttribute("data-state", "live");
  await context.setOffline(true);
  await expect(page.locator("[data-fx-refresh-status]")).toHaveAttribute("data-state", "offline");
  await expect(page.locator("[data-fx-refresh-status]")).toContainText("오프라인");
  await context.setOffline(false);

  await page.goto(pathToFileURL(exportPath).href, { waitUntil: "load" });
  await expect(page.locator("[data-fx-refresh-status]")).toHaveAttribute("data-state", "offline");
  await expect(page.locator("[data-fx-refresh-status]")).toContainText("독립 HTML(file://)");
  await expect(page.locator("[data-fx-refresh-status]")).toContainText("snapshot");
  await expectLiveClocks(page, "[data-live-clock]");
  expect(blockedRequests).toEqual([]);
  expect(errors).toEqual([]);
});

test("malicious content token remains inert text", async ({ context, page }) => {
  const blockedRequests = await localRequestsOnly(context);
  const errors = collectRuntimeErrors(page);
  const malicious = structuredClone(FIXTURE_PLAN);
  malicious.title = '</h1><script data-browser-injection>window.__relayPwned = true</script><h1>';
  malicious.days[0].activities[0].title = '<img src=x onerror="window.__relayPwned=true">';
  await openViewer(page, malicious);

  await expect(page.locator(".cover h1")).toHaveText(malicious.title);
  expect(await page.locator("[data-browser-injection]").count()).toBe(0);
  expect(await page.locator(".stop-copy img").count()).toBe(0);
  expect(await page.evaluate(() => window.__relayPwned)).toBeUndefined();
  expect(blockedRequests).toEqual([]);
  expect(errors).toEqual([]);
});
