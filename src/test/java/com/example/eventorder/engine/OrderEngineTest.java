package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.DependencyCheck;
import com.example.eventorder.model.OrderRequest;
import com.example.eventorder.model.OrderResponse;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Acceptance-level scenarios, run end to end through the engine:
 * circular dependencies, contradictory time constraints, multiple legal
 * orders, per-dependency verification and minimal readable conflict chains.
 */
class OrderEngineTest {

    private final OrderEngine engine = new OrderEngine("test-tzdb");

    @Test
    void cyclicDependenciesAreUnsatisfiableWithMinimalChain() {
        OrderResponse response = engine.process(TestFixtures.cyclicDependencies());
        assertFalse(response.satisfiable());
        assertNull(response.order());
        assertEquals(0, response.validOrderCount());
        assertEquals(1, response.conflicts().size());
        assertEquals("CYCLE", response.conflicts().get(0).kind());
        assertEquals(List.of("a", "b", "c", "a"), response.conflicts().get(0).chain());
        // every requested dependency is reported as not satisfied
        assertEquals(3, response.dependencyChecks().size());
        assertTrue(response.dependencyChecks().stream().noneMatch(DependencyCheck::satisfied));
    }

    @Test
    void contradictoryTimeConstraintsAreUnsatisfiableWithReadableChain() {
        OrderResponse response = engine.process(TestFixtures.contradictoryTimeChain());
        assertFalse(response.satisfiable());
        assertEquals(1, response.conflicts().size());
        assertEquals("TIME_CONTRADICTION", response.conflicts().get(0).kind());
        assertEquals(List.of("a", "b", "c"), response.conflicts().get(0).chain());
        String detail = response.conflicts().get(0).detail();
        assertTrue(detail.contains("2026-01-01T12:00:00Z"), detail);
        assertTrue(detail.contains("2026-01-01T11:00:00Z"), detail);
    }

    @Test
    void multipleLegalOrdersAreCountedAndEnumerated() {
        OrderResponse response = engine.process(TestFixtures.multipleLegalOrders());
        assertTrue(response.satisfiable());
        assertEquals(List.of("a", "b", "c"), response.order());
        assertEquals(3, response.validOrderCount());
        assertEquals(3, response.enumeratedOrders().size());
        assertTrue(response.enumeratedOrders().containsAll(List.of(
                List.of("a", "b", "c"), List.of("a", "c", "b"), List.of("b", "a", "c"))));
        // the emitted deterministic order is one of the valid orders
        assertTrue(response.enumeratedOrders().contains(response.order()));
    }

    @Test
    void everyDependencyIsVerified() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("build"), TestFixtures.event("test"),
                        TestFixtures.event("deploy")),
                List.of(TestFixtures.dep("build", "test"), TestFixtures.dep("test", "deploy")));
        OrderResponse response = engine.process(request);
        assertTrue(response.satisfiable());
        assertEquals(2, response.dependencyChecks().size());
        assertEquals(new DependencyCheck("build", "test", true), response.dependencyChecks().get(0));
        assertEquals(new DependencyCheck("test", "deploy", true), response.dependencyChecks().get(1));
    }

    @Test
    void overlappingWindowsDoNotCreateCausality() {
        OrderResponse response = engine.process(TestFixtures.overlappingWindowsNoDependency());
        assertTrue(response.satisfiable());
        // both directions stay legal: overlap did not force any edge
        assertEquals(2, response.validOrderCount());
        assertTrue(response.conflicts().isEmpty());
    }

    @Test
    void disjointWindowsDoNotCreateCausalityEither() {
        OrderResponse response = engine.process(TestFixtures.disjointWindowsNoDependency());
        assertTrue(response.satisfiable());
        assertEquals(2, response.validOrderCount());
        assertTrue(response.enumeratedOrders().contains(List.of("y", "x")));
    }

    @Test
    void versionContradictionIsUnsatisfiable() {
        OrderResponse response = engine.process(TestFixtures.versionContradiction());
        assertFalse(response.satisfiable());
        assertEquals("VERSION_CONTRADICTION", response.conflicts().get(0).kind());
    }

    @Test
    void tzdbVersionIsRecordedInResponse() {
        OrderResponse response = engine.process(TestFixtures.multipleLegalOrders());
        assertEquals("test-tzdb", response.tzdbVersion());
    }

    @Test
    void detectedTzdbVersionLooksLikeIanaRelease() {
        String version = TzdbVersion.detect();
        assertNotNull(version);
        assertTrue(version.matches("\\d{4}[a-z]"), "unexpected tzdb version: " + version);
    }

    @Test
    void requestIdIsEchoed() {
        OrderResponse response = engine.process(TestFixtures.multipleLegalOrders());
        assertEquals("test-req", response.requestId());
    }
}
