package joinorder;

import java.util.Collections;
import java.util.List;
import java.util.Map;

/** 基表扫描节点。 */
public final class ScanNode extends PlanNode {
    public final int tableIdx;

    public ScanNode(int tableIdx) {
        super(1 << tableIdx);
        this.tableIdx = tableIdx;
    }

    @Override public String nodeType() { return "scan"; }

    @Override public Map<String, Object> toMap(Model model) {
        return baseMap(model);
    }

    @Override String label(Model model) {
        Table t = model.table(tableIdx);
        StringBuilder sb = new StringBuilder();
        sb.append("Scan ").append(t.name)
          .append("  [est rows=").append(round(estimated.rowCount))
          .append(", cost=").append(round(estimatedCost));
        if (actual != null) {
            sb.append(" | actual rows=").append((long) actual.rowCount)
              .append(", cost=").append(round(actualCost));
        }
        sb.append(']');
        return sb.toString();
    }

    @Override List<PlanNode> children() { return Collections.emptyList(); }
}
