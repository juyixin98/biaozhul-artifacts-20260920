package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 合法连接顺序穷举器（仅用于小规模校验，n ≤ 6）。
 *
 * 递归枚举一个可达子集 S 的所有<b>二叉连接树</b>（左右子节点有序）：
 * <pre>
 *   |S| = 1 -> 只有扫描叶子
 *   |S| > 1 -> 枚举所有无向真分区（一侧含 S 最低位）；
 *              对每个合法分区，递归 L、R 全部树，
 *              并分别以 (L,R) 与 (R,L) 两种朝向组合上层节点
 * </pre>
 * 合法性判定与 DP 完全一致（见 {@link Components}）：
 * 连通子集的分区必须有跨边（哈希连接）；跨分量分区两侧必须是完整分量之并
 * （笛卡尔积）。不可达子集没有任何计划。
 *
 * 无任何谓词时，n 个表的合法树总数为 (2n-2)!/(n-1)!：
 * n=1..6 依次为 1, 2, 12, 120, 1680, 30240。穷举结果用于校验
 * {@link Optimizer} 的 DP 找到的最小代价与全局最小值完全一致。
 */
public final class Enumerator {

    public static final int ENUM_LIMIT = 6;

    private final List<Table> tables;
    private final Estimator estimator;
    private final Components components;
    private final Map<Integer, List<PlanNode>> memo = new LinkedHashMap<>();

    public Enumerator(List<Table> tables, List<JoinPred> preds) {
        this.tables = tables;
        this.estimator = new Estimator(tables, preds);
        this.components = new Components(tables.size(), preds);
    }

    /** 一个子集的全部合法计划；不可达子集返回空列表。 */
    public List<PlanNode> enumerate(int mask) {
        if (memo.containsKey(mask)) return memo.get(mask);
        List<PlanNode> result = enumerateUncached(mask);
        memo.put(mask, result);
        return result;
    }

    private List<PlanNode> enumerateUncached(int mask) {
        if (Integer.bitCount(mask) == 1) {
            int t = Integer.numberOfTrailingZeros(mask);
            List<PlanNode> one = new ArrayList<>();
            PlanNode leaf = PlanNode.leaf(t, tables.get(t).rowCount);
            leaf.estRows = tables.get(t).rowCount;
            leaf.cost = 0;
            one.add(leaf);
            return one;
        }

        List<PlanNode> result = new ArrayList<>();
        int lowest = mask & -mask;
        int rest = mask ^ lowest;
        for (int lSub = rest; ; lSub = (lSub - 1) & rest) {
            int aMask = lowest | lSub;
            int bMask = mask ^ aMask;
            if (bMask != 0) {
                List<PlanPred> aPlans = wrap(enumerate(aMask));
                List<PlanPred> bPlans = wrap(enumerate(bMask));
                if (!aPlans.isEmpty() && !bPlans.isEmpty()) {
                    List<JoinPred> crossing = estimator.crossingPreds(aMask, bMask);
                    if (components.legalSplit(aMask, bMask, !crossing.isEmpty())) {
                        // 两种朝向：(A 在左,B 在右) 与 (B 在左,A 在右)
                        addCombos(result, mask, aPlans, bPlans, aMask, bMask, crossing);
                        addCombos(result, mask, bPlans, aPlans, bMask, aMask, crossing);
                    }
                }
            }
            if (lSub == 0) break;
        }
        return result;
    }

    /** 携带自身掩码的计划包装，便于组合时取对侧列。 */
    private static final class PlanPred {
        final PlanNode node;
        PlanPred(PlanNode node) { this.node = node; }
    }

    private List<PlanPred> wrap(List<PlanNode> nodes) {
        List<PlanPred> out = new ArrayList<>(nodes.size());
        for (PlanNode n : nodes) out.add(new PlanPred(n));
        return out;
    }

    private void addCombos(List<PlanNode> out, int mask,
                           List<PlanPred> lefts, List<PlanPred> rights,
                           int leftMask, int rightMask, List<JoinPred> crossing) {
        boolean cartesian = crossing.isEmpty();
        Stats ls = estimator.estimate(leftMask);
        Stats rs = estimator.estimate(rightMask);
        double outRows = estimator.joinRows(ls, rs, leftMask, rightMask, crossing);
        for (PlanPred l : lefts) {
            for (PlanPred r : rights) {
                double nodeCost = PlanNode.nodeCost(
                        cartesian ? PlanNode.CARTESIAN : PlanNode.HASH_JOIN,
                        l.node.estRows, r.node.estRows, outRows);
                out.add(PlanNode.join(cartesian, mask, crossing,
                        l.node, r.node, outRows, l.node.cost + r.node.cost + nodeCost));
            }
        }
    }

