package com.portfolio.recall;

import org.testcontainers.utility.DockerImageName;

/**
 * Every registry image the integration tests pull, in one place. scripts/check_images.py reads
 * the string literals in this file, so a test image added anywhere else escapes the
 * availability check. Keep image references here.
 */
public final class TestImages {

    public static final String KAFKA = "apache/kafka:3.9.2";

    /** Stock ES; IngestionIdempotencyIT builds Nori into it, mirroring infra/elasticsearch. */
    public static final String ELASTICSEARCH = "docker.elastic.co/elasticsearch/elasticsearch:8.19.26";

    /**
     * MinIO stopped publishing community images: {@code minio/minio} is gone from Docker Hub and
     * quay.io, so a cold runner cannot pull it. Chainguard's build is a drop-in (entrypoint is the
     * {@code minio} binary). Its free tier tags only {@code latest}, so it is pinned by digest,
     * the same digest as docker-compose.yml. Bump both together.
     */
    public static final DockerImageName MINIO = DockerImageName
            .parse("cgr.dev/chainguard/minio@sha256:b9b1cce81a4a6cc9bf250e68d621cc0b9fb8ed15aefe18fcd1840525eab6a81a")
            .asCompatibleSubstituteFor("minio/minio");

    private TestImages() {}
}
