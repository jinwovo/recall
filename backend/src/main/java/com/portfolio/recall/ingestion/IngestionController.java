package com.portfolio.recall.ingestion;

import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

@RestController
@RequestMapping("/api/ingest")
public class IngestionController {

    private final IngestionService service;

    public IngestionController(IngestionService service) {
        this.service = service;
    }

    /**
     * Accepts a document and returns 202; indexing happens async (docs/adr/0003). Enqueueing
     * blocks on MinIO + the broker ack (docs/adr/0005), so it runs off the event loop.
     */
    @PostMapping
    public Mono<ResponseEntity<Ack>> ingest(@Valid @RequestBody IngestionEvent event) {
        return Mono.fromCallable(() -> {
                    service.enqueue(event);
                    return ResponseEntity.accepted().body(new Ack(event.docId(), "queued"));
                })
                .subscribeOn(Schedulers.boundedElastic());
    }

    public record Ack(String docId, String status) {}
}
