package joinplanner.sim;

import joinplanner.model.CostModel;

/** Cardinality oracle over measured row counts; mirrors {@link joinplanner.plan.Estimator}. */
public final class ActualCostModel {

    private final double[] card;
    private final boolean[] connected;
    public final CostModel costModel;

    public ActualCostModel(double[] card, boolean[] connected, CostModel costModel) {
        this.card = card;
        this.connected = connected;
        this.costModel = costModel;
    }

    public double card(int mask) {
        return card[mask];
    }

    public boolean connected(int mask) {
        return connected[mask];
    }

    public boolean hasCrossingEdge(int a, int b) {
        // Connectivity of a, b and of the union implies a crossing edge exists.
        return connected[a] && connected[b] && connected[a | b];
    }
}
