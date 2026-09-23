package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 查询计划节点（扫描或连接）。携带表集合掩码、估计/实际统计。 */
public abstract class PlanNode {
    /** 该计划包含的基表 bit 集合。 */
    public final int mask;
    /** 优化器估计统计。 */
    public Stats estimated;
    /** 优化器估计的累计代价。 */
    public double estimatedCost;
    /** 执行器实际统计（执行后填充）。 */
    public Stats actual;
    /** 执行器实际累计代价。 */
    public double actualCost;
    /** 该节点是否被标记为笛卡尔积（连接图断开导致）。 */
    public boolean cartesian;

    protected PlanNode(int mask) {
        this.mask = mask;
    }

    public abstract String nodeType();

    public abstract Map<String, Object> toMap(Model model);

    protected List<String> tableNames(Model model) {
        List<String> names = new ArrayList<>();
        int bits = mask;
        while (bits != 0) {
            int b = bits & -bits;
            names.add(model.table(Integer.numberOfTrailingZeros(b)).name);
            bits ^= b;
        }
        return names;
    }

    protected Map<String, Object> baseMap(Model model) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", nodeType());
        m.put("tables", tableNames(model));
        if (cartesian) m.put("cartesian", true);
        if (estimated != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("rowCount", round(estimated.rowCount));
            e.put("cost", round(estimatedCost));
            m.put("estimated", e);
        }
        if (actual != null) {
            Map<String, Object> a = new LinkedHashMap<>();
            a.put("rowCount", (long) actual.rowCount);
            a.put("cost", round(actualCost));
            m.put("actual", a);
        }
        return m;
    }

    static double round(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) return v;
        return Math.rint(v * 1000.0) / 1000.0;
    }

    /** 计划的可读树形文本。 */
    public String toTree(Model model) {
        StringBuilder sb = new StringBuilder();
        tree(model, sb, "", true);
        return sb.toString();
    }

    void tree(Model model, StringBuilder sb, String prefix, boolean last) {
        sb.append(prefix).append(last ? "└─ " : "├─ ").append(label(model)).append('\n');
        String childPrefix = prefix + (last ? "   " : "│  ");
        List<PlanNode> kids = children();
        for (int i = 0; i < kids.size(); i++) {
            kids.get(i).tree(model, sb, childPrefix, i == kids.size() - 1);
        }
    }

    abstract String label(Model model);
    abstract List<PlanNode> children();
}
