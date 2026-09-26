package com.example.eventorder;

import com.example.eventorder.model.DependencySpec;
import com.example.eventorder.model.EventSpec;
import com.example.eventorder.model.OrderOptions;
import com.example.eventorder.model.OrderRequest;
import java.util.List;

/**
 * Fixed local test data. Every scenario here is hand-constructed; no random or
 * external data is used anywhere in the project.
 */
public final class TestFixtures {

    private TestFixtures() {
    }

    public static EventSpec event(String id) {
        return new EventSpec(id, null, null, null);
    }

    public static EventSpec event(String id, String earliest, String latest) {
        return new EventSpec(id, earliest, latest, null);
    }

    public static EventSpec versioned(String id, long version) {
        return new EventSpec(id, null, null, version);
    }

    public static DependencySpec dep(String before, String after) {
        return new DependencySpec(before, after, null);
    }

    public static OrderRequest request(List<EventSpec> events, List<DependencySpec> deps) {
        return new OrderRequest("test-req", events, deps, null);
    }

    public static OrderRequest request(List<EventSpec> events, List<DependencySpec> deps,
                                       OrderOptions options) {
        return new OrderRequest("test-req", events, deps, options);
    }

    /**
     * Acceptance scenario "multiple legal orders": three independent-ish events
     * with a single constraint a -&gt; c. Valid orders: [a,b,c], [a,c,b], [b,a,c].
     */
    public static OrderRequest multipleLegalOrders() {
        return request(
                List.of(event("a"), event("b"), event("c")),
                List.of(dep("a", "c")));
    }

    /** Acceptance scenario "circular dependency": a -&gt; b -&gt; c -&gt; a. */
    public static OrderRequest cyclicDependencies() {
        return request(
                List.of(event("a"), event("b"), event("c")),
                List.of(dep("a", "b"), dep("b", "c"), dep("c", "a")));
    }

    /**
     * Acceptance scenario "contradictory time constraint": a must precede b, but
     * a cannot start before 12:00 while b cannot happen after 11:00 (UTC).
     */
    public static OrderRequest contradictoryTime() {
        return request(
                List.of(
                        event("a", "2026-01-01T12:00:00Z", null),
                        event("b", null, "2026-01-01T11:00:00Z")),
                List.of(dep("a", "b")));
    }

    /** Longer contradiction chain: earliest bound travels a -&gt; b -&gt; c. */
    public static OrderRequest contradictoryTimeChain() {
        return request(
                List.of(
                        event("a", "2026-01-01T12:00:00Z", null),
                        event("b", null, null),
                        event("c", null, "2026-01-01T11:00:00Z")),
                List.of(dep("a", "b"), dep("b", "c")));
    }

    /** Two events with fully overlapping windows and no dependency between them. */
    public static OrderRequest overlappingWindowsNoDependency() {
        return request(
                List.of(
                        event("x", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"),
                        event("y", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z")),
                List.of());
    }

    /**
     * Two events with disjoint windows (x is strictly earlier than y) but no
     * dependency. Both directions must remain legal: windows never create edges.
     */
    public static OrderRequest disjointWindowsNoDependency() {
        return request(
                List.of(
                        event("x", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"),
                        event("y", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z")),
                List.of());
    }

    /** Version rule enabled with a backwards version edge v2 -&gt; v1. */
    public static OrderRequest versionContradiction() {
        return request(
                List.of(versioned("a", 2), versioned("b", 1)),
                List.of(dep("a", "b")),
                new OrderOptions(true, null));
    }
}
