// k6 load test for the hybrid search endpoint.
//   k6 run load-test/search.js
//   RECALL_API=http://localhost:8080 k6 run load-test/search.js
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 20,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<800"], // p95 search latency budget (ms)
    http_req_failed: ["rate<0.01"],
  },
};

const API = __ENV.RECALL_API || "http://localhost:8080";
const QUERIES = [
  "kubernetes pod keeps restarting",
  "spring boot actuator prometheus",
  "s3 bucket policy read only",
  "쿠버네티스 롤링 업데이트",
];

export default function () {
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = http.get(`${API}/api/search?q=${encodeURIComponent(q)}`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(1);
}
