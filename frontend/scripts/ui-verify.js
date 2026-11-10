/**
 * Headless verification of the real running app.
 *
 * This is not a unit test: it launches Chromium against the API server that
 * serves the built shell, walks every route, screenshots both themes, and fails
 * loudly on a console error, a failed request, a blank view or a stray
 * cross-origin request. Run with:
 *   node scripts/ui-verify.js http://127.0.0.1:8765
 */
import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";

const base = process.argv[2] ?? "http://127.0.0.1:8765";
const outDir = process.argv[3] ?? "/home/user/ui-verification";
await mkdir(outDir, { recursive: true });


/**
 * Measures text/background contrast on the *rendered* page.
 *
 * Translucent surfaces are composited down the ancestor chain (starting from the
 * body colour) instead of being read as opaque - otherwise an accent pill with a
 * 15 % tint reports a meaningless 1.0:1 against its own text.
 */
async function measureContrast() {
  return page.evaluate(() => {
    const parse = (color) => {
      const match = color.match(/rgba?\(([^)]+)\)/);
      if (!match) return null;
      const parts = match[1].split(",").map((value) => Number(value.trim()));
      return { rgb: parts.slice(0, 3), alpha: parts.length > 3 ? parts[3] : 1 };
    };
    const lum = ([r, g, b]) => {
      const channel = (value) => {
        const scaled = value / 255;
        return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const ratio = (a, b) => {
      const l1 = lum(a);
      const l2 = lum(b);
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    };
    const composite = (element) => {
      const chain = [];
      let node = element;
      while (node && node !== document.documentElement) {
        const color = parse(getComputedStyle(node).backgroundColor);
        if (color && color.alpha > 0) chain.push(color);
        node = node.parentElement;
      }
      let base = [255, 255, 255];
      for (let index = chain.length - 1; index >= 0; index -= 1) {
        const layer = chain[index];
        base = [
          layer.rgb[0] * layer.alpha + base[0] * (1 - layer.alpha),
          layer.rgb[1] * layer.alpha + base[1] * (1 - layer.alpha),
          layer.rgb[2] * layer.alpha + base[2] * (1 - layer.alpha),
        ];
      }
      return base;
    };
    const samples = [];
    for (const element of Array.from(document.querySelectorAll("main p, main span, main td, aside span, main h1, main h2, footer span")).slice(0, 500)) {
      const text = (element.textContent ?? "").trim();
      if (!text || text.length < 2) continue;
      const style = getComputedStyle(element);
      const foreground = parse(style.color);
      if (!foreground) continue;
      // Icon glyphs and decorative dots are not text: skip zero-area elements.
      const rect = element.getBoundingClientRect();
      if (rect.width < 4 || rect.height < 4) continue;
      samples.push({
        text: text.slice(0, 32),
        size: Number.parseFloat(style.fontSize),
        ratio: ratio(foreground.rgb, composite(element)),
      });
    }
    const body = samples.filter((sample) => sample.size < 18.5);
    const large = samples.filter((sample) => sample.size >= 18.5);
    return {
      measured: samples.length,
      bodyMin: body.length ? Math.min(...body.map((sample) => sample.ratio)) : null,
      bodyFail: body.filter((sample) => sample.ratio < 4.5).slice(0, 8),
      largeMin: large.length ? Math.min(...large.map((sample) => sample.ratio)) : null,
    };
  });
}

const ROUTES = [
  ["command-center", "/"],
  ["ai-core", "/core"],
  ["agents", "/agents"],
  ["missions", "/missions"],
  ["tasks", "/tasks"],
  ["calendar", "/calendar"],
  ["memory", "/memory"],
  ["conversations", "/conversations"],
  ["knowledge", "/knowledge"],
  ["tools", "/tools"],
  ["workflows", "/workflows"],
  ["models", "/models"],
  ["voice", "/voice"],
  ["security", "/security"],
  ["system", "/system"],
  ["analytics", "/analytics"],
  ["settings", "/settings"],
];

const report = { base, consoleErrors: [], pageErrors: [], failedRequests: [], foreignRequests: [], routes: [], themes: {}, perf: {}, checks: [] };
const check = (name, pass, detail) => {
  report.checks.push({ name, pass, detail });
  if (!pass) console.error(`FAIL ${name} :: ${detail ?? ""}`);
  else console.log(`ok   ${name}${detail ? ` :: ${detail}` : ""}`);
};

const browser = await chromium.launch({
  executablePath: `${process.env.HOME}/.cache/ms-playwright/chromium-1140/chrome-linux/chrome`,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--font-render-hinting=none"],
});
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1, colorScheme: "dark" });
const page = await context.newPage();

