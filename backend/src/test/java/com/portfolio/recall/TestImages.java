package com.portfolio.recall;

import org.testcontainers.utility.DockerImageName;

/** Container images shared by the integration tests, pinned in one place. */
public final class TestImages {

    /**
     * MinIO stopped publishing community images: {@code minio/minio} is gone from Docker Hub and
     * quay.io, so a cold runner cannot pull it. Chainguard's build is a drop-in (entrypoint is the
     * {@code minio} binary). Its free tier tags only {@code latest}, so it is pinned by digest —
     * the same digest as docker-compose.yml; bump both together.
     */
    public static final DockerImageName MINIO = DockerImageName
            .parse("cgr.dev/chainguard/minio@sha256:f6243be11aaaa2fe654d260558e2b3fcef50135e78a3abfd0db802b13444f059")
            .asCompatibleSubstituteFor("minio/minio");

    private TestImages() {}
}
