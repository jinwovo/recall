package com.portfolio.recall.review;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.data.domain.PageRequest;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

/**
 * The reviewer's side of the ledger (docs/adr/0018): a blind queue, a label, an export.
 *
 * <p><b>Blind</b> means a reviewer sees the question, the passages and the answer, and
 * nothing a machine said about them: no judge verdict, no claim score, no inclusion
 * probability. A label given while looking at the judge's verdict agrees with the judge by
 * construction, and the estimate would be measuring the judge against itself.
 *
 * <p>The export is by default the <b>labelled prefix</b> of the ledger: every row older than
 * the oldest selected answer still waiting for a label. The queue is served oldest first, so
 * the prefix only grows, and stopping at it is a stopping time the anytime-valid estimate is
 * allowed to stop at. Exporting everything and dropping the unlabelled rows would not be: the
 * answers a reviewer skips are not a random sample of the ones selected.
 */
@Service
public class ReviewQueue {

    public record PendingItem(long id, Instant createdAt, String query, String answer,
                              List<Map<String, Object>> passages, List<String> claims) {}

    public record LabelRequest(Boolean supported, List<Boolean> claims) {}

    public record Summary(long judged, long selected, long labelled, long pending,
                         long exportable) {}

    private static final TypeReference<List<Map<String, Object>>> ROWS = new TypeReference<>() {};

    private final ReviewItemRepository repo;
    private final ObjectMapper json;

    public ReviewQueue(ReviewItemRepository repo, ObjectMapper json) {
        this.repo = repo;
        this.json = json;
    }

    public List<PendingItem> pending(int limit) {
        return repo.findBySelectedTrueAndSupportedIsNullOrderByIdAsc(PageRequest.of(0, limit))
                .stream()
                .map(i -> new PendingItem(i.getId(), i.getCreatedAt(), i.getQuery(), i.getAnswer(),
                        rows(i.getPassages()),
                        rows(i.getClaims()).stream().map(c -> String.valueOf(c.get("text"))).toList()))
                .toList();
    }

    @Transactional
    public void label(long id, LabelRequest request) {
        if (request == null || request.supported() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "'supported' must be true or false");
        }
        ReviewItem item = repo.findById(id)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "no review item " + id));
        if (!item.isSelected()) {
            // Labelling an answer the coin passed over would put it in the sample with a
            // weight it was never given — the one move that biases the estimate.
            throw new ResponseStatusException(HttpStatus.CONFLICT, "item " + id + " was not selected for review");
        }
        if (item.getSupported() != null) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "item " + id + " is already labelled");
        }
        String claimLabels = null;
        if (request.claims() != null) {
            int expected = rows(item.getClaims()).size();
            if (request.claims().size() != expected || request.claims().stream().anyMatch(java.util.Objects::isNull)) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                        "expected " + expected + " claim labels, each true or false");
            }
            claimLabels = write(request.claims());
        }
        item.label(request.supported(), claimLabels);
        repo.save(item);
    }

    public Summary summary() {
        long judged = repo.count();
        long selected = repo.countBySelectedTrue();
        long labelled = repo.countBySelectedTrueAndSupportedIsNotNull();
        long exportable = oldestPending().map(repo::countByIdLessThan)
                .orElse(judged);
        return new Summary(judged, selected, labelled, selected - labelled, exportable);
    }

    /** NDJSON, one row per judged answer, in the format {@code eval/active.py} reads. */
    public String export(boolean includeUnlabelled) {
        List<ReviewItem> rows = includeUnlabelled
                ? repo.findAllByOrderByIdAsc()
                : oldestPending().map(repo::findByIdLessThanOrderByIdAsc).orElseGet(repo::findAllByOrderByIdAsc);
        StringBuilder out = new StringBuilder();
        for (ReviewItem i : rows) {
            ObjectNode row = json.createObjectNode();
            row.put("id", i.getId());
            row.put("prediction", i.getPrediction());
            row.put("verdict", i.getVerdict());
            row.put("pi", i.getPi());
            row.put("selected", i.isSelected());
            if (i.getSupported() != null) {
                row.put("supported", i.getSupported());
                List<Map<String, Object>> claims = rows(i.getClaims());
                List<Boolean> labels = i.getClaimLabels() == null ? List.of()
                        : read(i.getClaimLabels(), new TypeReference<List<Boolean>>() {});
                if (!claims.isEmpty() && labels.size() == claims.size()) {
                    ArrayNode arr = row.putArray("claims");
                    for (int k = 0; k < claims.size(); k++) {
                        ObjectNode c = arr.addObject();
                        Object score = claims.get(k).get("score");
                        if (score instanceof Number n) c.put("score", n.doubleValue());
                        else c.putNull("score");
                        c.put("supported", labels.get(k));
                    }
                }
            }
            out.append(row).append('\n');
        }
        return out.toString();
    }

    private Optional<Long> oldestPending() {
        return repo.findFirstBySelectedTrueAndSupportedIsNullOrderByIdAsc().map(ReviewItem::getId);
    }

    private List<Map<String, Object>> rows(String text) {
        return text == null ? new ArrayList<>() : read(text, ROWS);
    }

    private <T> T read(String text, TypeReference<T> type) {
        try {
            return json.readValue(text, type);
        } catch (Exception e) {
            throw new IllegalStateException("corrupt review ledger JSON", e);
        }
    }

    private String write(Object o) {
        try {
            return json.writeValueAsString(o);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
