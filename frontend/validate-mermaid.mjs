// Validates every ```mermaid block in the docs using the real Mermaid parser.
import { chromium } from "playwright";
import fs from "fs";

const files = ["../README.md", "../docs/ARCHITECTURE.md"];
const blocks = [];
for (const f of files) {
  const md = fs.readFileSync(f, "utf8");
  const re = /```mermaid\n([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(md))) blocks.push({ f, code: m[1] });
}
console.log("found", blocks.length, "mermaid blocks");

const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent("<html><body></body></html>");
await page.addScriptTag({ url: "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" });
await page.evaluate(() => window.mermaid.initialize({ startOnLoad: false }));

let allOk = true;
for (let i = 0; i < blocks.length; i++) {
  const res = await page.evaluate(async (code) => {
    try { await window.mermaid.parse(code); return "OK"; } catch (e) { return "ERR: " + (e && e.message); }
  }, blocks[i].code);
  console.log(`${blocks[i].f} #${i + 1}: ${res}`);
  if (!res.startsWith("OK")) allOk = false;
}
await browser.close();
console.log(allOk ? "ALL VALID" : "HAS ERRORS");
