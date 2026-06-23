.PHONY: up down logs backend frontend seed eval loadtest

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

eval:          ## run the retrieval eval harness
	cd eval && python run_eval.py queries.example.jsonl

loadtest:      ## run the k6 search load test
	k6 run load-test/search.js
