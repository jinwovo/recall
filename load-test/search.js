// k6 load test for the search endpoint.
//
//   make loadtest                                        # bm25, the mode a budget fits
//   MODE=hybrid k6 run load-test/search.js               # the real serving path
//   RECALL_API=http://localhost:18080 k6 run load-test/search.js
//
// The mode is explicit on purpose. An earlier version sent no `mode` at all, which means
// `hybrid`, and held it to p95 < 800ms — against a path measured at 161-164s per query on
// an idle machine with the CPU cross-encoder (docs/BENCHMARK-RUNBOOK.md). That threshold
// was about two hundred times off and the run could only ever fail.
//
// Measured single-query, idle, 5,183 documents indexed, CPU reranker:
//
//     bm25     1.21 s      lexical only
//     vector   0.49 s      one embed, then a kNN lookup
//     hybrid    161 s      both, fused, then ~3s per reranked candidate x 50
//
// So a latency budget is a statement about which mode you are testing. Reranking depth is
// the cost, and it is linear: `CANDIDATES=10` brings hybrid to ~28s. On a GPU reranker
// these numbers change entirely, which is the point of keeping them next to the thresholds
// rather than in a commit message.
import http from "k6/http";
import { check, sleep } from "k6";

const API = __ENV.RECALL_API || "http://localhost:8080";
const MODE = __ENV.MODE || "bm25";
const CANDIDATES = __ENV.CANDIDATES || "";

// Budgets from the measurements above, with room for contention, and a concurrency that
// means something for each mode: 20 virtual users against a CPU cross-encoder queue behind
// one another and measure the queue, not the system.
const PROFILE = {
  bm25: { vus: 20, duration: "30s", p95: 3000 },
  vector: { vus: 20, duration: "30s", p95: 3000 },
  hyde: { vus: 4, duration: "60s", p95: 30000 },
  hybrid: { vus: 4, duration: "120s", p95: 300000 },
}[MODE];

if (!PROFILE) {
  throw new Error(`unknown MODE '${MODE}' — use bm25, vector, hybrid or hyde`);
}

export const options = {
  vus: PROFILE.vus,
  duration: PROFILE.duration,
  thresholds: {
    http_req_duration: [`p(95)<${PROFILE.p95}`],
    http_req_failed: ["rate<0.01"],
  },
};

const QUERIES = [
  "kubernetes pod keeps restarting",
  "spring boot actuator prometheus",
  "s3 bucket policy read only",
  "쿠버네티스 롤링 업데이트",
];

export default function () {
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  let url = `${API}/api/search?q=${encodeURIComponent(q)}&mode=${MODE}`;
  if (CANDIDATES) {
    url += `&candidates=${encodeURIComponent(CANDIDATES)}`;
  }
  // Long enough for the slowest profile above; k6 defaults to 60s and would time out on
  // hybrid before the request ever returned, reporting a failure rate rather than a latency.
  const res = http.get(url, { timeout: `${PROFILE.p95 + 60000}ms` });
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(1);
}
