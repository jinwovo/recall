package com.portfolio.recall.common;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

class HashingTest {

    @Test
    void chunkIdIsDeterministic() {
        assertThat(Hashing.chunkId("doc", 0, "hello")).isEqualTo(Hashing.chunkId("doc", 0, "hello"));
    }

    @Test
    void chunkIdDiffersByContent() {
        assertThat(Hashing.chunkId("doc", 0, "a")).isNotEqualTo(Hashing.chunkId("doc", 0, "b"));
    }

    @Test
    void chunkIdDiffersByIndex() {
        assertThat(Hashing.chunkId("doc", 0, "a")).isNotEqualTo(Hashing.chunkId("doc", 1, "a"));
    }

    @Test
    void sha256IsLowercaseHex64() {
        assertThat(Hashing.sha256("x")).hasSize(64).matches("[0-9a-f]+");
    }
}
