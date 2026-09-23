package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 单机内存执行器：按给定 {@link PlanNode} 真实执行连接并物化结果。
 *
 * <ul>
 *   <li>SCAN：直接读取请求中提供的表数据（rows）；</li>
 *   <li>HASH JOIN：以“右子计划建哈希表、左子计划探测”的方式执行
 *       {@link PlanNode#preds} 中全部等值谓词。键的规范化（数字 1 与 1.0 相等、
 *       null 不匹配任何值）与统计侧一致，保证估计与执行口径统一；</li>
 *   <li>CARTESIAN：嵌套循环生成 |L|×|R| 的全部配对（仅在断开的连接图中出现）。</li>
 * </ul>
 *
 * 没有任何投影/过滤算子：输出列 = 两个输入的列拼接，先左后右。
 * 等值连接的<b>语义</b>与顺序无关，因此不同合法计划产生的结果行多重集合一致
 * （行的排列顺序可能不同；本执行器按探测顺序输出）。
 */
public final class Executor {

    private final List<Table> tables;

    public Executor(List<Table> tables) {
        this.tables = tables;
    }

    public Rel execute(PlanNode plan) {
        return run(plan);
    }

    private Rel run(PlanNode node) {
        if (node.isLeaf()) {
            Table t = tables.get(node.tableIdx);
            if (t.statsOnly) {
                throw new IllegalStateException("表 " + t.name
                        + " 只有统计信息（无 rows），无法实际执行");
            }
            List<String> cols = new ArrayList<>();
            for (int i = 0; i < t.columns.size(); i++) cols.add(t.qualified(i));
            List<List<Object>> rows = new ArrayList<>(t.rows.size());
            for (List<Object> r : t.rows) rows.add(new ArrayList<>(r));
            node.actualRows = rows.size();
            return new Rel(cols, rows);
        }

        Rel left = run(node.left);
        Rel right = run(node.right);

        Rel out;
        if (node.kind.equals(PlanNode.CARTESIAN)) {
            out = nestedLoop(left, right);
        } else {
            out = hashJoin(node, left, right);
        }
        node.actualRows = out.rows.size();
        return out;
    }

    private Rel nestedLoop(Rel left, Rel right) {
        List<String> cols = new ArrayList<>(left.columns.size() + right.columns.size());
        cols.addAll(left.columns);
        cols.addAll(right.columns);
        List<List<Object>> out = new ArrayList<>(left.rows.size() * right.rows.size());
        for (List<Object> l : left.rows) {
            for (List<Object> r : right.rows) {
                List<Object> merged = new ArrayList<>(l.size() + r.size());
                merged.addAll(l);
                merged.addAll(r);
                out.add(merged);
            }
        }
        return new Rel(cols, out);
    }

    private Rel hashJoin(PlanNode node, Rel left, Rel right) {
        List<JoinPred> preds = node.preds;
        int[] lIdx = new int[preds.size()];
        int[] rIdx = new int[preds.size()];
        for (int i = 0; i < preds.size(); i++) {
            JoinPred p = preds.get(i);
            // 谓词方向是按基表编号定义的，这里要映射到“左子计划/右子计划”两侧
            boolean predLeftIsLeft = ((nodeMaskHas(node.left, p.leftTable))
                    && (nodeMaskHas(node.right, p.rightTable)));
            if (predLeftIsLeft) {
                lIdx[i] = left.indexOf(tables.get(p.leftTable).name + "." + p.leftColumn);
                rIdx[i] = right.indexOf(tables.get(p.rightTable).name + "." + p.rightColumn);
            } else {
                lIdx[i] = left.indexOf(tables.get(p.rightTable).name + "." + p.rightColumn);
                rIdx[i] = right.indexOf(tables.get(p.leftTable).name + "." + p.leftColumn);
            }
        }

        // 右表建哈希：复合键 -> 数据行列表
        Map<String, List<List<Object>>> table = new LinkedHashMap<>();
        for (List<Object> r : right.rows) {
            if (hasNull(r, rIdx)) continue; // SQL 语义：NULL 不参与等值匹配
            String key = Rel.keyOf(r, rIdx);
            table.computeIfAbsent(key, k -> new ArrayList<>()).add(r);
        }

        List<String> cols = new ArrayList<>(left.columns.size() + right.columns.size());
        cols.addAll(left.columns);
        cols.addAll(right.columns);
        List<List<Object>> out = new ArrayList<>();

        for (List<Object> l : left.rows) {
            if (hasNull(l, lIdx)) continue;
            List<List<Object>> matches = table.get(Rel.keyOf(l, lIdx));
            if (matches == null) continue;
            for (List<Object> r : matches) {
                List<Object> merged = new ArrayList<>(l.size() + r.size());
                merged.addAll(l);
                merged.addAll(r);
                out.add(merged);
            }
        }
        return new Rel(cols, out);
    }

    private static boolean nodeMaskHas(PlanNode n, int tableIdx) {
        return ((n.mask >>> tableIdx) & 1) == 1;
    }

    private static boolean hasNull(List<Object> row, int[] idxs) {
        for (int i : idxs) if (row.get(i) == null) return true;
        return false;
    }
}
