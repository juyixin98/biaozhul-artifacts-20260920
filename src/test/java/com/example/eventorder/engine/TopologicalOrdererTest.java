package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.OrderRequest;
import java.util.List;
import org.junit.jupiter.api.Test;

class TopologicalOrdererTest {

    private static List<String> orderOf(OrderRequest request) {
        ValidatedInput input = RequestValidator.validate(request);
        return TopologicalOrderer.lexicographicOrder(DependencyGraph.build(input));
    }

    @Test
    void producesLexicographicallySmallestOrder() {
        // b is unconstrained and 'b' < 'c', so b comes before c even though
        // the dependency only forces a < c.
        List<String> order = orderOf(TestFixtures.multipleLegalOrders());
        assertEquals(List.of("a", "b", "c"), order);
    }

    @Test
    void isDeterministicAcrossRepeatedRuns() {
        List<String> first = orderOf(TestFixtures.multipleLegalOrders());
        for (int i = 0; i < 20; i++) {
            assertEquals(first, orderOf(TestFixtures.multipleLegalOrders()));
        }
    }

    @Test
    void respectsEveryDependencyEdge() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("build"), TestFixtures.event("test"),
                        TestFixtures.event("deploy"), TestFixtures.event("notify")),
                List.of(TestFixtures.dep("build", "test"), TestFixtures.dep("test", "deploy"),
                        TestFixtures.dep("deploy", "notify")));
        List<String> order = orderOf(request);
        assertEquals(List.of("build", "test", "deploy", "notify"), order);
    }

    @Test
    void returnsNullOnCycle() {
        assertNull(orderOf(TestFixtures.cyclicDependencies()));
    }

    @Test
    void selfLoopIsNotOrdered() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a")),
                List.of(TestFixtures.dep("a", "a")));
        assertNull(orderOf(request));
    }
}
