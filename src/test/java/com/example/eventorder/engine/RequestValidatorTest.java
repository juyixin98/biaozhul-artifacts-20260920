package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.DependencySpec;
import com.example.eventorder.model.EventSpec;
import com.example.eventorder.model.OrderOptions;
import com.example.eventorder.model.OrderRequest;
import java.util.List;
import org.junit.jupiter.api.Test;

class RequestValidatorTest {

    @Test
    void rejectsNullRequest() {
        assertThrows(RequestException.class, () -> RequestValidator.validate(null));
    }

    @Test
    void rejectsEmptyEventList() {
        OrderRequest request = new OrderRequest("r", List.of(), List.of(), null);
        RequestException e = assertThrows(RequestException.class,
                () -> RequestValidator.validate(request));
        assertTrue(e.getMessage().contains("at least one event"));
    }

    @Test
    void rejectsDuplicateEventIds() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("a")), List.of());
        RequestException e = assertThrows(RequestException.class,
                () -> RequestValidator.validate(request));
        assertTrue(e.getMessage().contains("duplicate event id"));
    }

    @Test
    void rejectsDependencyOnUnknownEvent() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a")),
                List.of(TestFixtures.dep("a", "ghost")));
        RequestException e = assertThrows(RequestException.class,
                () -> RequestValidator.validate(request));
        assertTrue(e.getMessage().contains("unknown event: ghost"));
    }

    @Test
    void rejectsDuplicateDependencies() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b")),
                List.of(TestFixtures.dep("a", "b"), TestFixtures.dep("a", "b")));
        assertThrows(RequestException.class, () -> RequestValidator.validate(request));
    }

    @Test
    void rejectsTimestampWithoutOffset() {
        OrderRequest request = TestFixtures.request(
                List.of(new EventSpec("a", "2026-01-01T09:00:00", null, null)), List.of());
        RequestException e = assertThrows(RequestException.class,
                () -> RequestValidator.validate(request));
        assertTrue(e.getMessage().contains("earliest"));
    }

    @Test
    void acceptsNumericOffsetAndConvertsToInstant() {
        OrderRequest request = TestFixtures.request(
                List.of(new EventSpec("a", "2026-01-01T17:00:00+08:00", null, null)), List.of());
        ValidatedInput input = assertDoesNotThrow(() -> RequestValidator.validate(request));
        assertEquals("2026-01-01T09:00:00Z",
                input.events().get("a").earliest().orElseThrow().toString());
    }

    @Test
    void rejectsOutOfRangeEnumerationCap() {
        OrderRequest request = new OrderRequest("r",
                List.of(TestFixtures.event("a")), List.of(),
                new OrderOptions(null, -5));
        assertThrows(RequestException.class, () -> RequestValidator.validate(request));
    }

    @Test
    void defaultsAreApplied() {
        OrderRequest request = new OrderRequest("r",
                List.of(TestFixtures.event("a")), null, null);
        ValidatedInput input = RequestValidator.validate(request);
        assertEquals(RequestValidator.DEFAULT_ENFORCE_VERSION_ORDER, input.enforceVersionOrder());
        assertEquals(RequestValidator.DEFAULT_MAX_ENUMERATED_ORDERS, input.maxEnumeratedOrders());
        assertTrue(input.dependencies().isEmpty());
    }

    @Test
    void nullDependencyFieldsRejected() {
        OrderRequest request = new OrderRequest("r",
                List.of(TestFixtures.event("a")),
                List.of(new DependencySpec("a", null, null)), null);
        assertThrows(RequestException.class, () -> RequestValidator.validate(request));
    }
}
