package com.portfolio.recall.ingestion;

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.validation.Validation;
import jakarta.validation.Validator;
import jakarta.validation.ValidatorFactory;
import java.util.Set;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * {@code docId} is not only an identifier — {@code RawDocumentStore} concatenates it into
 * {@code docs/<docId>/raw.txt}. An id carrying a separator writes outside the intended
 * one-prefix-per-document layout, and two different ids can collide onto a single key where
 * the second silently overwrites the first archive.
 *
 * <p>The constraint is a denylist on purpose, so these tests are mostly about what has to
 * keep working: {@code scripts/ingest_folder.py} slugs file paths into ids containing
 * Hangul, and the BEIR corpora use bare numbers. An allowlist tight enough to feel safe
 * would reject both.
 */
class IngestionEventValidationTest {

    private static final String NEWLINE = Character.toString(10);
    private static final String TAB = Character.toString(9);

    private static ValidatorFactory factory;
    private static Validator validator;

    @BeforeAll
    static void openValidator() {
        factory = Validation.buildDefaultValidatorFactory();
        validator = factory.getValidator();
    }

    @AfterAll
    static void closeValidator() {
        factory.close();
    }

    private Set<String> violatedFieldsFor(String docId) {
        return validator.validate(new IngestionEvent(docId, "src", "en", "some content"))
                .stream()
                .map(v -> v.getPropertyPath().toString())
                .collect(Collectors.toSet());
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "4983",                       // BEIR corpora
            "adr-0013-conformal",         // ingest_folder slug
            "docs-아키텍처-개요",           // ingest_folder slug, Hangul
            "a.b.c",                      // dots, but not a traversal
            "doc:1",
            "UPPER_and_lower-123",
            "doc id",                     // a space is not dangerous in a key
    })
    void idsThatRealIngestionProducesAreAccepted(String docId) {
        assertThat(violatedFieldsFor(docId)).isEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "../secrets",                 // climbs out of the prefix
            "a/../b",                     // climbs out mid-key
            "nested/doc",                 // splits the prefix in two
            "back\\slash",                // the separator on the other platform
            "..",
    })
    void idsThatWouldEscapeOrCollideOnTheArchiveKeyAreRejected(String docId) {
        assertThat(violatedFieldsFor(docId)).contains("docId");
    }

    @Test
    void controlCharactersAreRejected() {
        assertThat(violatedFieldsFor("doc" + NEWLINE + "id")).contains("docId");
        assertThat(violatedFieldsFor("doc" + TAB + "id")).contains("docId");
    }

    @Test
    void anUnboundedIdIsRejectedBeforeItBecomesAnObjectKey() {
        assertThat(violatedFieldsFor("d".repeat(201))).contains("docId");
        assertThat(violatedFieldsFor("d".repeat(200))).isEmpty();
    }

    @Test
    void blankStaysRejectedAsItAlreadyWas() {
        assertThat(violatedFieldsFor("  ")).contains("docId");
    }
}
