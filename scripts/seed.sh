#!/usr/bin/env bash
# Seed a few sample docs so search/QA and the eval harness have something to hit.
#   ./scripts/seed.sh
set -euo pipefail
API="${RECALL_API:-http://localhost:8080}"

post() {
  curl -s -X POST "$API/api/ingest" -H 'Content-Type: application/json' -d "$1" >/dev/null \
    && echo "queued: $2"
}

post '{"docId":"spring-actuator","source":"spring-docs","lang":"en","content":"Spring Boot Actuator exposes operational endpoints. To publish Prometheus metrics, add the micrometer-registry-prometheus dependency and set management.endpoints.web.exposure.include=prometheus. Scrape /actuator/prometheus."}' "spring-actuator"

post '{"docId":"k8s-pod-troubleshooting","source":"k8s-docs","lang":"ko","content":"쿠버네티스 파드가 계속 재시작(CrashLoopBackOff)되면 kubectl describe pod 와 kubectl logs --previous 로 직전 컨테이너 로그를 확인한다. 흔한 원인은 잘못된 환경변수, 라이브니스 프로브 실패, OOMKilled 이다."}' "k8s-pod-troubleshooting"

post '{"docId":"aws-s3-bucket-policy","source":"aws-docs","lang":"en","content":"An S3 bucket policy granting read-only public access uses Effect Allow, Principal *, Action s3:GetObject, and Resource arn:aws:s3:::your-bucket/*. Prefer CloudFront with OAC over public buckets for production."}' "aws-s3-bucket-policy"

echo "Done. Indexing is async — wait a few seconds, then try /api/search?q=..."