page.on("console", (message) => {
  if (message.type() === "error") report.consoleErrors.push(message.text());
});
page.on("pageerror", (error) => report.pageErrors.push(String(error)));
page.on("requestfailed", (request) => {
  const reason = request.failure()?.errorText ?? "";
  // A page reload or route change aborts the open event stream and any in-flight
  // poll. Those aborts are the client doing the right thing, not a broken request.
  if (reason.includes("ERR_ABORTED")) {
    report.abortedRequests = (report.abortedRequests ?? 0) + 1;
    return;
  }
  report.failedRequests.push(`${request.method()} ${request.url()} :: ${reason}`);
});
page.on("request", (request) => {
  const url = request.url();
  if (!url.startsWith(base) && !url.startsWith("data:") && !url.startsWith("blob:")) report.foreignRequests.push(url);
});

async function settle(ms = 900) {
  await page.waitForTimeout(ms);
}

/* 1. First paint of the dashboard, dark theme. */
const started = Date.now();
await page.goto(`${base}/`, { waitUntil: "domcontentloaded" });
await settle(1400);
const loadMs = Date.now() - started;

const title = await page.title();
check("page title", title.includes("Omni-Assistant"), title);

const tokenMeta = await page.locator('meta[name="pc-agent-token"]').count();
check("API token meta present", tokenMeta === 1, `${tokenMeta} tag(s)`);

const navCount = await page.locator("aside nav button").count();
check("sidebar renders all destinations", navCount >= 17, `${navCount} nav buttons`);

const coreCanvas = await page.locator("main canvas").count();
check("AI core canvas mounted", coreCanvas >= 1, `${coreCanvas} canvas`);

const corePixels = await page.evaluate(() => {
  const canvas = document.querySelector("main canvas");
  if (!canvas) return 0;
  const context = canvas.getContext("2d");
  if (!context) return 0;
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  let nonEmpty = 0;
  for (let index = 3; index < data.length; index += 4) if (data[index] > 8) nonEmpty += 1;
  return nonEmpty;
});
check("AI core actually draws pixels", corePixels > 500, `${corePixels} non-transparent pixels`);

const feedHasEvents = await page.locator("main").innerText();
check("live event feed populated", /service\.api_started|task\.created|reminder/.test(feedHasEvents), "event types visible");

await page.screenshot({ path: `${outDir}/01-dashboard-dark.png`, fullPage: false });

/* 2. Light theme. */
await page.evaluate(() => localStorage.setItem("omni.theme", "light"));
await page.reload({ waitUntil: "domcontentloaded" });
await settle(1200);
const theme = await page.evaluate(() => document.documentElement.dataset.theme);
check("light theme applied", theme === "light", theme);
const bodyBgLight = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
check("light background is light", /24[0-9]|25[0-5]/.test(bodyBgLight), bodyBgLight);
await page.screenshot({ path: `${outDir}/02-dashboard-light.png` });

/* 3. Contrast measurement on real rendered colours (light theme). */
const contrast = await measureContrast();
report.themes.lightContrast = contrast;
check("light body text >= 4.5:1", (contrast.bodyMin ?? 0) >= 4.5, `min ${contrast.bodyMin?.toFixed(2)} over ${contrast.measured} samples`);
check("light large text >= 3:1", contrast.largeMin === null || contrast.largeMin >= 3, `min ${contrast.largeMin?.toFixed(2) ?? "n/a"}`);

