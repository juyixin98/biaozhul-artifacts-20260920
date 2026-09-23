package joinplanner.plan;

/** Result of the dynamic program for one connected set: best tree and its total cost. */
public record DpSolution(PlanNode tree, double cost, boolean saturated) {
}
