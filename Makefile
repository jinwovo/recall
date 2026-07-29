.PHONY: up down logs backend frontend seed seed-corpus eval eval-gate loadtest

up:            ## start infra (ES, Redis, Postgres, Kafka, MinIO, sidecar, Prometheus, Grafana)
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

backend:       ## run the Spring Boot backend on the host
	cd backend && ./gradlew bootRun

frontend:      ## run the Next.js dev server
	cd frontend && npm install && npm run dev

seed:          ## ingest sample documents
	./scripts/seed.sh

seed-corpus:   ## ingest the eval corpus and wait for async indexing to converge
	python scripts/seed_corpus.py eval/corpus.jsonl

eval:          ## run the retrieval eval harness
	cd eval && python run_eval.py queries.example.jsonl

eval-gate:     ## run the gated retrieval eval (same thresholds as CI, docs/adr/0007)
	cd eval && python run_eval.py gold.jsonl --gate

loadtest:      ## run the k6 search load test
	k6 run load-test/search.js
