package com.portfolio.recall.admin;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * Admin surface for the ingestion DLQ (docs/adr/0006): inspect → fix the fault → replay.
 * Kafka consumer calls are blocking, so they run on the bounded-elastic pool, off the
 * WebFlux event loop. Demo scope: unauthenticated like the rest of the API — in production
 * this sits behind the gateway's authn/authz.
 */
@RestController
@RequestMapping("/api/admin/dlq")
public class DlqAdminController {

    private static final int MAX_PEEK = 200;
    private static final int MAX_REPLAY = 500;

    private final DlqAdminService service;

    public DlqAdminController(DlqAdminService service) {
        this.service = service;
    }

    @GetMapping
    public Mono<DlqSnapshot> inspect(@RequestParam(value = "limit", defaultValue = "50") int limit) {
        int bounded = clamp(limit, MAX_PEEK);
        return Mono.fromCallable(() -> service.inspect(bounded))
                .subscribeOn(Schedulers.boundedElastic());
    }

    @PostMapping("/replay")
    public Mono<DlqReplayResult> replay(@RequestParam(value = "max", defaultValue = "200") int max) {
        int bounded = clamp(max, MAX_REPLAY);
        return Mono.fromCallable(() -> service.replay(bounded))
                .subscribeOn(Schedulers.boundedElastic());
    }

    private static int clamp(int value, int max) {
        return Math.max(1, Math.min(value, max));
    }
}
