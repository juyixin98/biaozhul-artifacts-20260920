package joinorder;

import java.util.List;
import java.util.Map;

/**
 * 基于行数与不同值数（NDV）的代价模型。
 *
 * 基数估计（各等值条件选择率独立假设）：
 *   sel(a = b) = 1 / max(ndv(R.a), ndv(S.b))
 *   |R ⋈ S| = |R| * |S| * Π sel
 * 结果不超过 |R|*|S|；两侧均非空但估计值小于 1 时取 1（教科书式最小值约定）。
 *
 * NDV 传播：连接输出列 c 的 ndv 取 min(输入 ndv, 输出行数)。
 *
 * 代价 = 计划树中所有算子输出行数之和（总 I/O 元组数，System-R 风格）：
 *   scan(t) 代价 = |t|
 *   join(A,B) 代价 = cost(A) + cost(B) + |A ⋈ B|
 */
public final class CostModel {

    private final Model model;

    public CostModel(Model model) {
        this.model = model;
    }

    /** 两子计划在 crossing edges 上的输出统计估计。edges 为空 => 笛卡尔积。 */
    public Stats estimateJoin(int leftMask, int rightMask, Stats left, Stats right, List<Edge> edges) {
        double cartesian = left.rowCount * right.rowCount;
        double rows = cartesian;

        if (edges != null && !edges.isEmpty() && cartesian > 0) {
            double denom = 1.0;
            for (Edge e : edges) {
                for (EqPredicate p : e.predicates) {
                    ColumnRef ca = p.columnOnSide(leftMask, model);
                    ColumnRef cb = p.columnOnSide(rightMask, model);
                    if (ca == null || cb == null) {
                        throw new EngineException("内部错误：谓词不跨越待连接的两个子计划: " + p);
                    }
                    Stats sa = sideStats(ca, left, right, leftMask, rightMask);
                    Stats sb = sideStats(cb, left, right, leftMask, rightMask);
                    denom *= Math.max(sa.getNdv(ca.canonical), sb.getNdv(cb.canonical));
                }
            }
            rows = cartesian / Math.max(1.0, denom);
            if (rows > cartesian) rows = cartesian;
            // 两侧均非空时按教科书约定至少估计为 1 行（独立选择率假设在低选择率端不给出 0 行）
            if (left.rowCount > 0 && right.rowCount > 0 && rows < 1.0) rows = 1.0;
        }
        return buildJoinStats(left, right, rows);
    }

    /** 输出 NDV = min(输入 NDV, 输出行数)，两侧列都保留。 */
    public static Stats buildJoinStats(Stats left, Stats right, double rows) {
        Stats out = new Stats(rows);
        for (Map.Entry<String, Double> e : left.ndv.entrySet()) {
            out.ndv.put(e.getKey(), Math.min(e.getValue(), rows));
        }
        for (Map.Entry<String, Double> e : right.ndv.entrySet()) {
            out.ndv.put(e.getKey(), Math.min(e.getValue(), rows));
        }
        return out;
    }

    private Stats sideStats(ColumnRef c, Stats left, Stats right, int lm, int rm) {
        int ti = 1 << model.indexOf(c.table);
        if ((lm & ti) != 0) return left;
        if ((rm & ti) != 0) return right;
        throw new EngineException("内部错误：列 " + c + " 不属于连接两侧");
    }

    public double joinCost(double leftCost, double rightCost, Stats join) {
        return leftCost + rightCost + join.rowCount;
    }

    public double scanCost(Stats s) {
        return s.rowCount;
    }
}
