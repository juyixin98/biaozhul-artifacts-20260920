package com.eventorder;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.eventorder.engine.GraphBuilder;
import com.eventorder.engine.OrderEngine;
import com.eventorder.io.JsonCodec;
import com.eventorder.model.Conflict;
import com.eventorder.model.Edge;
import com.eventorder.model.OrderRequest;
import com.eventorder.model.OrderResult;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Acceptance tests driven by the fixed example documents plus inline scenarios. */
class OrderEngineTest {

    private final JsonCodec codec = new JsonCodec();

    private OrderRequest load(String name) throws IOException {
        return codec.readRequest(Files.readString(Path.of("examples", name)));
    }

    private static void assertEveryEdgeRespected(OrderResult result, List<Edge> edges) {
        for (Edge edge : edges) {
            int from = result.order().indexOf(edge.from());
            int to = result.order().indexOf(edge.to());
            assertTrue(from >= 0 && to >= 0, "order must contain " + edge.from() + " and " + edge.to());
            assertTrue(from < to, "edge violated: " + edge.reason());
        }
    }

    private static List<Edge> edgesOf(OrderRequest request) {
        List<String> errors = new ArrayList<>();
        GraphBuilder.BuiltGraph graph = GraphBuilder.build(request, errors);
        assertTrue(errors.isEmpty(), "unexpected validation errors: " + errors);
        return graph.edges();
    }

    @Test
    void satisfiableMixedRulesProduceUniqueOrderRespectingEveryDependency() throws IOException {
        OrderRequest request = load("ok.json");
        OrderResult result = OrderEngine.solve(request);

        assertEquals("OK", result.status());
        assertEquals(List.of("E1", "E2", "E3", "E4"), result.order());
        assertFalse(result.multipleValidOrders());
        assertTrue(result.conflicts().isEmpty());
        assertEveryEdgeRespected(result, edgesOf(request));
    }

    @Test
    void cyclicDependenciesYieldMinimalReadableConflictChain() throws IOException {
        OrderResult result = OrderEngine.solve(load("cycle.json"));

        assertEquals("UNSATISFIABLE", result.status());
        assertEquals(1, result.conflicts().size());
        Conflict conflict = result.conflicts().get(0);
        assertEquals("CYCLE", conflict.type());
        assertEquals(3, conflict.chain().size(), "cycle X->Y->Z->X has exactly 3 edges");

        // Chain is a closed loop: each edge's target is the next edge's source.
        List<Edge> chain = conflict.chain();
        for (int i = 0; i < chain.size(); i++) {
            assertEquals(chain.get(i).to(), chain.get((i + 1) % chain.size()).from(),
                    "conflict chain must link edge " + i + " to edge " + (i + 1));
        }
        // Every link carries a human-readable reason.
        for (Edge edge : chain) {
            assertFalse(edge.reason().isBlank(), "conflict edge must explain itself");
        }
        assertTrue(conflict.message().contains("X"), "message names the events involved");
    }

    @Test
    void contradictoryTimeConstraintIsReportedWithBothWitnessEdges() throws IOException {
        OrderResult result = OrderEngine.solve(load("time-conflict.json"));

        assertEquals("UNSATISFIABLE", result.status());
        List<Edge> chain = result.conflicts().get(0).chain();
        assertEquals(2, chain.size(), "P vs Q contradiction is a 2-edge cycle");

        boolean hasExplicit = chain.stream().anyMatch(e ->
                e.kind() == Edge.Kind.EXPLICIT && e.from().equals("P") && e.to().equals("Q"));
        boolean hasTime = chain.stream().anyMatch(e ->
                e.kind() == Edge.Kind.TIME && e.from().equals("Q") && e.to().equals("P"));
        assertTrue(hasExplicit, "chain must cite the explicit dependency P before Q");
        assertTrue(hasTime, "chain must cite the time evidence that Q ended before P started");
    }

    @Test
    void multipleValidOrdersAreDeterministicAndFlagged() throws IOException {
        OrderRequest request = load("multi-order.json");
        OrderResult first = OrderEngine.solve(request);
        OrderResult second = OrderEngine.solve(request);

        assertEquals("OK", first.status());
        assertTrue(first.multipleValidOrders(),
                "A and B overlap in time with no dependency, so several orders are legal");
        assertEquals(List.of("A", "B", "C"), first.order(),
                "lexicographic tie-break fixes one deterministic order");
        assertEquals(first.order(), second.order(), "repeated runs must be identical");
        assertEveryEdgeRespected(first, edgesOf(request));
    }

