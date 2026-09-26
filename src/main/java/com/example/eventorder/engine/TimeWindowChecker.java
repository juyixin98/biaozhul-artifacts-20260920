package com.example.eventorder.engine;

import com.example.eventorder.model.Conflict;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Time-window feasibility for precedence constraints.
 *
 * <p>Semantics: each event may happen at any instant inside its window
 * [earliest, latest]; a dependency u -&gt; v only requires t(u) &le; t(v). Windows on
 * their own create <b>no</b> ordering: two events with overlapping (or disjoint)
 * windows and no dependency stay unordered.
 *
 * <p>Feasibility is the standard earliest-start propagation: process events in
 * topological order, propagating the largest lower bound forward. A schedule
 * exists exactly when every propagated lower bound fits that event's latest bound.
 */
public final class TimeWindowChecker {

    private TimeWindowChecker() {
    }

    /**
     * @param order a topological order of the graph (caller guarantees acyclicity)
     * @return one conflict per violated bound (typically one; more for independent branches)
     */
    public static List<Conflict> check(ValidatedInput input, DependencyGraph graph,
                                       List<String> order) {
        Map<String, Instant> lowerBound = new HashMap<>();
        Map<String, String> boundParent = new HashMap<>();

        for (String id : order) {
            ValidatedEvent event = input.events().get(id);
            Instant own = event.earliest().orElse(null);
            Instant best = own;
            String bestParent = null;
            for (String predecessor : graph.predecessorsOf(id)) {
                Instant predBound = lowerBound.get(predecessor);
                if (predBound != null && (best == null || predBound.isAfter(best))) {
                    best = predBound;
                    bestParent = predecessor;
                }
            }
            lowerBound.put(id, best);
            boundParent.put(id, bestParent);
        }

        List<Conflict> conflicts = new ArrayList<>();
        for (String id : order) {
            ValidatedEvent event = input.events().get(id);
            if (event.latest().isEmpty()) {
                continue;
            }
            Instant latest = event.latest().get();
            if (event.earliest().isPresent() && event.earliest().get().isAfter(latest)) {
                conflicts.add(new Conflict("TIME_CONTRADICTION", List.of(id),
                        "event '" + id + "' has earliest " + event.earliest().get()
                                + " later than its own latest " + latest));
                continue;
            }
            Instant required = lowerBound.get(id);
            if (required != null && required.isAfter(latest)) {
                List<String> chain = traceChain(id, boundParent);
                String headId = chain.get(0);
                Instant headEarliest = input.events().get(headId).earliest().orElseThrow();
                conflicts.add(new Conflict("TIME_CONTRADICTION", chain,
                        "dependency chain requires '" + id + "' to happen at or after "
                                + required + " (propagated from earliest " + headEarliest
                                + " of '" + headId + "'), but its latest bound is " + latest));
            }
        }
        return conflicts;
    }

    /** Follows bound parents up to the event carrying the concrete earliest bound. */
    private static List<String> traceChain(String id, Map<String, String> boundParent) {
        List<String> chain = new ArrayList<>();
        String current = id;
        while (current != null) {
            chain.add(0, current);
            current = boundParent.get(current);
        }
        return chain;
    }
}
