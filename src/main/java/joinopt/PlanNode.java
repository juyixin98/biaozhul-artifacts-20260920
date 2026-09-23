package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 连接计划树节点。
 *
 * 叶子节点（kind == "table"）：tableIdx 指向基表，children 为空。
 * 连接节点（kind == "hashjoin" 或 "cartesian"）：children 恰有两个，
 * preds 为本次连接应用的等值谓词（cartesian 时为空）。
 *
 * 每个节点保存：
 *   - mask：节点覆盖的表集合（位掩码）
 *   - estRows：估计输出行数（双精度，展示时四舍五入）
 *   - cost：以该节点为根的整棵子树的累计代价（见 {@link #nodeCost}）
 *   - actualRows：实际执行后回填的真实行数（未执行为 -1）
 */
public final class PlanNode {

    public static final String TABLE = "table";
    public static final String HASH_JOIN = "hashjoin";
    public static final String CARTESIAN = "cartesian";

    public final String kind;
    public final int mask;
    public final int tableIdx;            // 叶子节点有效，否则为 -1
    public final List<JoinPred> preds;    // 连接节点本次应用的等值谓词
    public final PlanNode left;
    public final PlanNode right;

    public double estRows;
    public double cost;
    public long actualRows = -1;

    private PlanNode(String kind, int mask, int tableIdx,
                     List<JoinPred> preds, PlanNode left, PlanNode right) {
        this.kind = kind;
        this.mask = mask;
        this.tableIdx = tableIdx;
        this.preds = preds;
        this.left = left;
        this.right = right;
    }

    public static PlanNode leaf(int tableIdx, double estRows) {
        return new PlanNode(TABLE, 1 << tableIdx, tableIdx,
                new ArrayList<>(), null, null);
    }

    public static PlanNode join(boolean cartesian, int mask, List<JoinPred> crossing,
                                PlanNode left, PlanNode right, double estRows, double totalCost) {
        PlanNode n = new PlanNode(cartesian ? CARTESIAN : HASH_JOIN, mask, -1,
                crossing, left, right);
        n.estRows = estRows;
        n.cost = totalCost;
        return n;
    }

    public boolean isLeaf() {
        return kind == TABLE;
    }

    /** 该节点自身（不含子节点）的代价，供穷举器按树结构直接复算。 */
    public static double nodeCost(String kind, double leftRows, double rightRows, double outRows) {
        if (TABLE.equals(kind)) return 0;
        if (CARTESIAN.equals(kind)) {
            // 嵌套循环：产生 |L|×|R| 个配对，全部物化为输出
            return leftRows * rightRows;
        }
        // 等值哈希连接：建表探测 + 输出（I/O 与 CPU 的抽象单位）
        return leftRows + rightRows + outRows;
    }

    /** 自底向上复算整棵树的累计代价（主要用于测试校验）。 */
    public static double recomputeTotalCost(PlanNode n) {
        if (n.isLeaf()) {
            n.cost = 0;
            return 0;
        }
        double cl = recomputeTotalCost(n.left);
        double cr = recomputeTotalCost(n.right);
        double self = nodeCost(n.kind, n.left.estRows, n.right.estRows, n.estRows);
        n.cost = cl + cr + self;
        return n.cost;
    }

    /** 序列化为可导出/响应的 JSON 结构。 */
    public Map<String, Object> toJson(List<Table> tables) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("kind", kind);
        if (isLeaf()) {
            Table t = tables.get(tableIdx);
            m.put("table", t.name);
            m.put("tableIndex", tableIdx);
            m.put("estRows", rounded());
        } else {
            m.put("cartesian", kind.equals(CARTESIAN));
            List<Map<String, Object>> pj = new ArrayList<>();
            for (JoinPred p : preds) pj.add(predJson(p, tables));
            m.put("predicates", pj);
            List<Object> ch = new ArrayList<>();
            ch.add(left.toJson(tables));
            ch.add(right.toJson(tables));
            m.put("children", ch);
        }
        m.put("tables", maskTableNames(mask, tables));
        m.put("estRows", rounded());
        m.put("cost", cleanNumber(cost));
        if (actualRows >= 0) m.put("actualRows", actualRows);
        return m;
    }

    private static Map<String, Object> predJson(JoinPred p, List<Table> tables) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("left", tables.get(p.leftTable).name + "." + p.leftColumn);
        m.put("right", tables.get(p.rightTable).name + "." + p.rightColumn);
        return m;
    }

    private List<String> maskTableNames(int mask, List<Table> tables) {
        List<String> names = new ArrayList<>();
        for (int i = 0; i < tables.size(); i++) {
            if (((mask >>> i) & 1) == 1) names.add(tables.get(i).name);
        }
        return names;
    }

    public long rounded() {
        return Math.round(estRows);
    }

    private static Object cleanNumber(double d) {
        if (d == Math.rint(d) && !Double.isInfinite(d)) return (long) d;
        return d;
    }

    /** 缩进文本树，用于命令行展示。 */
    public String toText(List<Table> tables) {
        StringBuilder sb = new StringBuilder();
        appendText(sb, tables, "", true);
        return sb.toString();
    }

    private void appendText(StringBuilder sb, List<Table> tables, String prefix, boolean last) {
        sb.append(prefix);
        if (prefix.isEmpty()) {
            // 根节点不加树枝符号
        } else {
            sb.append(last ? "└─ " : "├─ ");
        }
        sb.append(label(tables)).append('\n');
        if (!isLeaf()) {
            String childPrefix = prefix + (prefix.isEmpty() ? "" : (last ? "   " : "│  "));
            left.appendText(sb, tables, childPrefix, false);
            right.appendText(sb, tables, childPrefix, true);
        }
    }

    private String label(List<Table> tables) {
        String est = String.format("%,d", rounded());
        if (isLeaf()) {
            return "SCAN " + tables.get(tableIdx).name
                    + "  [rows=" + est + "]";
        }
        String head;
        if (kind.equals(CARTESIAN)) {
            head = "NESTEDLOOP CROSS JOIN  [cartesian=true, estRows=" + est + ", cost=" + costText() + "]";
        } else {
            StringBuilder cols = new StringBuilder();
            for (int i = 0; i < preds.size(); i++) {
                JoinPred p = preds.get(i);
                if (i > 0) cols.append(" AND ");
                cols.append(tables.get(p.leftTable).name).append('.').append(p.leftColumn)
                    .append('=').append(tables.get(p.rightTable).name).append('.').append(p.rightColumn);
            }
            head = "HASH JOIN ON " + cols + "  [estRows=" + est + ", cost=" + costText() + "]";
        }
        if (actualRows >= 0) head += "  actualRows=" + String.format("%,d", actualRows);
        return head;
    }

    private String costText() {
        if (cost == Math.rint(cost)) return String.format("%,.0f", cost);
        return String.format("%,.2f", cost);
    }

    /** 结构 + 估计的规范化串，用于判断两个计划是否“同一棵树”（代价一致时 DP 与穷举应一致）。 */
    public String canonical(List<Table> tables) {
        if (isLeaf()) return tables.get(tableIdx).name;
        StringBuilder sb = new StringBuilder(kind.equals(CARTESIAN) ? "X[" : "J[");
        for (int i = 0; i < preds.size(); i++) {
            JoinPred p = preds.get(i);
            if (i > 0) sb.append('&');
            int a = Math.min(p.leftTable, p.rightTable);
            int b = Math.max(p.leftTable, p.rightTable);
            sb.append(tables.get(a).name).append('=').append(tables.get(b).name);
        }
        sb.append("](").append(left.canonical(tables)).append(',')
          .append(right.canonical(tables)).append(')');
        return sb.toString();
    }
}