    /** 穷举结果汇总。 */
    public static final class Report {
        public final int totalTrees;
        public final double minCost;
        public final double maxCost;
        public final int optimalCount;
        public final PlanNode anOptimalPlan;
        public final long expectedTreeCount;

        Report(int totalTrees, double minCost, double maxCost, int optimalCount,
               PlanNode anOptimalPlan, long expectedTreeCount) {
            this.totalTrees = totalTrees;
            this.minCost = minCost;
            this.maxCost = maxCost;
            this.optimalCount = optimalCount;
            this.anOptimalPlan = anOptimalPlan;
            this.expectedTreeCount = expectedTreeCount;
        }

        public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("totalTrees", totalTrees);
            m.put("expectedTreeCount", expectedTreeCount);
            m.put("minCost", clean(minCost));
            m.put("maxCost", clean(maxCost));
            m.put("optimalCount", optimalCount);
            return m;
        }

        private static Object clean(double d) {
            if (d == Math.rint(d) && !Double.isInfinite(d)) return (long) d;
            return d;
        }
    }

    /** 穷举全部表并汇总最小代价等信息。 */
    public Report fullReport() {
        if (tables.size() > ENUM_LIMIT) {
            throw new IllegalArgumentException("穷举仅支持 ≤ " + ENUM_LIMIT + " 张表");
        }
        int full = (1 << tables.size()) - 1;
        List<PlanNode> all = enumerate(full);
        if (all.isEmpty()) {
            throw new IllegalStateException("穷举未得到任何完整计划（不应发生）");
        }
        double min = Double.POSITIVE_INFINITY;
        double max = Double.NEGATIVE_INFINITY;
        PlanNode bestOne = null;
        for (PlanNode p : all) {
            if (p.cost < min) { min = p.cost; bestOne = p; }
            if (p.cost > max) max = p.cost;
        }
        int ties = 0;
        for (PlanNode p : all) if (p.cost == min) ties++;
        long expected = exactTreeCount(full);
        return new Report(all.size(), min, max, ties, bestOne, expected);
    }

    /**
     * 合法完整树理论数量的精确计算（与穷举同一套合法分区规则，只数树不建树）。
     *
     * 对每个可达掩码 M 做计数 DP：
     * <pre>
     *   单表: T(M) = 1
     *   其余: T(M) = Σ_{无向合法分区 {A,B}} T(A)·T(B)·2
     *          （乘 2 来自左右两种朝向；A=B 不可能，因为分区两侧不交）
     * </pre>
     * 连通单分量场景退化为 f(n)=(2n-2)!/(n-1)!；
     * 无边场景（每个分量一张表）同样等于 f(n)。
     */
    public long exactTreeCount(int fullMask) {
        int n = Integer.bitCount(fullMask);
        Map<Integer, Long> t = new LinkedHashMap<>();
        for (int size = 1; size <= n; size++) {
            for (int mask = 1; mask <= fullMask; mask++) {
                if (Integer.bitCount(mask) != size) continue;
                if (size == 1) {
                    t.put(mask, 1L);
                    continue;
                }
                long sum = 0;
                int lowest = mask & -mask;
                int rest = mask ^ lowest;
                for (int lSub = rest; ; lSub = (lSub - 1) & rest) {
                    int aMask = lowest | lSub;
                    int bMask = mask ^ aMask;
                    if (bMask != 0 && t.containsKey(aMask) && t.containsKey(bMask)) {
                        boolean hasCross = !estimator.crossingPreds(aMask, bMask).isEmpty();
                        if (components.legalSplit(aMask, bMask, hasCross)) {
                            sum += t.get(aMask) * t.get(bMask) * 2L;
                        }
                    }
                    if (lSub == 0) break;
                }
                if (sum > 0) t.put(mask, sum);
            }
        }
        Long v = t.get(fullMask);
        return v == null ? 0 : v;
    }
}
