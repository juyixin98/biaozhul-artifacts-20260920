package com.example.eventorder.engine;

import com.example.eventorder.model.Conflict;
import com.example.eventorder.model.DependencyCheck;
import com.example.eventorder.model.DependencySpec;
import com.example.eventorder.model.OrderRequest;
import com.example.eventorder.model.OrderResponse;
import java.util.ArrayList;
import java.util.List;

/**
 * Orchestrates validation, conflict detection and order reconstruction.
 *
 * <p>Pipeline: validate -&gt; build graph -&gt; detect cycles -&gt; (if acyclic)
 * propagate time windows -&gt; apply version rule -&gt; if no conflicts, emit the
 * deterministic order, the exact extension count and capped enumeration.
 */
public final class OrderEngine {

    private final String tzdbVersion;

    public OrderEngine(String tzdbVersion) {
        this.tzdbVersion = tzdbVersion;
    }

    public OrderResponse process(OrderRequest request) {
        ValidatedInput input = RequestValidator.validate(request);
        DependencyGraph graph = DependencyGraph.build(input);

        List<Conflict> conflicts = new ArrayList<>(CycleFinder.findCycles(graph));
        List<String> order = null;
        if (conflicts.isEmpty()) {
            order = TopologicalOrderer.lexicographicOrder(graph);
            conflicts.addAll(TimeWindowChecker.check(input, graph, order));
            conflicts.addAll(VersionRuleChecker.check(input));
        }

        List<DependencyCheck> checks = new ArrayList<>();
        for (DependencySpec dep : input.dependencies()) {
            checks.add(new DependencyCheck(dep.before(), dep.after(), conflicts.isEmpty()));
        }

        if (!conflicts.isEmpty()) {
            return new OrderResponse(input.requestId(), tzdbVersion, false,
                    null, 0, List.of(), checks, List.copyOf(conflicts));
        }
        long count = LinearExtensions.count(graph);
        List<List<String>> enumerated = LinearExtensions.enumerate(graph, input.maxEnumeratedOrders());
        return new OrderResponse(input.requestId(), tzdbVersion, true,
                order, count, enumerated, checks, List.of());
    }
}
