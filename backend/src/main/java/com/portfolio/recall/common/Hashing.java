package com.portfolio.recall.common;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

/** Deterministic hashing for idempotency keys (docs/adr/0003). */
public final class Hashing {

    private Hashing() {}

    public static String sha256(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(s.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Stable per-chunk id used as the Elasticsearch {@code _id} so re-ingestion upserts. */
    public static String chunkId(String docId, int index, String content) {
        return sha256(docId + ":" + index + ":" + content);
    }
}
