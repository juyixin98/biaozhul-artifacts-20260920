package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.Conflict;
import com.example.eventorder.model.OrderRequest;
import java.util.List;
import org.junit.jupiter.api.Test;

class TimeWindowCheckerTest {

    private static List<Conflict> timeConflictsOf(OrderRequest request) {
        ValidatedInput input = RequestValidator.validate(request);
        DependencyGraph graph = DependencyGraph.build(input);
        List<String> order = TopologicalOrderer.lexicographicOrder(graph);
        return TimeWindowChecker.check(input, graph, order);
    }

    @Test
    void directContradictionProducesTwoNodeChain() {
        List<Conflict> conflicts = timeConflictsOf(TestFixtures.contradictoryTime());
        assertEquals(1, conflicts.size());
        Conflict conflict = conflicts.get(0);
        assertEquals("TIME_CONTRADICTION", conflict.kind());
        assertEquals(List.of("a", "b"), conflict.chain());
        assertTrue(conflict.detail().contains("2026-01-01T12:00:00Z"));
        assertTrue(conflict.detail().contains("2026-01-01T11:00:00Z"));
    }

    @Test
    void propagatedContradictionTracesFullChain() {
        // a's earliest 12:00 propagates a -> b -> c; c's latest 11:00 is violated.
        List<Conflict> conflicts = timeConflictsOf(TestFixtures.contradictoryTimeChain());
        assertEquals(1, conflicts.size());
        assertEquals(List.of("a", "b", "c"), conflicts.get(0).chain());
    }

    @Test
    void eventWithInvertedOwnWindowIsContradiction() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")),
                List.of());
        List<Conflict> conflicts = timeConflictsOf(request);
        assertEquals(1, conflicts.size());
        assertEquals(List.of("a"), conflicts.get(0).chain());
    }

    @Test
    void feasibleChainHasNoConflict() {
        OrderRequest request = TestFixtures.request(
                List.of(
                        TestFixtures.event("a", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"),
                        TestFixtures.event("b", "2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z")),
                List.of(TestFixtures.dep("a", "b")));
        assertTrue(timeConflictsOf(request).isEmpty());
    }

    @Test
    void touchingBoundsAreFeasible() {
        // a.latest == b.earliest: t(a) <= t(b) can hold with equality.
        OrderRequest request = TestFixtures.request(
                List.of(
                        TestFixtures.event("a", null, "2026-01-01T10:00:00Z"),
                        TestFixtures.event("b", "2026-01-01T10:00:00Z", null)),
                List.of(TestFixtures.dep("a", "b")));
        assertTrue(timeConflictsOf(request).isEmpty());
    }

    @Test
    void overlappingWindowsWithoutDependencyProduceNoConflict() {
        assertTrue(timeConflictsOf(TestFixtures.overlappingWindowsNoDependency()).isEmpty());
    }

    @Test
    void latestBoundPropagatesOnlyForwardAlongDependencies() {
        // b must follow a; b's latest 09:00 conflicts with a's earliest 10:00,
        // but c (unrelated, also latest 09:00) must NOT be reported.
        OrderRequest request = TestFixtures.request(
                List.of(
                        TestFixtures.event("a", "2026-01-01T10:00:00Z", null),
                        TestFixtures.event("b", null, "2026-01-01T09:00:00Z"),
                        TestFixtures.event("c", null, "2026-01-01T09:00:00Z")),
                List.of(TestFixtures.dep("a", "b")));
        List<Conflict> conflicts = timeConflictsOf(request);
        assertEquals(1, conflicts.size());
        assertEquals(List.of("a", "b"), conflicts.get(0).chain());
    }
}
