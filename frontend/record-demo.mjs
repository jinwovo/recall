// Records the full Ask flow (typing → retrieve → streamed answer with [n] citations →
// LLM-judge verdict → citation click) as a video for the README demo GIF.
//   node record-demo.mjs         → prints VIDEO: <path to .webm>
import { chromium } from "playwright";

const BASE = process.env.UI_BASE || "http://localhost:3000";
const QUERY = process.env.UI_QUERY || "How do I prevent duplicate messages when the producer retries?";
const OUT_DIR = process.env.UI_VIDEO_DIR || "demo-video";

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  recordVideo: { dir: OUT_DIR, size: { width: 1280, height: 800 } },
});
const page = await context.newPage();

await page.goto(BASE, { waitUntil: "networkidle" });
// Screencast only captures on main-frame paints; during long idle waits (CPU LLM judging)
// nothing repaints and the video freezes early. An invisible ticking counter forces a
// cheap layout+paint every 250ms so the recording covers the whole pipeline.
await page.evaluate(() => {
  const tick = document.createElement("div");
  tick.style.cssText =
    "position:fixed;bottom:2px;right:2px;width:2px;height:2px;overflow:hidden;color:transparent;z-index:9999";
  document.body.appendChild(tick);
  setInterval(() => {
    tick.textContent = String(Date.now());
  }, 250);
});
await page.waitForTimeout(700);
await page.click("input");
await page.type("input", QUERY, { delay: 30 }); // visible typing
await page.waitForTimeout(400);
await page.click('button:has-text("Ask")');

await page.waitForSelector("#src-0", { timeout: 60000 }); // sources arrived
try {
  await page.waitForSelector('[data-testid="groundedness-badge"]', { timeout: 300000 });
  console.log("judge verdict arrived");
} catch {
  console.log("no groundedness badge within timeout (continuing)");
}
try {
  await page.waitForFunction(() => !document.body.innerText.includes("● streaming"), { timeout: 30000 });
} catch {
  console.log("stream did not finish within timeout (continuing)");
}
await page.waitForTimeout(1200);

const chip = await page.$("button.align-super");
if (chip) {
  await chip.click(); // highlight the cited source
  await page.waitForTimeout(1800);
}

const video = page.video();
await context.close(); // flush the recording
console.log("VIDEO:", await video.path());
await browser.close();
