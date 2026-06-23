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

For RAG quality (groundedness / citation coverage), add an LLM-judge pass that checks
each answer's claims against its cited passages.
