// Captures the /admin ops page — DLQ depth, forensic table, replay control.
// Seed a failure first (e.g. a poison record) so the table has rows:
//   node screenshot-admin.mjs
import { chromium } from "playwright";

const BASE = process.env.UI_BASE || "http://localhost:3000";
const OUT = process.env.UI_OUT || "../docs/screenshots/admin-dlq.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 810 } });
page.on("console", (m) => console.log("[console]", m.type(), m.text()));
page.on("pageerror", (e) => console.log("[pageerror]", e.message));
page.on("requestfailed", (r) => console.log("[reqfailed]", r.url(), r.failure()?.errorText));

await page.goto(`${BASE}/admin`, { waitUntil: "networkidle" });
try {
  // Depth stat renders after the first snapshot fetch; the table needs ≥1 record.
  await page.waitForFunction(
    () => document.querySelector('[data-testid="dlq-depth"]')?.textContent?.trim() !== "—",
    { timeout: 15000 },
  );
  await page.waitForSelector('[data-testid="dlq-table"] tbody tr', { timeout: 5000 });
  console.log("DLQ table populated");
} catch {
  console.log("no DLQ rows within timeout (empty queue?) — capturing anyway");
}
await page.waitForTimeout(800);
await page.screenshot({ path: OUT, fullPage: process.env.UI_FULLPAGE === "1" });
console.log("saved", OUT);
await browser.close();
