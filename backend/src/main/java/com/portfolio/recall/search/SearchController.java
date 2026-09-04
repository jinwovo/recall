package com.portfolio.recall.search;

import jakarta.validation.constraints.Size;
import java.util.List;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

@RestController
@Validated
@RequestMapping("/api/search")
public class SearchController {

    /**
     * Upper bound on a query, shared with the RAG endpoint.
     *
     * <p>Every retrieval path prices the query by its length twice over: once to embed it,
     * and again inside the cross-encoder, which scores the query against each of
     * {@code candidates} passages. On a CPU reranker that second cost is the dominant one in
     * the whole system, so an unbounded {@code q} is a way to buy a large amount of the
     * machine's time with a single GET. A real question is a small fraction of this;
     * the bound exists to have one at all.
     */
    public static final int MAX_QUERY_CHARS = 1000;

    private final SearchService service;

    public SearchController(SearchService service) {
        this.service = service;
    }

    /**
     * {@code mode} is bm25 | vector | hybrid (default) | hyde; {@code rerank} applies to
     * hybrid only: cross-encoder (default) | m3 (docs/adr/0008). Both case-insensitive
     * (see CorsConfig converters). {@code rrfK} / {@code candidates} are clamped tuning
     * overrides used by the self-tuning sweep (docs/adr/0010).
     */
    @GetMapping
    public Mono<SearchResponse> search(
            @RequestParam("q") @Size(max = MAX_QUERY_CHARS) String q,
            @RequestParam(value = "mode", defaultValue = "HYBRID") SearchMode mode,
            @RequestParam(value = "rerank", defaultValue = "CROSS_ENCODER") RerankStrategy rerank,
            @RequestParam(value = "rrfK", required = false) Integer rrfK,
            @RequestParam(value = "candidates", required = false) Integer candidates) {
        return service.search(q, mode, rerank, TuningOverrides.of(rrfK, candidates))
                .map(results -> new SearchResponse(q, mode.name().toLowerCase(), results));
    }

    public record SearchResponse(String query, String mode, List<RetrievedChunk> results) {}
}
