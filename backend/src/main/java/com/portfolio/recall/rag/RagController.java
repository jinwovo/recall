package com.portfolio.recall.rag;

import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;

@RestController
@RequestMapping("/api/ask")
public class RagController {

    private final RagService service;

    public RagController(RagService service) {
        this.service = service;
    }

    /** Streamed RAG answer. Consume with EventSource on the frontend. */
    @GetMapping(produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> ask(@RequestParam("q") String q) {
        return service.ask(q);
    }
}
