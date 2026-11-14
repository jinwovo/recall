package com.portfolio.recall.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.portfolio.recall.cache.SemanticCacheService;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.embedding.EmbeddingClient;
import com.portfolio.recall.llm.FinishReason;
import com.portfolio.recall.llm.LlmClient;
import com.portfolio.recall.llm.ModelTier;
import com.portfolio.recall.llm.StreamChunk;
import com.portfolio.recall.persistence.QueryLogService;
import com.portfolio.recall.search.RetrievedChunk;
import com.portfolio.recall.search.SearchService;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * RAG QA (docs/adr/0001, 0002, 0004, 0009, 0013, 0016): retrieve → size the context → sufficiency
 * gate → assemble grounded prompt → stream answer over SSE, then grade the finished answer
 * with a post-hoc groundedness judge. Semantic-cache short-circuit and a per-question query
 * log included.
 * SSE event types: {@code sources}, {@code sufficiency}, {@code token}, {@code finish},
 * {@code cache}, {@code judging}, {@code groundedness}, {@code claims}, {@code done},
 * {@code error}.
 */
@Service
public class RagService {

    /** Stable system prompt → carries the prompt-cache breakpoint in {@link ClaudeClient}. */
    private static final String SYSTEM_PROMPT = """
            You are Recall, a precise technical assistant. Answer ONLY from the provided
            context passages. Cite sources inline as [n] using the passage numbers.
            If the context does not contain the answer, say exactly: "I don't know based on
            the available documents." Do not invent facts or cite passages you did not use.
            """;

    /** Canned answer when retrieval returns no context — the no-hallucination floor. */
    private static final String IDK = "I don't know based on the available documents.";

    private final SearchService search;
    private final EmbeddingClient embeddings;
    private final SemanticCacheService cache;
    private final LlmClient llm;
    private final GroundednessJudge judge;
    private final SufficiencyCheck sufficiency;
    private final ConformalSetSizer contextSizer;
    private final CoverageMonitor coverage;
    private final ClaimVerifier claims;
    private final FactualityMonitor factuality;
    private final QueryLogService queryLog;
    private final ObjectMapper json;
    private final MeterRegistry meters;
    private final Duration firstTokenTimeout;
    private final Duration stallTimeout;

    public RagService(SearchService search, EmbeddingClient embeddings, SemanticCacheService cache,
                      LlmClient llm, GroundednessJudge judge, SufficiencyCheck sufficiency,
                      ConformalSetSizer contextSizer, CoverageMonitor coverage,
                      ClaimVerifier claims, FactualityMonitor factuality, QueryLogService queryLog, ObjectMapper json, MeterRegistry meters,
                      RecallProperties props) {
        this.search = search;
        this.embeddings = embeddings;
        this.cache = cache;
        this.llm = llm;
        this.judge = judge;
        this.sufficiency = sufficiency;
        this.contextSizer = contextSizer;
        this.coverage = coverage;
        this.claims = claims;
        this.factuality = factuality;
        this.queryLog = queryLog;
        this.json = json;
        this.meters = meters;
        this.firstTokenTimeout =
                Duration.ofSeconds(props.rag().generation().firstTokenTimeoutSeconds());
        this.stallTimeout =
                Duration.ofSeconds(props.rag().generation().stallTimeoutSeconds());
    }

    public Flux<ServerSentEvent<String>> ask(String query) {
        return Flux.defer(() -> {
            long start = System.nanoTime();
            // Embed once: the vector is the semantic-cache key AND is reused by hybridWithVector().
            return embeddings.embedOne(query)
                    .flatMapMany(vector -> cache.lookup(vector).flatMapMany(hit -> hit
                            .map(answer -> cached(query, answer, start))
                            .orElseGet(() -> generate(query, vector, start))));
        }).onErrorResume(e -> Flux.just(sse("error", e.getMessage())));
    }

    private Flux<ServerSentEvent<String>> cached(String query, String answer, long start) {
        Flux<ServerSentEvent<String>> head = Flux.just(sse("cache", "hit"), sse("token", toJson(answer)));
        Flux<ServerSentEvent<String>> tail = Flux.defer(() ->
                queryLog.record(query, "ask", true, 0, answer.length(), elapsedMs(start), null)
                        .thenMany(Flux.just(sse("done", ""))));
        return Flux.concat(head, tail);
    }

