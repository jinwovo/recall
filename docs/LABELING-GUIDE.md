# Labeling guide — claims for the factuality certificate

**English** · [한국어](LABELING-GUIDE.ko.md)

This is the manual for the one step of [ADR 0017](adr/0017-claim-level-conformal-factuality.md)
that code cannot do: deciding, for each sentence of an answer, whether the passages it was
generated from support it. Those decisions become the calibration set, and the threshold
computed from them is what serving uses to withhold claims. **A labelling habit that is off
by a few percent moves the threshold, and the guarantee is only as true as these labels.**

## The workflow

```bash
# 1. Serving in observe mode: claims are scored, nothing is withheld
RAG_FACTUALITY_ENABLED=true RAG_FACTUALITY_THRESHOLD=-1   # in .env, then start the stack

# 2. Collect — one row per answer, every claim with "supported": null
make collect-claims GOLD=beir-scifact/gold.jsonl          # writes eval/claims-to-label.jsonl

# 3. Label — this guide

# 4. Certify — prints the threshold and the YAML to paste; exits non-zero if it cannot
make certify-claims
```

Each row looks like this:

```json
{"query": "how does the cache stay bounded?",
 "passages": [{"n": 1, "source": "redis.md", "content": "..."},
              {"n": 2, "source": "cache.md", "content": "..."}],
 "claims": [{"text": "Entries expire after the configured TTL [2].", "score": 0.91, "supported": null},
            {"text": "Eviction uses LFU by default.", "score": 0.12, "supported": null}]}
```

Set every `null` to `true` or `false`. **Do not look at `score` while labelling** — cover the
column or strip it to a copy. A label that peeks at the score agrees with the scorer by
construction, and the threshold computed from it certifies nothing.

## The one question

> **Does at least one of the listed `passages` state or directly imply this sentence?**

Not "is it true", not "is it reasonable", not "would an expert say so". The promise serving
makes is about *grounding in what the model was shown*, so that is the only thing a label may
measure.

| situation | label | why |
|---|:---:|---|
| A passage says it, in any wording | `true` | paraphrase is support |
| It follows from one passage without outside knowledge | `true` | "directly imply" |
| It is correct, but no passage says it | **`false`** | world knowledge is not grounding — this is the most common labelling error |
| It needs two passages combined | `true` | the scorer takes the best single passage, so these are rarer than they look; label honestly anyway |
| Part of it is supported and part is not | **`false`** | one unsupported part is an unsupported claim — "Redis persists with RDB and AOF, enabled by default" with nothing about the default is `false` |
| Numbers, versions, names, units differ at all | **`false`** | "30 seconds" is not supported by "30 ms"; "Kafka 3.6" is not supported by "Kafka 3.5" |
| The citation `[n]` points at the wrong passage, but another passage supports it | `true` | the label is about content; citation accuracy is a different question |
| The citation is right but the passage does not actually say it | **`false`** | a citation is not evidence |
| A hedge that is still a claim ("X may cause Y") | judge the claim | `true` only if a passage says X can cause Y |
| No checkable assertion ("Here is how:", "Two options exist:", "In summary") | `true` | nothing false is asserted. Labelling these `false` would raise the threshold for no reason |
| "I don't know based on the available documents" | — | never collected; abstentions are skipped |
| A sentence cut off mid-way (answer hit the token limit) | judge what it asserts so far | "The default timeout is" asserts nothing yet → `true`; "The default timeout is 5" asserts a value → check it |
| Code, a command, a config key | `true` only if it appears in or follows from a passage | an invented flag is the most expensive hallucination in a technical KB |
| You cannot tell within about a minute | **`false`** | doubt goes to the safe side. It makes the threshold slightly stricter, never looser |

## Things that look like shortcuts and are not

- **Do not use an LLM to pre-fill labels.** The judge is the instrument this calibration is
  supposed to be independent of ([ADR 0015](adr/0015-prediction-powered-inference.md)). If a
  judge-labelled set is wanted for volume, keep it separate and combine it with the human set
  through PPI, never by mixing the files.
- **Do not skip hard rows.** Skipping the ambiguous answers leaves an easier calibration set
  than serving will see, and the guarantee is exchangeability-conditional. Label hard rows
  `false` per the doubt rule instead.
- **Do not edit `text` or `score`.** Fix nothing but the labels.

## How many

- **Minimum to certify at all**: 9 answers at `alpha = 0.10` — enough for a statement, not for
  a useful threshold.
- **Recommended**: 200–300 answers, drawn from the questions real traffic asks. The 10-query
  `gold.jsonl` is too small and too clean; BEIR SciFact's 300 queries are a reasonable start.
- **Time**: about 15 seconds per claim once practised, and a few claims per answer — budget
  4–6 hours for 300 answers. Several sittings are better than one; label quality drops after
  about an hour.
- **Per domain**: if you will certify with `--by-group`, each group needs its own sample of
  this size.

## Checking the labels before you trust them

1. Have a second person label a random 10% of rows, blind to the first person's labels.
2. Compute agreement on those claims. **Below about 90%, stop**: the disagreements are almost
   always a rule in this guide being read two ways. Resolve them, add the case to the table
   above, and re-check the rows that rule touched.
3. Run `make certify-claims`. If the certificate comes back **vacuous** (keeps under 20% of
   supported claims), the scorer cannot separate supported from unsupported claims on your
   data — more labels will not fix that, and a looser `alpha` only hides it.

## After the threshold ships

The threshold is valid while traffic looks like the labelled sample. `FactualityMonitor`
watches that from the judge's side and logs a warning when the promise has been ruled out
(`recall_rag_factuality_alarming` on the dashboard). When it fires, collect and label a fresh
sample from current traffic rather than adjusting the number by hand.
