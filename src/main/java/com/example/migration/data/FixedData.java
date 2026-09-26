package com.example.migration.data;

import com.example.migration.model.Edge;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * Local, fixed test data. No database, no network — every scenario in the
 * acceptance criteria is represented here as a named scenario.
 *
 * <p>Version ids are deliberately non-numeric / non-ordered ({@code legacy},
 * {@code schema-aurora}, ...) to make it impossible for a planner to "cheat"
 * by comparing version numbers: migration direction exists ONLY where an edge
 * declares it.
 */
public final class FixedData {

    private FixedData() {
    }

    public static final String DEFAULT_SCENARIO = "shop";

    /** Scenario names available through the CLI / embedded server. */
    public static List<String> scenarios() {
        return List.of("shop", "branch", "cycle", "island", "tie", "rollback");
    }

    // ---------------------------------------------------------------- scenario 1

    /**
     * "shop": linear chain with a fork, time windows, preconditions and a
     * one-way (irreversible) edge.
     *
     * <pre>
     *   legacy --v1--> v2 --v3--> v3-1 --v4--> schema-aurora
     *                     |                      ^
     *                     +------ v3-hotfix -----+
     * </pre>
     */
    public static Scenario shop() {
        List<String> nodes = List.of("legacy", "v2", "v3-1", "v3-hotfix", "schema-aurora");
        List<Edge> edges = List.of(
                new Edge("legacy", "v2", 5, false,
                        Map.of("maintenance_window", true),
                        null, null, "baseline import (one-way, destructive)"),
                new Edge("v2", "v3-1", 3, true,
                        Map.of("region", List.of("cn-east", "cn-north")),
                        Instant.parse("2026-01-01T00:00:00Z"),
                        Instant.parse("2026-12-31T23:59:59Z"),
                        "structured column migration"),
                new Edge("v2", "v3-hotfix", 2, false,
                        Map.of("emergency", true),
                        null, null, "emergency hotfix branch (not reversible)"),
                new Edge("v3-1", "schema-aurora", 4, true,
                        Map.of("engine", "aurora"), null, null, "move to aurora dialect"),
                new Edge("v3-hotfix", "schema-aurora", 6, true,
                        null, null, null, "reconcile hotfix then move to aurora"),
                // real reverse edges (only where they truly exist)
                new Edge("v3-1", "v2", 3, true,
                        Map.of("region", List.of("cn-east", "cn-north")),
                        null, null, "real rollback path for v3-1"),
                new Edge("schema-aurora", "v3-1", 4, true,
                        Map.of("engine", "aurora"), null, null, "real rollback path for aurora")
        );
        return new Scenario("shop",
                "linear chain + fork with preconditions, a time window and one-way edges",
                nodes, edges);
    }

    // ---------------------------------------------------------------- scenario 2

    /**
     * "branch": genuine fork from v2 to two independently numbered targets that
     * converge. Demonstrates that equal ids do not imply ordering.
     */
    public static Scenario branch() {
        List<String> nodes = List.of("v2", "v3-blue", "v3-green", "v4");
        List<Edge> edges = List.of(
                Edge.of("v2", "v3-blue", 2, true),
                Edge.of("v2", "v3-green", 2, true),
                Edge.of("v3-blue", "v4", 3, true),
                Edge.of("v3-green", "v4", 3, true),
                Edge.of("v3-blue", "v3-green", 8, false) // cross branch, costly
        );
        return new Scenario("branch", "diamond fork with equal-cost branches", nodes, edges);
    }

    // ---------------------------------------------------------------- scenario 3

    /**
     * "cycle": a directed cycle v3 -> v3b -> v3 exists alongside a path out.
     * Enumeration must terminate and must not emit loop-containing paths.
     */
    public static Scenario cycle() {
        List<String> nodes = List.of("v2", "v3", "v3b", "v4");
        List<Edge> edges = List.of(
                Edge.of("v2", "v3", 1, true),
                Edge.of("v3", "v3b", 1, true),
                Edge.of("v3b", "v3", 1, true),
                Edge.of("v3", "v4", 5, true),
                Edge.of("v3b", "v4", 2, true)
        );
        return new Scenario("cycle", "directed cycle v3 <-> v3b with an exit", nodes, edges);
    }

    // ---------------------------------------------------------------- scenario 4

    /**
     * "island": target version sits in a component with no incoming real edge.
     */
    public static Scenario island() {
        List<String> nodes = List.of("v2", "v3", "vX-orphan");
        List<Edge> edges = List.of(
                Edge.of("v2", "v3", 2, true),
                // note: vX-orphan is declared as a node but has NO edge connecting it;
                // even though vX-orphan "looks newer", it is unreachable
                Edge.of("vX-orphan", "v3", 1, false)
        );
        return new Scenario("island", "disconnected target; no real path exists", nodes, edges);
    }

    // ---------------------------------------------------------------- scenario 5

    /**
     * "tie": two equally costly paths must BOTH be returned with stable ranks.
     */
    public static Scenario tie() {
        List<String> nodes = List.of("a", "b", "c", "d");
        List<Edge> edges = List.of(
                Edge.of("a", "b", 2, true),
                Edge.of("a", "c", 2, true),
                Edge.of("b", "d", 3, true),
                Edge.of("c", "d", 3, true)
        );
        return new Scenario("tie", "exact total-cost tie (5 via b vs 5 via c)", nodes, edges);
    }

    // ---------------------------------------------------------------- scenario 6

    /**
     * "rollback": from=schema-aurora, to=v2. Only REAL reverse edges count.
     * One edge is marked reversible=true WITHOUT a reverse edge — it must NOT
     * make rollback possible.
     */
    public static Scenario rollback() {
        List<String> nodes = List.of("v2", "v3-1", "v3-hotfix", "schema-aurora");
        List<Edge> edges = List.of(
                Edge.of("v2", "v3-1", 3, true),
                Edge.of("v2", "v3-hotfix", 2, true),
                Edge.of("v3-1", "schema-aurora", 4, true),
                Edge.of("v3-hotfix", "schema-aurora", 6, true),
                // real reverse edge only on the v3-1 line
                Edge.of("schema-aurora", "v3-1", 4, true),
                Edge.of("v3-1", "v2", 3, true)
                // v3-hotfix forward edges are marked reversible but no reverse
                // edge exists; rollback aurora->v2 via hotfix is therefore impossible
        );
        return new Scenario("rollback",
                "rollback requires real reverse edges; reversible flags are not edges",
                nodes, edges);
    }

    public static Scenario byName(String name) {
        return switch (name == null ? "" : name) {
            case "shop" -> shop();
            case "branch" -> branch();
            case "cycle" -> cycle();
            case "island" -> island();
            case "tie" -> tie();
            case "rollback" -> rollback();
            default -> throw new IllegalArgumentException(
                    "unknown scenario '" + name + "', known: " + scenarios());
        };
    }

    public record Scenario(String name, String description, List<String> nodes, List<Edge> edges) {
    }
}
