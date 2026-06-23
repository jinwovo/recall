// Captures a screenshot of the Recall search UI with real results.
//   node screenshot.mjs
import { chromium } from "playwright";

const BASE = process.env.UI_BASE || "http://localhost:3000";
const QUERY = process.env.UI_QUERY || "find the most similar items to a query embedding quickly";
const OUT = process.env.UI_OUT || "../docs/screenshots/search.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on("console", (m) => console.log("[console]", m.type(), m.text()));
page.on("pageerror", (e) => console.log("[pageerror]", e.message));
page.on("requestfailed", (r) => console.log("[reqfailed]", r.url(), r.failure()?.errorText));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.fill("input", QUERY);
await page.click('button:has-text("Search")');
try {
  await page.waitForSelector("#src-0", { timeout: 15000 });
  console.log("results appeared");
} catch {
  console.log("no #src-0 within timeout");
}
await page.waitForTimeout(1000);
await page.screenshot({ path: OUT, fullPage: true });
console.log("saved", OUT);
await browser.close();
