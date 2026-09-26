package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.Conflict;
import com.example.eventorder.model.OrderRequest;
import java.util.List;
import org.junit.jupiter.api.Test;

class CycleFinderTest {

    private static List<Conflict> cyclesOf(OrderRequest request) {
        ValidatedInput input = RequestValidator.validate(request);
        return CycleFinder.findCycles(DependencyGraph.build(input));
    }

    @Test
    void detectsThreeNodeCycleAsReadableChain() {
        List<Conflict> conflicts = cyclesOf(TestFixtures.cyclicDependencies());
        assertEquals(1, conflicts.size());
        Conflict conflict = conflicts.get(0);
        assertEquals("CYCLE", conflict.kind());
        assertEquals(List.of("a", "b", "c", "a"), conflict.chain());
        assertTrue(conflict.detail().contains("a -> b -> c -> a"));
    }

    @Test
    void detectsSelfLoop() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a")),
                List.of(TestFixtures.dep("a", "a")));
        List<Conflict> conflicts = cyclesOf(request);
        assertEquals(1, conflicts.size());
        assertEquals(List.of("a", "a"), conflicts.get(0).chain());
    }

    @Test
    void reportsShortestCycleInsideLargerComponent() {
        // Component {a,b,c,d}; shortcut c -> a makes a->b->c->a the shortest cycle
        // through 'a', shorter than a->b->c->d->a.
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b"),
                        TestFixtures.event("c"), TestFixtures.event("d")),
                List.of(TestFixtures.dep("a", "b"), TestFixtures.dep("b", "c"),
                        TestFixtures.dep("c", "d"), TestFixtures.dep("d", "a"),
                        TestFixtures.dep("c", "a")));
        List<Conflict> conflicts = cyclesOf(request);
        assertEquals(1, conflicts.size());
        assertEquals(List.of("a", "b", "c", "a"), conflicts.get(0).chain());
    }

    @Test
    void reportsEachIndependentCycleOnce() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b"),
                        TestFixtures.event("x"), TestFixtures.event("y")),
                List.of(TestFixtures.dep("a", "b"), TestFixtures.dep("b", "a"),
                        TestFixtures.dep("x", "y"), TestFixtures.dep("y", "x")));
        List<Conflict> conflicts = cyclesOf(request);
        assertEquals(2, conflicts.size());
        assertEquals(List.of("a", "b", "a"), conflicts.get(0).chain());
        assertEquals(List.of("x", "y", "x"), conflicts.get(1).chain());
    }

    @Test
    void acyclicGraphHasNoConflicts() {
        assertTrue(cyclesOf(TestFixtures.multipleLegalOrders()).isEmpty());
    }

    @Test
    void selfLoopOnSmallestSccNodeDoesNotMaskRealCycle() {
        // 'a' has a self-loop AND sits in the non-trivial SCC {a,b}; the SCC's
        // shortest cycle must still be a -> b -> a, not the self-edge.
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b")),
                List.of(TestFixtures.dep("a", "a"), TestFixtures.dep("a", "b"),
                        TestFixtures.dep("b", "a")));
        List<Conflict> conflicts = cyclesOf(request);
        assertEquals(2, conflicts.size());
        assertEquals(List.of("a", "a"), conflicts.get(0).chain());
        assertEquals(List.of("a", "b", "a"), conflicts.get(1).chain());
    }
}
