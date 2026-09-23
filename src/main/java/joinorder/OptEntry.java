package joinorder;

import java.util.LinkedHashMap;
import java.util.Map;

/** 某表集合上的最优（子）计划条目。 */
public final class OptEntry {
    public final int mask;
    public final PlanNode plan;
    public final Stats stats;
    public final double cost;

    public OptEntry(int mask, PlanNode plan, Stats stats, double cost) {
        this.mask = mask;
        this.plan = plan;
        this.stats = stats;
        this.cost = cost;
    }

    public Map<String, Object> dump(Model model) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tables", plan.tableNames(model));
        m.put("connected", model.isConnected(mask));
        m.put("estimatedRows", PlanNode.round(stats.rowCount));
        m.put("cost", PlanNode.round(cost));
        m.put("rootType", plan.nodeType());
        return m;
    }
}
