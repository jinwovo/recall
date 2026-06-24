// Captures the Recall QA (Ask) view: streamed grounded answer + clickable [n] citation.
//   node screenshot-ask.mjs   (override query via UI_QUERY)
import { chromium } from "playwright";

const BASE = process.env.UI_BASE || "http://localhost:3000";
const QUERY = process.env.UI_QUERY || "How do I prevent duplicate messages when the producer retries?";
const OUT = process.env.UI_OUT || "../docs/screenshots/qa.png";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

await page.goto(BASE, { waitUntil: "networkidle" });
await page.fill("input", QUERY);
await page.click('button:has-text("Ask")');

await page.waitForSelector("#src-0", { timeout: 30000 }); // sources arrived
// wait until streaming finishes (the "● streaming" indicator disappears)
try {
  await page.waitForFunction(() => !document.body.innerText.includes("● streaming"), { timeout: 120000 });
} catch {
  console.log("stream did not finish within timeout (continuing)");
}
await page.waitForTimeout(800);

const chip = await page.$("button.align-super");
if (chip) { await chip.click(); await page.waitForTimeout(800); } // highlight cited source

await page.screenshot({ path: OUT, fullPage: true });
const text = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").slice(0, 500));
console.log("PAGE TEXT:", text);
console.log("saved", OUT);
await browser.close();
