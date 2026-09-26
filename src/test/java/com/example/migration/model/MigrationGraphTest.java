package com.example.migration.model;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MigrationGraphTest {

    @Test
    @DisplayName("implicit nodes are collected from edge endpoints")
    void implicitNodes() {
        MigrationGraph g = MigrationGraph.of(null, List.of(Edge.of("x", "y", 1, true)));
        assertTrue(g.contains("x") && g.contains("y"));
        assertTrue(g.hasDirectedEdge("x", "y"));
        assertFalse(g.hasDirectedEdge("y", "x"));
    }

    @Test
    @DisplayName("reject self loops, non-positive cost, and duplicate edges")
    void validation() {
        assertThrows(IllegalArgumentException.class,
                () -> MigrationGraph.of(null, List.of(Edge.of("x", "x", 1, true))));
        assertThrows(IllegalArgumentException.class,
                () -> MigrationGraph.of(null, List.of(Edge.of("x", "y", 0, true))));
        assertThrows(IllegalArgumentException.class,
                () -> MigrationGraph.of(null, List.of(
                        Edge.of("x", "y", 1, true), Edge.of("x", "y", 1, true))));
    }

    @Test
    @DisplayName("parallel edges between the same endpoints with different shape are allowed")
    void parallelEdgesAllowed() {
        MigrationGraph g = MigrationGraph.of(null, List.of(
                Edge.of("x", "y", 1, true),
                new Edge("x", "y", 5, false,
                        java.util.Map.of("mode", "safe"), null, null, null)));
        assertEquals(2, g.outgoingFrom("x").size());
    }
}