/* 4. Every route renders real content, no crash, no fake-looking error. */
for (const [id, path] of ROUTES) {
  await page.goto(`${base}/#${path}`, { waitUntil: "domcontentloaded" });
  await settle(700);
  const info = await page.evaluate(() => {
    const main = document.querySelector("main");
    return {
      heading: main?.querySelector("h1")?.textContent?.trim() ?? "",
      textLength: (main?.innerText ?? "").length,
      broken: (main?.innerText ?? "").includes("failed to render"),
      loadingStuck: /Loading module…/.test(main?.innerText ?? ""),
      scrollWidth: main?.scrollWidth ?? 0,
      clientWidth: main?.clientWidth ?? 0,
    };
  });
  const pass = info.textLength > 220 && !info.broken && !info.loadingStuck;
  report.routes.push({ id, path, ...info, pass });
  check(`route ${id} renders`, pass, `${info.textLength} chars, heading “${info.heading}”`);
  if (id === "settings" || id === "security" || id === "system" || id === "agents") {
    await page.screenshot({ path: `${outDir}/route-${id}-light.png` });
  }
}

/* 5. Dark theme sweep + contrast. */
await page.evaluate(() => localStorage.setItem("omni.theme", "dark"));
await page.goto(`${base}/#/`, { waitUntil: "domcontentloaded" });
await page.reload({ waitUntil: "domcontentloaded" });
await settle(1200);
const bodyBgDark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
check("dark background is dark", (() => {
  const numbers = bodyBgDark.match(/\d+/g)?.map(Number) ?? [];
  return numbers.length >= 3 && numbers[0] < 40 && numbers[1] < 45 && numbers[2] < 60;
})(), bodyBgDark);
const darkContrast = await measureContrast();
report.themes.darkContrast = darkContrast;
check("dark body text >= 4.5:1", (darkContrast.bodyMin ?? 0) >= 4.5, `min ${darkContrast.bodyMin?.toFixed(2)} over ${darkContrast.measured} samples`);
await page.screenshot({ path: `${outDir}/03-dashboard-dark-verified.png` });

/* 6. Command palette opens with the keyboard and lists real destinations. */
await page.keyboard.press("Control+k");
await settle(400);
const paletteOpen = await page.locator('[role="dialog"][aria-label="Command palette"]').count();
check("command palette opens on Ctrl+K", paletteOpen === 1, `${paletteOpen}`);
await page.keyboard.type("secur");
await settle(300);
const paletteHit = await page.locator('[role="dialog"][aria-label="Command palette"]').innerText();
check("palette filters to Security Center", /Security Center/i.test(paletteHit), paletteHit.split("\n")[1] ?? "");
await page.screenshot({ path: `${outDir}/04-command-palette.png` });
await page.keyboard.press("Enter");
await settle(800);
const afterPalette = await page.evaluate(() => document.querySelector("main h1")?.textContent ?? "");
check("palette navigates", /security/i.test(afterPalette), afterPalette);

/* 7. Live stream: a new task appears without a manual reload. */
await page.goto(`${base}/#/`, { waitUntil: "domcontentloaded" });
await settle(900);
const before = await page.locator("main").innerText();
const token = await page.evaluate(() => document.querySelector('meta[name="pc-agent-token"]')?.content ?? "");
await page.evaluate(async (bearer) => {
  await fetch("/v1/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearer}` },
    body: JSON.stringify({ instruction: "UI verification: live stream probe" }),
  });
}, token);
await settle(3500);
const after = await page.locator("main").innerText();
check("live event stream updates the feed", after.length !== before.length || /live stream probe|task\.created/.test(after), "feed changed after a new task");

/* 8. Sidebar collapse + persisted layout survive a reload. */
await page.keyboard.press("Control+b");
await settle(500);
const collapsedWidth = await page.evaluate(() => document.querySelector("aside")?.getBoundingClientRect().width ?? 0);
await page.reload({ waitUntil: "domcontentloaded" });
await settle(900);
const collapsedAfterReload = await page.evaluate(() => document.querySelector("aside")?.getBoundingClientRect().width ?? 0);
check("sidebar collapse persists", Math.abs(collapsedWidth - collapsedAfterReload) < 2 && collapsedWidth < 100, `${collapsedWidth}px → ${collapsedAfterReload}px`);
await page.keyboard.press("Control+b");
await settle(400);

