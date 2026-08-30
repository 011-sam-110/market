/* Screenshot the built board, in one batched round.
 *
 *   node tools/shoot.mjs [--base http://127.0.0.1:4173] [--out ../.review]
 *
 * Three widths, chosen from how this board is actually looked at rather than
 * from a device list:
 *
 *   390   the phone it is read on most mornings;
 *   1440  a nominal desktop, the width the layout was composed at;
 *   1800  the operator's effective CSS width at 80% browser zoom. He reviews
 *         at 80%, so every type size renders about 20% smaller than designed
 *         and the layout has ~25% more room than 1440. Both of those are
 *         real and neither shows up at 1440.
 *
 * Viewport shots, never `fullPage`. A full-page capture races the entry
 * animation and comes back with charts and figures at frame 0 — blank marks
 * on a page that looks fine in a browser. Each view is captured at the top
 * and then scrolled, so what is below the fold is still seen.
 */

import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}

const BASE = args.get("base") ?? "http://127.0.0.1:4173";
const OUT = resolve(args.get("out") ?? "../.review");

const WIDTHS = [
  { name: "phone", width: 390, height: 844, dsf: 3 },
  { name: "desktop", width: 1440, height: 900, dsf: 2 },
  { name: "zoom80", width: 1800, height: 1000, dsf: 1.5 },
];

const VIEWS = ["shortlist", "console", "niches", "explore", "coverage", "trends"];

const console_errors = [];

async function settle(page) {
  await page.waitForLoadState("networkidle");
  // The board fetches ~3 MB before it renders anything, then runs one
  // authored entrance. Waiting for the record head means waiting for real
  // content rather than for a timer.
  await page.waitForSelector(".record__facts dd", { timeout: 30_000 }).catch(() => {});
  await page.waitForTimeout(700);
}

const browser = await chromium.launch();

for (const w of WIDTHS) {
  const dir = resolve(OUT, w.name);
  await mkdir(dir, { recursive: true });

  const context = await browser.newContext({
    viewport: { width: w.width, height: w.height },
    deviceScaleFactor: w.dsf,
    isMobile: w.name === "phone",
    hasTouch: w.name === "phone",
    reducedMotion: "no-preference",
  });
  const page = await context.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") console_errors.push(`[${w.name}] ${m.text()}`);
  });
  page.on("pageerror", (e) => console_errors.push(`[${w.name}] ${e.message}`));

  for (const view of VIEWS) {
    await page.goto(`${BASE}/#/${view}`, { waitUntil: "domcontentloaded" });
    await settle(page);

    await page.screenshot({ path: resolve(dir, `${view}-01-top.png`) });

    // Two scrolls rather than one full-page shot: the fold is where a
    // layout usually breaks and a stitched capture hides it.
    for (const step of [1, 2]) {
      const moved = await page.evaluate((s) => {
        const before = window.scrollY;
        window.scrollTo(0, window.innerHeight * 0.85 * s);
        return window.scrollY !== before || window.scrollY > 0;
      }, step);
      if (!moved) break;
      await page.waitForTimeout(450);
      await page.screenshot({ path: resolve(dir, `${view}-0${step + 1}-scroll.png`) });
    }

    // The body must never scroll horizontally. Checked here rather than by
    // eye, because a 4px overflow is invisible in a screenshot and obvious
    // on a phone.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    if (overflow > 0) {
      console_errors.push(
        `[${w.name}/${view}] the page scrolls horizontally by ${overflow}px`,
      );
    }
  }

  await context.close();
  console.log(`captured ${w.name} (${w.width}px)`);
}

await browser.close();

if (console_errors.length) {
  console.log("\nPROBLEMS FOUND:");
  for (const e of console_errors) console.log("  " + e);
  process.exitCode = 1;
} else {
  console.log("\nno console errors, no horizontal overflow");
}
console.log(`shots in ${OUT}`);
