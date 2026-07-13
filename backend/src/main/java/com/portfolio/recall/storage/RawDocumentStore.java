package com.portfolio.recall.storage;

import com.portfolio.recall.config.RecallProperties;
import io.minio.BucketExistsArgs;
import io.minio.GetObjectArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import io.minio.errors.ErrorResponseException;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.stereotype.Component;

/**
 * Raw-document archive on MinIO/S3 (docs/adr/0005). Every accepted document is stored here
 * before its Kafka event is published: the object is the claim-check payload for large
 * documents and the re-indexing source of truth for all of them.
 *
 * <p>The bucket is created lazily on first use so the app can boot while MinIO is down;
 * failures surface as {@link RawStorageException} and follow the retry/DLQ policy.
 */
@Component
public class RawDocumentStore {

    private final MinioClient minio;
    private final String bucket;
    private final AtomicBoolean bucketReady = new AtomicBoolean(false);

    public RawDocumentStore(MinioClient minio, RecallProperties props) {
        this.minio = minio;
        this.bucket = props.storage().bucket();
    }

    /** Stores the raw content and returns its object key ({@code docs/<docId>/raw.txt}). */
    public String store(String docId, String content) {
        String key = objectKey(docId);
        byte[] bytes = content.getBytes(StandardCharsets.UTF_8);
        try {
            ensureBucket();
            minio.putObject(PutObjectArgs.builder()
                    .bucket(bucket).object(key)
                    .stream(new ByteArrayInputStream(bytes), bytes.length, -1)
                    .contentType("text/plain; charset=utf-8")
                    .build());
            return key;
        } catch (Exception e) {
            throw new RawStorageException("Failed to archive raw doc " + docId, e);
        }
    }

    /** Fetches raw content by object key — the consumer side of the claim check. */
    public String fetch(String objectKey) {
        try (InputStream in = minio.getObject(
                GetObjectArgs.builder().bucket(bucket).object(objectKey).build())) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new RawStorageException("Failed to fetch raw object " + objectKey, e);
        }
    }

    public static String objectKey(String docId) {
        return "docs/" + docId + "/raw.txt";
    }

    private void ensureBucket() throws Exception {
        if (bucketReady.get()) {
            return;
        }
        synchronized (this) {
            if (bucketReady.get()) {
                return;
            }
            if (!minio.bucketExists(BucketExistsArgs.builder().bucket(bucket).build())) {
                try {
                    minio.makeBucket(MakeBucketArgs.builder().bucket(bucket).build());
                } catch (ErrorResponseException e) {
                    // Another instance won the race — that's fine, the bucket exists.
                    if (!"BucketAlreadyOwnedByYou".equals(e.errorResponse().code())) {
                        throw e;
                    }
                }
            }
            bucketReady.set(true);
        }
    }

    /** Wraps MinIO's many checked exceptions; classified as retryable by the DLQ error handler. */
    public static class RawStorageException extends RuntimeException {
        public RawStorageException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
