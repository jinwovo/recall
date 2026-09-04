package com.portfolio.recall.rag;

import com.portfolio.recall.search.SearchController;
import jakarta.validation.constraints.Size;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;

@RestController
@Validated
@RequestMapping("/api/ask")
public class RagController {

    private final RagService service;

    public RagController(RagService service) {
        this.service = service;
    }

    /**
     * Streamed RAG answer. Consume with EventSource on the frontend.
     *
     * <p>Bounded by the same {@link SearchController#MAX_QUERY_CHARS} as plain search: this
     * path runs the same embedding and cross-encoder work before it reaches the LLM, and
     * then pays for the query again in the prompt.
     */
    @GetMapping(produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> ask(
            @RequestParam("q") @Size(max = SearchController.MAX_QUERY_CHARS) String q) {
        return service.ask(q);
    }
}
