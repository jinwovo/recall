# ADR 0010 — From portfolio to tool: folder ingestion, MCP surface, reusable gate, self-tuning loop

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

Nine ADRs in, the platform is deep but closed: the corpus is a demo JSONL, the only client
is the bundled UI, the eval gate guards this repo alone, and every tuning decision is
manual. Each of those is a wall between "impressive to read" and "useful to a stranger" —
and usefulness, not depth, is what makes an open-source repo get adopted. This round
removes the four walls without adding a single new service.

## Decision

**1. Bring-your-own-corpus (`scripts/ingest_folder.py`).** Walk a directory
(.md/.txt/.html built in, .pdf via optional `pypdf`), derive stable docIds from relative
paths, detect KO/EN heuristically, and push every file through the *real* pipeline —
`/api/ingest` → Kafka → chunk → embed → idempotent upsert — then hold at the indexing
barrier so "done" means "searchable". No side door: user documents get the same
durability contract (202 ⇒ archived + broker-acked, ADR 0005) as everything else.
Re-running converges (content-hash idempotency, ADR 0003).

**2. MCP surface (`mcp-server/`).** A thin stdio server exposing `recall_search` and
`recall_ask` over the Model Context Protocol — Claude Desktop / Claude Code / any MCP
client can search and question the user's own corpus, locally, no API key. The server is
a client of the public API (zero backend changes): SSE events are collected into a final
answer with citations, the groundedness verdict, and an explicit note when the sufficiency
gate abstained. RAG guardrails travel with the answer instead of being lost at the
protocol boundary.

**3. Reusable eval gate (`.github/actions/rag-eval-gate`).** The regression gate
(ADR 0007) extracted as a composite action any repo can use:
`uses: jinwovo/recall/.github/actions/rag-eval-gate@main` with an API URL, a gold set,
optional corpus seeding, and thresholds. The action vendors `run_eval.py` /
`seed_corpus.py` so it is self-contained for external checkouts; a CI `action-sync` job
diffs the vendored copies against the canonical files so they cannot drift silently. Our
own eval workflow now consumes the action — dogfooding is the compatibility test.

**4. Self-tuning loop (`eval/tune.py` + nightly `tune` workflow).** Retrieval knobs
(`rrf-k`, fused `candidates`) become clamped per-request overrides on `/api/search` —
the sweep probes configurations through the public API instead of redeploying. Nightly:
boot the real stack, sweep the grid against the gold set, and if a combo beats the
configured baseline by more than epsilon (`MRR@10` +0.01 — jitter must not generate churn
PRs), apply it to `application.yml` and **open a pull request** with the full sweep table.
The proposal is machine-made; the proof is the existing eval gate re-running on that PR;
the merge is human. The loop closes the eval-driven story: measure → gate → *propose*.

## Alternatives considered

- **Watch-folder daemon instead of a CLI**: continuous sync is a real product feature,
  but a daemon owns state (inode tracking, deletes) and failure modes; the CLI reuses the
  pipeline's idempotency to make re-runs the sync mechanism. Revisit if demand shows up.
- **Marketplace-listed standalone action repo**: listing requires `action.yml` at the
  root of a dedicated repository. The in-repo composite action ships the capability today
  (path-referenced `uses:` works for everyone); the split is mechanical when wanted.
- **Auto-merge tuning PRs when the gate passes**: tempting, but the gate checks floors,
  not intent — a human should see the sweep table. Also, PRs opened with the default
  `GITHUB_TOKEN` don't trigger workflows (documented; optional `TUNE_PAT` secret enables
  auto-triggered checks).
- **Bayesian optimization over the grid**: 9 combos × 10 queries doesn't need it; the
  grid is exhaustive at this scale. Revisit when the knob space grows (fusion weights,
  rerank depth).

## Consequences

- **+** The repo answers "can I use this for MY docs?" with one command, and "can my
  agent use it?" with one config block — the two questions adoption actually hinges on.
- **+** The eval gate becomes a giveable artifact; every external user of the action is a
  user of this repo.
- **+** Tuning regressions are structurally impossible to merge silently: proposals carry
  their sweep table and must survive the same gate as human changes.
- **−** Vendored action scripts duplicate two files (guarded by `action-sync`).
- **−** The tuning surface (`rrfK`, `candidates` params) is public; clamping bounds it,
  and it is read-only with respect to state.
- **−** Nightly tuning burns ~15 CI minutes on a quiet corpus mostly to conclude
  "no change" — acceptable; the schedule can widen once the config plateaus.

## Validation

- `TuningOverridesTest` (clamp floors/ceilings, absent-value fallback); full backend
  suite green.
- `ingest_folder.py` demo: mixed md/html/txt folder ingested through Kafka, barrier
  converges, content searchable.
- MCP server smoke: tools importable and schema-valid against the `mcp` SDK.
- The refactored eval workflow (action-consuming) gates this PR itself; `action-sync`
  proves vendored copies match.
- `tune.py` live sweep against the running stack produces the report and correctly
  identifies the baseline as winner (no churn PR on an already-tuned config).
