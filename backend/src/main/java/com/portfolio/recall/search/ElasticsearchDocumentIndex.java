package com.portfolio.recall.search;

import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch.core.SearchResponse;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.portfolio.recall.config.RecallProperties;
import java.io.IOException;
import java.io.StringReader;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * Elasticsearch-backed {@link DocumentIndex}. Field names are camelCase to match the Jackson
 * serialization of {@link ChunkDocument}. (ES client API is version-sensitive — if a builder
 * signature differs in your ES client version, this is the only file to adjust.)
 */
@Component
public class ElasticsearchDocumentIndex implements DocumentIndex {

    private static final String MAPPING_TEMPLATE = """
            {
              "settings": { "analysis": { "analyzer": { "korean": { "type": "nori" } } } },
              "mappings": {
                "properties": {
                  "docId":       { "type": "keyword" },
                  "chunkIndex":  { "type": "integer" },
                  "content":     { "type": "text", "analyzer": "korean" },
                  "source":      { "type": "keyword" },
                  "lang":        { "type": "keyword" },
                  "contentHash": { "type": "keyword" },
                  "embedding":   { "type": "dense_vector", "dims": __DIM__, "index": true, "similarity": "cosine" }
                }
              }
            }
            """;

    private final ElasticsearchClient es;
    private final String index;
    private final int dim;

    public ElasticsearchDocumentIndex(ElasticsearchClient es, RecallProperties props) {
        this.es = es;
        this.index = props.elasticsearch().index();
        this.dim = props.embedding().dim();
    }

    @Override
    public void ensureIndex() {
        try {
            if (es.indices().exists(e -> e.index(index)).value()) {
                return;
            }
            String json = MAPPING_TEMPLATE.replace("__DIM__", Integer.toString(dim));
            es.indices().create(c -> c.index(index).withJson(new StringReader(json)));
        } catch (IOException e) {
            throw new UncheckedIOException("Failed to ensure ES index " + index, e);
        }
    }

    @Override
    public List<RetrievedChunk> bm25(String query, int size) {
        try {
            SearchResponse<ChunkSource> resp = es.search(s -> s
                    .index(index)
                    .size(size)
                    .query(q -> q.match(m -> m.field("content").query(query))), ChunkSource.class);
            return toChunks(resp);
        } catch (IOException e) {
            throw new UncheckedIOException("BM25 search failed", e);
        }
    }

    @Override
    public List<RetrievedChunk> knn(float[] queryVector, int size) {
        List<Float> qv = new ArrayList<>(queryVector.length);
        for (float v : queryVector) {
            qv.add(v);
        }
        try {
            SearchResponse<ChunkSource> resp = es.search(s -> s
                    .index(index)
                    .knn(k -> k
                            .field("embedding")
                            .queryVector(qv)
                            .k(size)
                            .numCandidates(Math.max(size * 4, 100))), ChunkSource.class);
            return toChunks(resp);
        } catch (IOException e) {
            throw new UncheckedIOException("kNN search failed", e);
        }
    }

    @Override
    public void upsert(ChunkDocument doc) {
        try {
            es.index(i -> i.index(index).id(doc.contentHash()).document(doc));
        } catch (IOException e) {
            throw new UncheckedIOException("Index upsert failed", e);
        }
    }

    private List<RetrievedChunk> toChunks(SearchResponse<ChunkSource> resp) {
        return resp.hits().hits().stream().map(h -> {
            ChunkSource s = h.source();
            double score = h.score() == null ? 0.0 : h.score();
            return new RetrievedChunk(
                    h.id(),
                    s == null ? null : s.docId(),
                    s == null ? 0 : s.chunkIndex(),
                    s == null ? "" : s.content(),
                    s == null ? null : s.source(),
                    s == null ? null : s.lang(),
                    score);
        }).toList();
    }

    /** Read-side projection (no embedding) for deserializing hits. */
    @JsonIgnoreProperties(ignoreUnknown = true)
    record ChunkSource(String docId, int chunkIndex, String content, String source, String lang) {}
}
