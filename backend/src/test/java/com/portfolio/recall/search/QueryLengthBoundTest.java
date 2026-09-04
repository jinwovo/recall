package com.portfolio.recall.search;

import static org.assertj.core.api.Assertions.assertThat;

import com.portfolio.recall.rag.RagController;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import jakarta.validation.ValidatorFactory;
import jakarta.validation.executable.ExecutableValidator;
import java.lang.reflect.Method;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * Both query endpoints price {@code q} by its length twice: once to embed it, and again in
 * the cross-encoder, which scores it against every candidate passage. On a CPU reranker that
 * second cost dominates the whole system — a hybrid query was measured at ~163s against
 * SciFact — so an unbounded {@code q} lets one GET buy a large share of the machine.
 *
 * <p>The bound lives on the controller parameters, which Spring enforces through method
 * validation. That is what {@link ExecutableValidator} checks here, without standing up a
 * web context the rest of this suite does not use.
 */
class QueryLengthBoundTest {

    private static ValidatorFactory factory;
    private static ExecutableValidator executables;

    @BeforeAll
    static void openValidator() {
        factory = Validation.buildDefaultValidatorFactory();
        Validator validator = factory.getValidator();
        executables = validator.forExecutables();
    }

    @AfterAll
    static void closeValidator() {
        factory.close();
    }

    private int violationsFor(Class<?> controller, String method, Object[] args) throws Exception {
        Method m = null;
        for (Method candidate : controller.getMethods()) {
            if (candidate.getName().equals(method)) {
                m = candidate;
            }
        }
        assertThat(m).as("method %s on %s", method, controller.getSimpleName()).isNotNull();
        // Parameter validation reads annotations only, so the instance never has to work —
        // a controller built on a null service is enough to hang the constraints off.
        Object instance = controller.getDeclaredConstructors()[0].newInstance(new Object[] {null});
        return executables.validateParameters(instance, m, args).size();
    }

    @Test
    void aQueryAtTheBoundIsAccepted() throws Exception {
        String q = "a".repeat(SearchController.MAX_QUERY_CHARS);
        assertThat(violationsFor(SearchController.class, "search",
                new Object[] {q, SearchMode.HYBRID, RerankStrategy.CROSS_ENCODER, null, null}))
                .isZero();
    }

    @Test
    void aQueryPastTheBoundIsRejectedBeforeAnyEmbeddingIsPaidFor() throws Exception {
        String q = "a".repeat(SearchController.MAX_QUERY_CHARS + 1);
        assertThat(violationsFor(SearchController.class, "search",
                new Object[] {q, SearchMode.HYBRID, RerankStrategy.CROSS_ENCODER, null, null}))
                .isEqualTo(1);
    }

    @Test
    void theRagEndpointCarriesTheSameBound() throws Exception {
        // The RAG path runs the same retrieval and then pays for the query again in the
        // prompt, so a laxer bound there would defeat the one on search.
        assertThat(violationsFor(RagController.class, "ask",
                new Object[] {"a".repeat(SearchController.MAX_QUERY_CHARS)}))
                .isZero();
        assertThat(violationsFor(RagController.class, "ask",
                new Object[] {"a".repeat(SearchController.MAX_QUERY_CHARS + 1)}))
                .isEqualTo(1);
    }
}
