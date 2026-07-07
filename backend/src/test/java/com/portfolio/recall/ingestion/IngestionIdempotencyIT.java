package com.portfolio.recall.ingestion;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.json.jackson.JacksonJsonpMapper;
import co.elastic.clients.transport.rest_client.RestClientTransport;
import com.portfolio.recall.common.Hashing;
import com.portfolio.recall.config.RecallProperties;
import com.portfolio.recall.search.ChunkDocument;
import com.portfolio.recall.search.ElasticsearchDocumentIndex;
import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.http.HttpHost;
import org.elasticsearch.client.RestClient;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.elasticsearch.ElasticsearchContainer;
import org.testcontainers.images.builder.ImageFromDockerfile;
import org.testcontainers.utility.DockerImageName;

/**
 * Validates ADR 0003: re-ingesting the same content must NOT create duplicate chunks.
 * The ES {@code _id} is {@link Hashing#chunkId} (same as {@link IngestionConsumer}), so N
 * identical upserts — including concurrent ones — converge to a single document.
 *
 * <p>Runs the same image recipe as production (ES 8.15 + analysis-nori, see
 * infra/elasticsearch/Dockerfile) so {@link ElasticsearchDocumentIndex#ensureIndex()} is
 * exercised as-is, Korean analyzer included. Skipped automatically when Docker is unavailable.
 */
@Tag("integration")
class IngestionIdempotencyIT {

    private static final String ES_BASE_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.15.0";
    private static final int EMBEDDING_DIM = 8;
    private static final AtomicInteger INDEX_SEQ = new AtomicInteger();

    private static ElasticsearchContainer es;
    private static RestClient restClient;
    private static ElasticsearchClient client;

    private ElasticsearchDocumentIndex index;
    private String indexName;

    @BeforeAll
    static void startElasticsearch() throws Exception {
        assumeTrue(DockerClientFactory.instance().isDockerAvailable(), "Docker required for this IT");

        // Stock ES images ship without Nori — build the plugin in, mirroring infra/elasticsearch.
        String noriImage = new ImageFromDockerfile("recall-es-nori-it:8.15.0", false)
                .withDockerfileFromBuilder(b -> b
                        .from(ES_BASE_IMAGE)
                        .run("bin/elasticsearch-plugin install --batch analysis-nori"))
                .get();

        es = new ElasticsearchContainer(DockerImageName.parse(noriImage)
                .asCompatibleSubstituteFor("docker.elastic.co/elasticsearch/elasticsearch"))
                .withEnv("xpack.security.enabled", "false")
                .withEnv("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
                .withStartupTimeout(Duration.ofMinutes(3));
        es.start();

        String host = es.getHttpHostAddress();
        restClient = RestClient
                .builder(HttpHost.create(host.startsWith("http") ? host : "http://" + host))
                .build();
        client = new ElasticsearchClient(new RestClientTransport(restClient, new JacksonJsonpMapper()));
    }

    @AfterAll
    static void stopElasticsearch() throws IOException {
        if (restClient != null) {
            restClient.close();
        }
        if (es != null) {
            es.stop();
        }
    }

    @BeforeEach
    void freshIndex() {
        indexName = "recall-it-" + INDEX_SEQ.incrementAndGet();
        index = new ElasticsearchDocumentIndex(client, propsFor(indexName));
        index.ensureIndex();
        index.ensureIndex(); // ensureIndex on an existing index must be a no-op, not an error
    }

    @Test
    void concurrentDuplicateUpsertsConvergeToSingleDocument() throws Exception {
        ChunkDocument doc = chunk("doc-1", 0,
                "스프링 부트는 자동 설정을 제공한다. Spring Boot provides auto-configuration.");

        int threads = 8;
        int totalUpserts = 200;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            List<Callable<Void>> jobs = new ArrayList<>();
            for (int i = 0; i < totalUpserts; i++) {
                jobs.add(() -> {
                    index.upsert(doc);
                    return null;
                });
            }
            for (Future<Void> f : pool.invokeAll(jobs)) {
                f.get(); // surface any upsert failure as a test failure
            }
        } finally {
            pool.shutdownNow();
        }

        refresh();
        assertThat(countByHash(doc.contentHash())).isEqualTo(1);
        assertThat(countAll()).isEqualTo(1);
    }

    @Test
    void changedContentIndexesAsNewDocumentWithoutDuplicatingOld() throws IOException {
        ChunkDocument v1 = chunk("doc-1", 0, "쿠버네티스 파드는 하나 이상의 컨테이너를 실행한다.");
        ChunkDocument v2 = chunk("doc-1", 0, "쿠버네티스 파드는 하나 이상의 컨테이너를 실행한다. (개정판)");

        index.upsert(v1);
        index.upsert(v2);
        index.upsert(v1); // unchanged content re-ingested → no duplicate

        refresh();
        assertThat(countByHash(v1.contentHash())).isEqualTo(1);
        assertThat(countByHash(v2.contentHash())).isEqualTo(1);
        assertThat(countAll()).isEqualTo(2);
    }

    @Test
    void koreanAnalyzerIsActive() throws IOException {
        // '자동차를' must reduce to the noun '자동차' — only a morphological analyzer (Nori)
        // does that; standard tokenization would keep '자동차를' and miss the query.
        index.upsert(chunk("doc-ko", 0, "자동차를 구매했다"));
        refresh();
        assertThat(index.bm25("자동차", 5)).hasSize(1);
    }

    private ChunkDocument chunk(String docId, int chunkIndex, String content) {
        float[] embedding = new float[EMBEDDING_DIM];
        embedding[0] = 1f; // cosine similarity needs a non-zero vector
        String hash = Hashing.chunkId(docId, chunkIndex, content); // same key as IngestionConsumer
        return new ChunkDocument(docId, chunkIndex, content, "it://" + docId, "ko", hash, embedding);
    }

    private long countByHash(String hash) throws IOException {
        return client.count(c -> c.index(indexName)
                .query(q -> q.term(t -> t.field("contentHash").value(hash)))).count();
    }

    private long countAll() throws IOException {
        return client.count(c -> c.index(indexName)).count();
    }

    private void refresh() throws IOException {
        client.indices().refresh(r -> r.index(indexName));
    }

    private static RecallProperties propsFor(String indexName) {
        return new RecallProperties(
                new RecallProperties.Elasticsearch("unused", indexName),
                new RecallProperties.Embedding("unused", EMBEDDING_DIM),
                null, null, null, null, null, null);
    }
}
