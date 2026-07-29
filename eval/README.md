# Eval harness

Measures retrieval quality so improvements are **numbers, not vibes** (docs/adr/0001) — and
enforces them in CI so regressions are **build failures, not surprises** (docs/adr/0007).

```bash
python run_eval.py gold.jsonl                                  # comparison table
python run_eval.py gold.jsonl --gate                           # CI mode: exit 1 on regression
python run_eval.py gold.jsonl --json out.json --markdown summary.md
# RECALL_API=http://localhost:18080 python run_eval.py gold.jsonl
```

- Gold set format: JSONL, one `{ "query": "...", "relevant_doc_ids": ["..."] }` per line.
- **Sweeps `bm25` / `vector` / `hybrid` automatically** (via `/api/search?mode=`) and reports
  **Recall@5, Recall@10, MRR@10, nDCG@10** per mode in a single run, plus the first-relevant
  rank per query — the hybrid lift is the README headline number.
- Metrics are **doc-level**: rankings come back chunk-level, so ranked docIds are
  deduplicated by first occurrence before scoring (otherwise one document's chunks occupy
  several ranks and DCG can exceed the ideal DCG).
- `--gate` enforces minimum thresholds on hybrid (defaults: Recall@5 ≥ 0.90, MRR@10 ≥ 0.85,
  nDCG@10 ≥ 0.85 — deliberately below measured values; a gate catches regressions, it isn't
  a leaderboard) and exits non-zero on breach. `--markdown` renders the table + verdicts for
  the GitHub step summary; `--json` dumps everything, per-query detail included.
- `--modes` extends the sweep with the experimental paper modes (docs/adr/0008):
  `hybrid-m3` (bge-m3 tri-modal self-hybrid rerank, deterministic) and `hyde`
  (LLM hypothetical-document embeddings — needs a provider; free local Ollama works).
  `make eval-sweep` runs all five. CI keeps the deterministic default sweep.
- CI: `.github/workflows/eval.yml` boots the real stack, seeds the corpus through the async
  pipeline (`scripts/seed_corpus.py` polls ES until all docs are searchable), then runs the
  gate on every retrieval-affecting PR.

## RAG QA eval (groundedness)

`run_qa_eval.py` drives the full RAG path (`/api/ask`, SSE) over the same gold set and
aggregates the answer-quality signals produced by the post-hoc LLM judge (docs/adr/0004):

```bash
python run_qa_eval.py gold.jsonl
# RECALL_API=http://localhost:18080 python run_qa_eval.py gold.jsonl
```

Reports per query and in summary:

- **groundedness** — judge verdict split (supported / partial / unsupported) + average score
- **citation coverage** — share of generated answers containing at least one `[n]` citation
- **abstention** — answers that (correctly) said "I don't know"; never graded as hallucinations
- **TTFT p50 / e2e p50** — streaming latency seen by the client

Cache hits are reported separately (cached answers skip the judge). Requires the stack up
and an LLM provider configured — the free local `ollama` provider works.
