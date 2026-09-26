package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.OrderRequest;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

class LinearExtensionsTest {

    private static DependencyGraph graphOf(OrderRequest request) {
        return DependencyGraph.build(RequestValidator.validate(request));
    }

    @Test
    void countsAllLinearExtensions() {
        // a -> c only: valid orders are abc, acb, bac.
        assertEquals(3, LinearExtensions.count(graphOf(TestFixtures.multipleLegalOrders())));
    }

    @Test
    void unconstrainedEventsHaveFactorialCount() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b"),
                        TestFixtures.event("c"), TestFixtures.event("d")),
                List.of());
        assertEquals(24, LinearExtensions.count(graphOf(request)));
    }

    @Test
    void totalChainHasExactlyOneOrder() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.event("a"), TestFixtures.event("b"),
                        TestFixtures.event("c")),
                List.of(TestFixtures.dep("a", "b"), TestFixtures.dep("b", "c")));
        assertEquals(1, LinearExtensions.count(graphOf(request)));
    }

    @Test
    void enumerationMatchesCountAndIsLexicographic() {
        DependencyGraph graph = graphOf(TestFixtures.multipleLegalOrders());
        List<List<String>> orders = LinearExtensions.enumerate(graph, 100);
        assertEquals(3, orders.size());
        assertEquals(List.of(
                List.of("a", "b", "c"),
                List.of("a", "c", "b"),
                List.of("b", "a", "c")), orders);
    }

    @Test
    void enumerationRespectsLimit() {
        DependencyGraph graph = graphOf(TestFixtures.multipleLegalOrders());
        assertEquals(2, LinearExtensions.enumerate(graph, 2).size());
        assertEquals(0, LinearExtensions.enumerate(graph, 0).size());
    }

    @Test
    void countAboveExactLimitReturnsMinusOne() {
        List<com.example.eventorder.model.EventSpec> events = new ArrayList<>();
        for (int i = 0; i < LinearExtensions.MAX_EXACT_COUNT_NODES + 1; i++) {
            events.add(TestFixtures.event(String.format("e%02d", i)));
        }
        OrderRequest request = TestFixtures.request(events, List.of());
        assertEquals(-1, LinearExtensions.count(graphOf(request)));
    }
}