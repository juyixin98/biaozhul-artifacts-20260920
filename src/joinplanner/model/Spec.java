package joinplanner.model;

import java.util.ArrayList;
import java.util.List;

/** Validated join-planning request. */
public final class Spec {
    public final List<Table> tables;
    public final List<Edge> edges;
    public final boolean leftDeepOnly;
    public final CostModel costModel;
    public final double defaultSelectivity;
    /** Cardinalities / costs above this are saturated and flagged as overflow. */
    public final double estimateCap;

    public final List<String> warnings = new ArrayList<>();

    public Spec(List<Table> tables, List<Edge> edges, boolean leftDeepOnly, CostModel costModel,
                double defaultSelectivity, double estimateCap) {
        this.tables = tables;
        this.edges = edges;
        this.leftDeepOnly = leftDeepOnly;
        this.costModel = costModel;
        this.defaultSelectivity = defaultSelectivity;
        this.estimateCap = estimateCap;
    }

    public int n() {
        return tables.size();
    }

    public void warn(String message) {
        warnings.add(message);
    }
}
