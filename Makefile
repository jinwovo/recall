.PHONY: up down logs backend frontend seed seed-corpus ingest eval eval-gate eval-sweep eval-test eval-stats loadtest

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

ingest:        ## ingest your own documents: make ingest DIR=~/my-docs
	python scripts/ingest_folder.py $(DIR)

eval:          ## run the retrieval eval harness
	cd eval && python run_eval.py queries.example.jsonl

eval-gate:     ## run the gated retrieval eval (same thresholds as CI, docs/adr/0007)
	cd eval && python run_eval.py gold.jsonl --gate

eval-sweep:    ## full sweep incl. experimental paper modes (hyde needs an LLM provider)
	cd eval && python run_eval.py gold.jsonl --modes bm25,vector,hybrid,hybrid-m3,hyde

eval-test:     ## unit-test the eval harness itself — no stack needed (docs/adr/0011)
	python -m unittest discover -s eval -p "test_*.py" -v

eval-stats:    ## what the current gold set can and cannot resolve, before running anything
	cd eval && python power_report.py gold.jsonl

loadtest:      ## run the k6 search load test
	k6 run load-test/search.js
