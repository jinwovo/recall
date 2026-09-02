.PHONY: up down logs backend frontend seed seed-corpus ingest eval eval-gate eval-sweep eval-test eval-stats eval-anytime eval-ppi eval-adaptive beir beir-eval calibrate loadtest

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

eval-gate:     ## gated retrieval eval; GATE_POLICY=point|ci-lower|regression|sequential
	cd eval && python run_eval.py $(or $(GOLD),gold.jsonl) --gate --gate-policy $(or $(GATE_POLICY),point) $(if $(filter sequential,$(GATE_POLICY)),--modes hybrid,)

eval-sweep:    ## full sweep incl. experimental paper modes (hyde needs an LLM provider)
	cd eval && python run_eval.py gold.jsonl --modes bm25,vector,hybrid,hybrid-m3,hyde

eval-test:     ## unit-test the eval harness itself — no stack needed (docs/adr/0011)
	python -m unittest discover -s eval -p "test_*.py" -v

eval-stats:    ## what the current gold set can and cannot resolve, before running anything
	cd eval && python power_report.py gold.jsonl

eval-anytime:  ## reproduce the anytime-evaluation experiment (docs/adr/0012) — no stack needed
	cd eval && python anytime_experiment.py

eval-ppi:      ## reproduce the judge-calibration experiment (docs/adr/0015) — no stack needed
	cd eval && python ppi_experiment.py

eval-adaptive: ## reproduce the drift/adaptive-conformal experiment (docs/adr/0016) — no stack needed
	cd eval && python adaptive_experiment.py

beir:          ## fetch a BEIR benchmark: make beir DATASET=scifact (300 queries, 5k docs)
	cd eval && python beir.py $(or $(DATASET),scifact)

beir-eval:     ## ingest a fetched BEIR benchmark and evaluate on it (needs the stack up)
	python scripts/seed_corpus.py eval/beir-$(or $(DATASET),scifact)/corpus.jsonl --timeout 1800
	cd eval && python run_eval.py beir-$(or $(DATASET),scifact)/gold.jsonl --json ../docs/beir-results.json --markdown ../docs/beir-summary.md --records ../docs/beir-scores.json

calibrate:     ## certify the context size and abstention threshold (docs/adr/0013)
	cd eval && python calibrate.py $(or $(GOLD),gold.jsonl) --json ../calibration.json

loadtest:      ## run the k6 search load test
	k6 run load-test/search.js
