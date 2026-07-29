package com.portfolio.recall.search;

import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

@RestController
@RequestMapping("/api/search")
public class SearchController {

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
            @RequestParam("q") String q,
            @RequestParam(value = "mode", defaultValue = "HYBRID") SearchMode mode,
            @RequestParam(value = "rerank", defaultValue = "CROSS_ENCODER") RerankStrategy rerank,
            @RequestParam(value = "rrfK", required = false) Integer rrfK,
            @RequestParam(value = "candidates", required = false) Integer candidates) {
        return service.search(q, mode, rerank, TuningOverrides.of(rrfK, candidates))
                .map(results -> new SearchResponse(q, mode.name().toLowerCase(), results));
    }

    public record SearchResponse(String query, String mode, List<RetrievedChunk> results) {}
}
