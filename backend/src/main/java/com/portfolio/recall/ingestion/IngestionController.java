package com.portfolio.recall.ingestion;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/ingest")
public class IngestionController {

    private final IngestionService service;

    public IngestionController(IngestionService service) {
        this.service = service;
    }

    /** Accepts a document and returns 202 immediately; indexing happens async (docs/adr/0003). */
    @PostMapping
    public ResponseEntity<Ack> ingest(@RequestBody IngestionEvent event) {
        service.enqueue(event);
        return ResponseEntity.accepted().body(new Ack(event.docId(), "queued"));
    }

    public record Ack(String docId, String status) {}
}