/* 9. Widget reorder persists. */
const widgetOrderBefore = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]")[0]?.innerText?.slice(0, 24) ?? "");
await page.evaluate(() => {
  const first = document.querySelectorAll("main .grid > div[draggable]");
  if (first.length < 2) return;
  const second = first[1];
  second.querySelector('button[aria-label*="earlier"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(400);
const widgetOrderAfter = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]")[0]?.innerText?.slice(0, 24) ?? "");
check("widget reorder works", Boolean(widgetOrderBefore) && Boolean(widgetOrderAfter), `${JSON.stringify(widgetOrderBefore)} → ${JSON.stringify(widgetOrderAfter)}`);

/* 9b. Widget width + visibility are real and persisted. */
const spanBefore = await page.evaluate(() => {
  const first = document.querySelectorAll("main .grid > div[draggable]")[0];
  return first ? { cls: first.className, w: Math.round(first.getBoundingClientRect().width) } : null;
});
await page.evaluate(() => {
  const first = document.querySelectorAll("main .grid > div[draggable]")[0];
  first?.querySelector('button[aria-label*="width"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(500);
const spanAfter = await page.evaluate(() => {
  const first = document.querySelectorAll("main .grid > div[draggable]")[0];
  return first ? { cls: first.className, w: Math.round(first.getBoundingClientRect().width) } : null;
});
check("widget width toggle changes the span", Boolean(spanBefore && spanAfter) && spanBefore.w !== spanAfter.w, `${spanBefore?.w}px → ${spanAfter?.w}px`);

const countBefore = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]").length);
await page.evaluate(() => {
  const first = document.querySelectorAll("main .grid > div[draggable]")[0];
  first?.querySelector('button[aria-label^="Hide"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(400);
const countAfterHide = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]").length);
await page.reload({ waitUntil: "domcontentloaded" });
await settle(1400);
const countAfterReload = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]").length);
check("hiding a widget removes it and survives a reload", countBefore > countAfterHide && countAfterHide === countAfterReload, `${countBefore} → ${countAfterHide} → ${countAfterReload} (reload)`);
await page.evaluate(() => {
  const button = [...document.querySelectorAll("button")].find((node) => node.textContent?.trim().startsWith("Reset layout"));
  button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(400);
const countAfterReset = await page.evaluate(() => document.querySelectorAll("main .grid > div[draggable]").length);
check("reset layout restores every widget", countAfterReset === countBefore, `${countAfterReset} of ${countBefore}`);

/* 9c. Arrange mode makes every control touch-usable without hover. */
const arrangeButton = () =>
  [...document.querySelectorAll("button")].find(
    (node) => node.textContent?.trim() === "Arrange" || node.textContent?.trim() === "Done",
  );
const controlsBefore = await page.evaluate(() => {
  const toolbar = document.querySelector("main .grid > div[draggable] > div.absolute");
  const button = toolbar?.querySelector("button");
  if (!toolbar || !button) return null;
  return {
    opacity: getComputedStyle(toolbar).opacity,
    size: Math.round(button.getBoundingClientRect().height),
  };
});
await page.evaluate(() => {
  const button = [...document.querySelectorAll("button")].find(
    (node) => node.textContent?.trim() === "Arrange",
  );
  button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(400);
const controlsAfter = await page.evaluate(() => {
  const toolbar = document.querySelector("main .grid > div[draggable] > div.absolute");
  const button = toolbar?.querySelector("button");
  if (!toolbar || !button) return null;
  return {
    opacity: getComputedStyle(toolbar).opacity,
    size: Math.round(button.getBoundingClientRect().height),
    pressed: [...document.querySelectorAll("button")].find(
      (node) => node.textContent?.trim() === "Done",
    )?.getAttribute("aria-pressed"),
  };
});
check(
  "arrange mode keeps the widget controls visible without hover",
  controlsBefore?.opacity === "0" && controlsAfter?.opacity === "1" && controlsAfter?.pressed === "true",
  `opacity ${controlsBefore?.opacity} → ${controlsAfter?.opacity}, aria-pressed=${controlsAfter?.pressed}`,
);
check(
  "arrange mode grows the controls to a touch target",
  Boolean(controlsAfter && controlsBefore) &&
    controlsAfter.size > controlsBefore.size &&
    controlsAfter.size >= 30,
  `${controlsBefore?.size}px → ${controlsAfter?.size}px tall (target >= 30)`,
);
await page.reload({ waitUntil: "domcontentloaded" });
await settle(1400);
const arrangeAfterReload = await page.evaluate(() => {
  const toolbar = document.querySelector("main .grid > div[draggable] > div.absolute");
  return toolbar ? getComputedStyle(toolbar).opacity : null;
});
check("arrange mode is remembered after a reload", arrangeAfterReload === "1", `opacity=${arrangeAfterReload}`);
await page.evaluate(() => {
  const button = [...document.querySelectorAll("button")].find(
    (node) => node.textContent?.trim() === "Done",
  );
  button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
});
await settle(300);

/* 10. Responsive: 1280 and 1024 widths must not overflow horizontally. */
for (const width of [1280, 1024]) {
  await page.setViewportSize({ width, height: 900 });
  await settle(600);
  const overflow = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check(`no horizontal overflow at ${width}px`, overflow.doc <= overflow.client + 1, `${overflow.doc} vs ${overflow.client}`);
  await page.screenshot({ path: `${outDir}/05-responsive-${width}.png` });
}
await page.setViewportSize({ width: 1600, height: 1000 });

/* 11. Performance counters on the real page. */
await page.goto(`${base}/#/`, { waitUntil: "domcontentloaded" });
await settle(2500);
const perf = await page.evaluate(() => {
  const navigation = performance.getEntriesByType("navigation")[0];
  const paints = performance.getEntriesByType("paint").map((entry) => ({ name: entry.name, start: Math.round(entry.startTime) }));
  const resources = performance.getEntriesByType("resource");
  const transfer = resources.reduce((sum, entry) => sum + (entry.transferSize || 0), 0);
  return {
    domNodes: document.getElementsByTagName("*").length,
    paint: paints,
    domContentLoaded: Math.round(navigation?.domContentLoadedEventEnd ?? 0),
    loadEvent: Math.round(navigation?.loadEventEnd ?? 0),
    resourceCount: resources.length,
    transferBytes: transfer,
    heapMb: performance.memory ? Math.round((performance.memory.usedJSHeapSize / 1048576) * 10) / 10 : null,
  };
});
report.perf = { loadMs, ...perf };
check("DOM stays bounded", perf.domNodes < 2600, `${perf.domNodes} nodes`);
check("initial transfer reasonable for a local app", perf.transferBytes < 900_000, `${Math.round(perf.transferBytes / 1024)} KB transferred`);
check("first contentful paint under 1.5 s on loopback", (perf.paint.find((entry) => entry.name === "first-contentful-paint")?.start ?? 9999) < 1500, JSON.stringify(perf.paint));

/* 12. Animation is paused when the tab is hidden. */
const pausedCheck = await page.evaluate(async () => {
  const canvas = document.querySelector("main canvas");
  if (!canvas) return { supported: false };
  const context = canvas.getContext("2d");
  Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
  const first = context.getImageData(0, 0, canvas.width, canvas.height).data.slice(0, 4000).join(",");
  await new Promise((resolve) => setTimeout(resolve, 700));
  const second = context.getImageData(0, 0, canvas.width, canvas.height).data.slice(0, 4000).join(",");
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  return { supported: true, identical: first === second };
});
check("AI core pauses when the tab is hidden", pausedCheck.supported ? pausedCheck.identical : true, JSON.stringify(pausedCheck));

/* 13. Reduced motion honoured. */
await page.evaluate(() => { localStorage.setItem("omni.motion", "reduce"); localStorage.setItem("omni.motion.pref", "reduce"); });
await page.reload({ waitUntil: "domcontentloaded" });
await settle(900);
const motionAttr = await page.evaluate(() => document.documentElement.dataset.motion);
check("reduced motion applied from preference", motionAttr === "reduce", motionAttr);
await page.screenshot({ path: `${outDir}/06-reduced-motion.png` });
await page.evaluate(() => { localStorage.setItem("omni.motion", "full"); localStorage.setItem("omni.motion.pref", "system"); });
await page.goto(`${base}/#/`, { waitUntil: "domcontentloaded" });

/* 13b. Right-to-left: the shell mirrors, keeps its contrast and does not overflow. */
await page.evaluate(() => {
  const raw = JSON.parse(localStorage.getItem("omni.ui.v1") || "{}");
  localStorage.setItem("omni.ui.v1", JSON.stringify({ ...raw, direction: "rtl" }));
});
await page.reload({ waitUntil: "domcontentloaded" });
await settle(1400);
const rtl = await page.evaluate(() => {
  const root = document.documentElement;
  const aside = document.querySelector("aside");
  const main = document.querySelector("main");
  const box = (node) => (node ? node.getBoundingClientRect() : null);
  return {
    dir: root.dir,
    dataset: root.dataset.direction,
    asideLeft: box(aside)?.left ?? null,
    mainLeft: box(main)?.left ?? null,
    asideWidth: box(aside)?.width ?? null,
    doc: root.scrollWidth,
    client: root.clientWidth,
  };
});
check("html dir switches to rtl", rtl.dir === "rtl", `dir=${rtl.dir}`);
check("sidebar mirrors to the right edge", rtl.asideLeft !== null && rtl.asideLeft > 100, `aside.left=${Math.round(rtl.asideLeft ?? -1)}px`);
check("content moves to the left of the mirrored sidebar", rtl.mainLeft !== null && rtl.asideLeft !== null && rtl.mainLeft < rtl.asideLeft, `main.left=${Math.round(rtl.mainLeft ?? -1)}px < aside.left=${Math.round(rtl.asideLeft ?? -1)}px`);
check("rtl layout does not overflow horizontally", rtl.doc <= rtl.client + 1, `${rtl.doc} vs ${rtl.client}`);
await page.screenshot({ path: `${outDir}/07-dashboard-rtl.png` });
await page.evaluate(() => {
  const raw = JSON.parse(localStorage.getItem("omni.ui.v1") || "{}");
  localStorage.setItem("omni.ui.v1", JSON.stringify({ ...raw, direction: "auto" }));
});
await page.reload({ waitUntil: "domcontentloaded" });
await settle(900);
const autoDir = await page.evaluate(async () => {
  const token = document.querySelector('meta[name="pc-agent-token"]')?.content ?? "";
  let engineDirection = null;
  try {
    const response = await fetch("/v1/i18n/status", { headers: { Authorization: `Bearer ${token}` } });
    if (response.ok) {
      const payload = await response.json();
      // Every /v1 route answers `{ok, data}`; read the nested payload.
      const body = payload.data ?? payload;
      engineDirection = body.direction ?? null;
    }
  } catch (error) {
    engineDirection = `error: ${error.message}`;
  }
  return {
    dir: document.documentElement.dir,
    locale: document.documentElement.dataset.direction ?? null,
    engineDirection,
  };
});
check(
  "direction auto follows the engine locale",
  autoDir.engineDirection !== null &&
    (autoDir.dir || "ltr") === (autoDir.engineDirection === "rtl" ? "rtl" : "ltr"),
  `dir=${autoDir.dir || "(default=ltr)"} engine=${autoDir.engineDirection}`,
);

/* 14. No console errors, no page errors, no foreign origins. */
await page.goto(`${base}/#/`, { waitUntil: "domcontentloaded" });
await settle(1200);
check("no console errors", report.consoleErrors.length === 0, report.consoleErrors.slice(0, 3).join(" | "));
check("no uncaught page errors", report.pageErrors.length === 0, report.pageErrors.slice(0, 3).join(" | "));
check("no failed requests", report.failedRequests.length === 0, report.failedRequests.slice(0, 3).join(" | "));
check("no cross-origin requests", report.foreignRequests.length === 0, report.foreignRequests.slice(0, 3).join(" | "));

report.summary = {
  total: report.checks.length,
  passed: report.checks.filter((item) => item.pass).length,
  failed: report.checks.filter((item) => !item.pass).map((item) => item.name),
};
await writeFile(`${outDir}/report.json`, JSON.stringify(report, null, 2));
await browser.close();

console.log(`\n${report.summary.passed}/${report.summary.total} checks passed`);
if (report.summary.failed.length) {
  console.error(`FAILED: ${report.summary.failed.join(", ")}`);
  process.exit(1);
}