    private Flux<ServerSentEvent<String>> generate(String query, float[] vector, long start) {
        // Reuse the embedding from the cache key — no second embed call.
        return search.hybridWithVector(query, vector).flatMapMany(all -> {
            // Search returns the full reranked list. How much of it becomes LLM context is
            // decided per query by the conformal set sizer (docs/adr/0013): a confident
            // retrieval gets a short prompt, an ambiguous one gets a long enough prompt to
            // keep the coverage guarantee. Uncalibrated or disabled, this is topK.
            List<RetrievedChunk> chunks = all.stream().limit(contextSizer.size(all)).toList();

            // Groundedness guard: no context → don't call the LLM, return the canned "I don't know".
            if (chunks.isEmpty()) {
                return Flux.concat(
                        Flux.just(sse("sources", "[]"), sse("token", toJson(IDK))),
                        Flux.defer(() -> queryLog.record(query, "ask", false, 0, IDK.length(), elapsedMs(start), null)
                                .thenMany(Flux.just(sse("done", "")))));
            }

            // Sufficiency gate (docs/adr/0009): low-confidence retrieval gets a CHEAP-tier
            // "can these passages answer this?" check — insufficient → abstain before
            // spending the PRIMARY generation. Fail-open; the post-hoc judge still runs.
            return sufficiency.allowGeneration(query, chunks).flatMapMany(sufficient -> {
                if (!sufficient) {
                    return Flux.concat(
                            Flux.just(sse("sources", toJson(chunks)),
                                    sse("sufficiency", "{\"verdict\":\"INSUFFICIENT\"}"),
                                    sse("token", toJson(IDK))),
                            Flux.defer(() -> queryLog.record(query, "ask", false, chunks.size(),
                                            IDK.length(), elapsedMs(start), null)
                                    .thenMany(Flux.just(sse("done", "")))));
                }

                String prompt = buildPrompt(query, chunks);
                StringBuilder answer = new StringBuilder();
                AtomicReference<FinishReason> finish = new AtomicReference<>(FinishReason.UNREPORTED);

                Flux<ServerSentEvent<String>> sources = Flux.just(sse("sources", toJson(chunks)));
                Flux<ServerSentEvent<String>> tokens = llm
                        .stream(SYSTEM_PROMPT, prompt, ModelTier.PRIMARY)
                        // The only path here without a fallback, so it is the only one that
                        // must not fail open: bound the gap between tokens rather than the
                        // answer. A provider that stops sending would otherwise hold the SSE
                        // connection for as long as it hangs, and the tail below — judge,
                        // cache, query log — never runs, because the stream never completes.
                        // First token is separate: prefill is the slow part, and one timeout
                        // sized for a local CPU model would let a mid-answer stall run just
                        // as long.
                        .timeout(Mono.delay(firstTokenTimeout), t -> Mono.delay(stallTimeout))
                        .concatMap(chunk -> switch (chunk) {
                            case StreamChunk.Text t -> {
                                answer.append(t.text());
                                yield Flux.just(sse("token", toJson(t.text())));
                            }
                            // The reader is told why the answer stopped, not only that it did:
                            // a max_tokens cut ends mid-sentence with no error to explain it.
                            case StreamChunk.End e -> {
                                finish.set(e.reason());
                                meters.counter("recall.rag.generation.finish",
                                        "reason", e.reason().name().toLowerCase()).increment();
                                yield Flux.just(sse("finish", toJson(Map.of("reason", e.reason()))));
                            }
                        });
                // TODO: switch [n] citations → Claude native citations (claude provider + paid key).
                Flux<ServerSentEvent<String>> tail =
                        Flux.defer(() -> judged(query, vector, chunks, answer.toString(), finish.get(), start));

                return Flux.concat(sources, tokens, tail);
            });
        });
    }

