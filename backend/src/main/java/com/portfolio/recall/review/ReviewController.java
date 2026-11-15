package com.portfolio.recall.review;

import java.util.List;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * Admin surface for the active review queue (docs/adr/0018). JPA is blocking, so every call
 * runs on the bounded-elastic pool. Demo scope: unauthenticated like the DLQ admin — in
 * production this sits behind the gateway's authn/authz.
 */
@RestController
@RequestMapping("/api/admin/review")
public class ReviewController {

    private static final int MAX_PAGE = 100;

    private final ReviewQueue queue;

    public ReviewController(ReviewQueue queue) {
        this.queue = queue;
    }

    @GetMapping
    public Mono<List<ReviewQueue.PendingItem>> pending(
            @RequestParam(value = "limit", defaultValue = "10") int limit) {
        int bounded = Math.max(1, Math.min(limit, MAX_PAGE));
        return Mono.fromCallable(() -> queue.pending(bounded)).subscribeOn(Schedulers.boundedElastic());
    }

    @PostMapping("/{id}")
    public Mono<Void> label(@PathVariable long id, @RequestBody ReviewQueue.LabelRequest request) {
        return Mono.fromRunnable(() -> queue.label(id, request)).subscribeOn(Schedulers.boundedElastic()).then();
    }

    @GetMapping("/summary")
    public Mono<ReviewQueue.Summary> summary() {
        return Mono.fromCallable(queue::summary).subscribeOn(Schedulers.boundedElastic());
    }

    /**
     * The ledger for {@code python eval/active.py}. By default only the labelled prefix;
     * {@code all=true} includes rows past the first unlabelled one, which the estimator
     * refuses on purpose.
     */
    @GetMapping(value = "/export", produces = "application/x-ndjson")
    public Mono<String> export(@RequestParam(value = "all", defaultValue = "false") boolean all) {
        return Mono.fromCallable(() -> queue.export(all)).subscribeOn(Schedulers.boundedElastic());
    }
}