    @Test
    void overlappingIntervalsCreateNoCausalEdge() throws IOException {
        OrderRequest request = load("multi-order.json");
        List<Edge> edges = edgesOf(request);

        boolean anyEdgeBetweenAAndB = edges.stream().anyMatch(e ->
                (e.from().equals("A") && e.to().equals("B"))
                        || (e.from().equals("B") && e.to().equals("A")));
        assertFalse(anyEdgeBetweenAAndB,
                "A [09:00,10:00] and B [09:30,10:30] overlap; overlap must not imply causality");

        boolean aBeforeC = edges.stream().anyMatch(e -> e.from().equals("A") && e.to().equals("C"));
        boolean bBeforeC = edges.stream().anyMatch(e -> e.from().equals("B") && e.to().equals("C"));
        assertTrue(aBeforeC && bBeforeC, "disjoint intervals still produce time edges");
    }

    @Test
    void versionRuleOrdersSameStreamAscending() throws IOException {
        OrderRequest request = codec.readRequest("""
                {
                  "events": [
                    { "id": "v3", "version": { "stream": "s", "value": 3 } },
                    { "id": "v1", "version": { "stream": "s", "value": 1 } },
                    { "id": "v2", "version": { "stream": "s", "value": 2 } }
                  ],
                  "dependencies": []
                }
                """);
        OrderResult result = OrderEngine.solve(request);

        assertEquals("OK", result.status());
        assertEquals(List.of("v1", "v2", "v3"), result.order());
        assertFalse(result.multipleValidOrders());
    }

    @Test
    void crossTimezoneIntervalsCompareByInstant() throws IOException {
        OrderRequest request = codec.readRequest("""
                {
                  "events": [
                    { "id": "shanghai", "interval": { "start": "2026-01-01T09:00:00+08:00[Asia/Shanghai]", "end": "2026-01-01T09:30:00+08:00[Asia/Shanghai]" } },
                    { "id": "london",   "interval": { "start": "2026-01-01T02:00:00+00:00[Europe/London]",  "end": "2026-01-01T03:00:00+00:00[Europe/London]" } }
                  ],
                  "dependencies": []
                }
                """);
        OrderResult result = OrderEngine.solve(request);

        // shanghai ends 01:30Z, london starts 02:00Z -> shanghai strictly before london.
        assertEquals("OK", result.status());
        assertEquals(List.of("shanghai", "london"), result.order());
        assertFalse(result.multipleValidOrders(), "time rule fully determines this order");
    }

    @Test
    void invalidInputIsRejectedWithReadableErrors() throws IOException {
        OrderRequest request = codec.readRequest("""
                {
                  "events": [
                    { "id": "A", "interval": { "start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z" } },
                    { "id": "A" },
                    { "id": "B" }
                  ],
                  "dependencies": [
                    { "before": "A", "after": "GHOST" }
                  ]
                }
                """);
        OrderResult result = OrderEngine.solve(request);

        assertEquals("INVALID_INPUT", result.status());
        assertTrue(result.errors().stream().anyMatch(e -> e.contains("duplicate event id")),
                "duplicate id reported: " + result.errors());
        assertTrue(result.errors().stream().anyMatch(e -> e.contains("end is before start")),
                "inverted interval reported: " + result.errors());
        assertTrue(result.errors().stream().anyMatch(e -> e.contains("unknown event")),
                "dangling dependency reported: " + result.errors());
    }

    @Test
    void tzdbVersionIsRecordedInDiagnostics() throws IOException {
        OrderResult result = OrderEngine.solve(load("ok.json"));

        assertFalse(result.diagnostics().tzdbVersion().isBlank(),
                "IANA tzdb version must be recorded");
        assertEquals(4, result.diagnostics().eventCount());
        assertTrue(result.diagnostics().edgeCount() >= 3,
                "time edge E1->E2, explicit E2->E3, version E3->E4");
    }

    @Test
    void resultSerializesToJson() throws IOException {
        OrderResult result = OrderEngine.solve(load("ok.json"));
        String json = codec.writeResult(result);

        assertTrue(json.contains("\"status\" : \"OK\""));
        assertTrue(json.contains("\"tzdbVersion\""));
        assertTrue(json.contains("\"multipleValidOrders\" : false"));
    }
}