    /**
     * Post-answer tail (docs/adr/0004): grade groundedness (fail-open), then persist
     * cache/log and close the stream. Abstentions ("I don't know") are not judged —
     * declining to answer is the guardrail working, not a hallucination.
     */
    private Flux<ServerSentEvent<String>> judged(String query, float[] vector,
                                                 List<RetrievedChunk> chunks, String answer, FinishReason finish,
                                                 long start) {
        boolean judgeable = judge.enabled() && !answer.contains(IDK);
        Mono<Optional<Judgment>> judgment = judgeable
                ? judge.judge(query, chunks, answer)
                : Mono.just(Optional.empty());
        // Claim scoring (docs/adr/0017) runs beside the judge, not after it: both are
        // fail-open and bounded, so the tail waits for the slower one rather than the sum.
        boolean checkable = claims.enabled() && !answer.contains(IDK);
        Mono<Optional<ClaimReport>> claimReport = checkable
                ? claims.verify(chunks, answer)
                : Mono.just(Optional.empty());

        Flux<ServerSentEvent<String>> judging = judgeable || checkable
                ? Flux.just(sse("judging", "")) : Flux.empty();
        Flux<ServerSentEvent<String>> rest = Mono.zip(judgment, claimReport).flatMapMany(both -> {
            Optional<Judgment> j = both.getT1();
            Optional<ClaimReport> c = both.getT2();
            // Feed the verdict to the coverage monitor (docs/adr/0016). This is the only
            // coverage signal serving has — the true one needs a gold label — so the alarm
            // it raises is the judge's view, with the caveats CoverageMonitor documents.
            j.ifPresent(coverage::observe);
            c.filter(ClaimReport::certified).ifPresent(r -> watchFactuality(query, chunks, answer, r, j));
            // Never cache a judged-unsupported answer or an abstention — a semantic-cache hit
            // replays the answer verbatim and skips re-judging, making a hallucination (or a
            // sampling-artifact "I don't know") sticky across near-duplicate questions. The same
            // goes for an answer cut at max_tokens or filtered by the provider: one unlucky
            // generation would otherwise be the answer to every paraphrase until it expired.
            // An answer with withheld claims is not cached either: the cache stores text, and
            // replaying it would replay the claims the filter removed.
            boolean cacheable = finish.cacheable()
                    && !answer.contains(IDK)
                    && c.map(r -> r.withheld() == 0).orElse(true)
                    && j.map(v -> v.verdict() != Judgment.Verdict.UNSUPPORTED).orElse(true);
            return Flux.concat(
                    j.map(v -> Flux.just(sse("groundedness", toJson(v)))).orElseGet(Flux::empty),
                    c.map(r -> Flux.just(sse("claims", toJson(r)))).orElseGet(Flux::empty),
                    (cacheable ? cache.put(vector, answer, query) : Mono.<Void>empty())
                            .then(queryLog.record(query, "ask", false, chunks.size(), answer.length(),
                                    elapsedMs(start), j.map(Judgment::score).orElse(null)))
                            .thenMany(Flux.just(sse("done", ""))));
        });

        return Flux.concat(judging, rest);
    }

    /**
     * Feed the claim certificate's monitor (docs/adr/0017) the judge's verdict on the text the
     * reader was left with. Nothing withheld: that is the full answer, already judged. All
     * withheld: no claim is left to be wrong, which the calibration also counts as factual.
     * Otherwise the remaining text is judged once more — detached from the response, because
     * the reader is not waiting on a number only the alarm reads. The judge is fail-open and
     * bounded, so a detached call cannot pile up past its own timeout.
     */
    private void watchFactuality(String query, List<RetrievedChunk> chunks, String answer,
                                 ClaimReport report, Optional<Judgment> fullVerdict) {
        if (!factuality.enabled()) {
            return;
        }
        if (report.withheld() == 0) {
            fullVerdict.ifPresent(factuality::observe);
            return;
        }
        String kept = report.keptText(answer);
        if (kept.isBlank()) {
            factuality.observe(Judgment.Verdict.SUPPORTED.toJudgment());
            return;
        }
        judge.judge(query, chunks, kept).subscribe(
                verdict -> verdict.ifPresent(factuality::observe),
                e -> { /* fail-open: an unjudged answer is simply not an observation */ });
    }

    private static long elapsedMs(long startNanos) {
        return (System.nanoTime() - startNanos) / 1_000_000;
    }

    private String buildPrompt(String query, List<RetrievedChunk> chunks) {
        StringBuilder sb = new StringBuilder("Context passages:\n\n");
        for (int i = 0; i < chunks.size(); i++) {
            sb.append('[').append(i + 1).append("] (").append(chunks.get(i).source()).append(")\n")
                    .append(chunks.get(i).content()).append("\n\n");
        }
        sb.append("Question: ").append(query).append('\n');
        return sb.toString();
    }

    private String toJson(Object o) {
        try {
            return json.writeValueAsString(o);
        } catch (Exception e) {
            return "[]";
        }
    }

    private ServerSentEvent<String> sse(String event, String data) {
        return ServerSentEvent.<String>builder().event(event).data(data).build();
    }
}
