package joinplanner.core;

import java.util.List;

import joinplanner.model.PlanNode;

/** Result of a DP run. */
public record DpResult(
        PlanNode root,
        double totalCost,
        boolean costOverflow,
        List<Integer> joinOrder,
        List<String> joinOrderNames) {
}
