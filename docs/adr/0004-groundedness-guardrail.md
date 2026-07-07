# ADR 0004 — Post-hoc groundedness judge (hallucination guardrail)

- **Status:** Accepted
- **Date:** 2026-07-07

## Context

RAG answers can sound confident while citing nothing the retriever actually
returned. The existing guardrails are preventive only: a grounded system prompt
("answer ONLY from the provided context"), inline `[n]` citations, and a canned
"I don't know" when retrieval comes back empty. Nothing **verifies the finished
answer** against its sources, and the eval story (README metric: groundedness %)
had no producer.

Constraints:
- Must not add latency to the visible answer — TTFT and token streaming are a
  headline metric (ADR 0002).
- Must work on the free local provider (Ollama, small model) as well as Claude —
  so the judge contract has to be parseable from a 3B model's output.
- A guardrail that can break the answer path is worse than no guardrail.

## Decision

**Judge after the answer, not during it** — a post-hoc LLM-judge grades the
completed answer against the retrieved passages, on the **CHEAP model tier**
(ADR 0002: grading is a cheap-tier task, not a PRIMARY one).

Mechanics (`GroundednessJudge`, wired in `RagService`):

1. The answer streams to the client unchanged (`sources` → `token*`).
2. A `judging` SSE event tells the UI verification started.
3. The judge prompt carries the same passages + question + finished answer and
   demands **exactly one word**: `SUPPORTED` / `PARTIAL` / `UNSUPPORTED`.
   Single-word contract = robust to parse even from small local models; parsing
   is lenient (keyword scan, `UNSUPPORTED` checked before its substring
   `SUPPORTED`).
4. The verdict is emitted as a `groundedness` SSE event
   (`{"verdict":"SUPPORTED","score":1.0}`), shown as a badge in the UI,
   recorded on the `query_log` row (score 1.0 / 0.5 / 0.0), and exported as
   Prometheus metrics (`recall_rag_groundedness` summary,
   `recall_rag_judge_verdicts_total{verdict=...}` counter) → groundedness %
   becomes a dashboard number and an eval input.
5. **Fail-open**: judge error, timeout (`RAG_JUDGE_TIMEOUT_SECONDS`, default
   45s), or unparseable output → no event, `groundedness = null`, answer
   unaffected. Toggle: `RAG_JUDGE_ENABLED`.
6. Abstentions are **not judged**: if the answer contains the canned
   "I don't know", declining is the guardrail working — grading it as
   "unsupported" would poison the metric.
7. `UNSUPPORTED` answers and abstentions are **excluded from the semantic
   cache** — a cache hit replays its answer verbatim and skips re-judging, so
   caching a judged-bad answer makes the hallucination sticky, and caching an
   "I don't know" makes a sampling artifact permanent for every near-duplicate
   question. (Found live: an abstention got cached and replayed on the eval run.)

## Alternatives considered

- **Inline / pre-send judging** (hold the answer until verified): kills TTFT,
  turns streaming into batch. Rejected — visibility over gating. A judged-bad
  answer is flagged, not suppressed; suppression can be layered on later once
  judge precision is measured.
- **NLI / embedding-similarity checks** (no LLM): cheaper, but sentence-level
  NLI over KO/EN mixed technical text underperforms, and we already have a
  cheap-tier LLM path with a provider abstraction.
- **Claude native citations as the guardrail**: still planned (paid key), but
  citations prove *where* a claim came from, not *whether unsupported claims
  exist* — the two are complementary.

## Consequences

- **+** Every generated answer gets a machine-readable groundedness label at
  near-zero user-visible cost (verdict arrives after the full answer).
- **+** README/eval "groundedness %" is now measured, not aspirational;
  regressions in prompt or retrieval quality show up on the dashboard.
- **−** One extra CHEAP-tier LLM call per uncached answer (~the cheapest call
  in the pipeline; on the local provider it is free).
- **−** LLM-as-judge is itself fallible (especially the 3B local judge) — the
  score is a signal, not ground truth. Judge quality is auditable via
  `query_log` (query, answer length, verdict) against the eval gold set.
