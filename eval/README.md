# Eval harness

Measures retrieval quality so improvements are **numbers, not vibes** (docs/adr/0001).

```bash
python run_eval.py queries.example.jsonl
# RECALL_API=http://localhost:8080 EVAL_K=10 python run_eval.py my_queries.jsonl
```

- Gold set format: JSONL, one `{ "query": "...", "relevant_doc_ids": ["..."] }` per line.
- Reports **Recall@K**, **MRR@K**, **nDCG@K**.
- **Sweeps `bm25` / `vector` / `hybrid` automatically** (via `/api/search?mode=`) and prints a
  comparison table — the hybrid lift is the README headline number. Example:

  ```
  queries=3  K=10

  mode        Recall@K     MRR@K    nDCG@K
  ----------------------------------------
  bm25           0.667     0.583     0.612
  vector         0.667     0.667     0.701
  hybrid         1.000     0.917     0.945
  ```
  (illustrative — fill in with your own gold set)
- Wire this into CI as a regression gate once you have a stable gold set.

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
