package com.eventorder.engine;

import com.eventorder.model.Conflict;
import com.eventorder.model.Edge;
import com.eventorder.model.OrderRequest;
import com.eventorder.model.OrderResult;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/** Orchestrates validation, graph construction, cycle detection and sorting. */
public final class OrderEngine {

    private OrderEngine() {
    }

    public static OrderResult solve(OrderRequest request) {
        String tzdb = TzdbInfo.version();

        List<String> errors = new ArrayList<>();
        GraphBuilder.BuiltGraph graph = GraphBuilder.build(request, errors);
        if (!errors.isEmpty()) {
            return OrderResult.invalidInput(errors, tzdb);
        }

        OrderResult.Diagnostics diagnostics = new OrderResult.Diagnostics(
                tzdb, graph.nodeIds().size(), graph.edges().size());

        Optional<List<Edge>> cycle = CycleFinder.findCycle(graph);
        if (cycle.isPresent()) {
            return OrderResult.unsatisfiable(List.of(Conflict.cycle(cycle.get())), diagnostics);
        }

        TopoSorter.SortOutcome outcome = TopoSorter.sort(graph);
        return OrderResult.ok(outcome.order(), outcome.multipleValidOrders(), diagnostics);
    }
}
