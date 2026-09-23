package joinopt;

import java.util.List;

/**
 * 连接顺序动态规划优化器（System R 风格自底向上子集 DP）。
 *
 * <h2>状态定义</h2>
 * 表集合用位掩码表示（最多 8 张表，2^8=256 个子集）。
 * best[mask] = 覆盖该子集的、累计估计代价最小的 {@link PlanNode}。
 * 只有“连通子集”和“若干完整连通分量的并”可达（见 {@link Components}），
 * 其余掩码为 null，不会参与分区。
 *
 * <h2>转移</h2>
 * 对每个可达子集 S，枚举其所有无向真分区（令一侧含 S 的最低位，去掉重复），
 * 对每个<b>合法</b>分区再考虑两种子节点朝向（左右互换是两棵不同的树）：
 * <pre>
 *   分区两侧之间存在等值谓词 -> HASH JOIN（一次应用全部跨分区谓词）
 *   不存在谓词，但两侧各为完整分量的并 -> CARTESIAN（嵌套循环）
 *   其余分区非法，跳过
 *   cost(S) = cost(L) + cost(R) + nodeCost(本节点)
 * </pre>
 * 取所有候选中代价最小者；代价相同按计划规范串字典序打破平局，保证结果确定。
 *
 * <h2>断开的连接图</h2>
 * 当等值谓词构成的图存在多个连通分量时，分量之间没有等值谓词，完整计划必然
 * 包含笛卡尔积节点。DP 只在“完整分量之间”引入笛卡尔积，绝不把一个分量拆散
 * 后凭空做积（那类计划不合法）。由于笛卡尔积代价为 |L|×|R|，优化器会把
 * 分量间的积推迟到代价最小的位置。
 *
 * <h2>语义不变性</h2>
 * 估计只影响代价与顺序选择；所有合法计划表达的关系代数语义相同，
 * 执行器对任意合法顺序产生完全一致的结果行多重集合（见 Executor 与穷举测试）。
 */
public final class Optimizer {

    public static final int MAX_TABLES = 8;

    private final List<Table> tables;
    private final List<JoinPred> preds;
    private final Estimator estimator;
    private final Components components;

    private final int full;
    private final PlanNode[] best;
    private final double[] bestCost;

    public Optimizer(List<Table> tables, List<JoinPred> preds) {
        if (tables.isEmpty()) throw new IllegalArgumentException("至少需要 1 张表");
        if (tables.size() > MAX_TABLES) {
            throw new IllegalArgumentException("最多支持 " + MAX_TABLES + " 张表，当前 "
                    + tables.size() + " 张");
        }
        this.tables = tables;
        this.preds = preds;
        this.estimator = new Estimator(tables, preds);
        this.components = new Components(tables.size(), preds);
        this.full = (1 << tables.size()) - 1;
        this.best = new PlanNode[full + 1];
        this.bestCost = new double[full + 1];
        solve();
    }

    private void solve() {
        int n = tables.size();
        for (int t = 0; t < n; t++) {
            int mask = 1 << t;
            PlanNode leaf = PlanNode.leaf(t, tables.get(t).rowCount);
            leaf.estRows = tables.get(t).rowCount;
            leaf.cost = 0;
            best[mask] = leaf;
            bestCost[mask] = 0;
        }
        for (int size = 2; size <= n; size++) {
            for (int mask = 1; mask <= full; mask++) {
                if (Integer.bitCount(mask) != size) continue;
                PlanNode plan = buildBest(mask);
                if (plan != null) {
                    best[mask] = plan;
                    bestCost[mask] = plan.cost;
                }
            }
        }
    }

    /** 不可达子集返回 null。 */
    private PlanNode buildBest(int mask) {
        PlanNode winner = null;
        double winnerCost = Double.POSITIVE_INFINITY;
        String winnerCanonical = null;

        int lowest = mask & -mask;
        int rest = mask ^ lowest;
        for (int lSub = rest; ; lSub = (lSub - 1) & rest) {
            int aMask = lowest | lSub;
            int bMask = mask ^ aMask;
            if (bMask != 0 && best[aMask] != null && best[bMask] != null) {
                List<JoinPred> crossing = estimator.crossingPreds(aMask, bMask);
                boolean legal = components.legalSplit(aMask, bMask, !crossing.isEmpty());
                if (legal) {
                    // 两种子节点朝向各产生一棵候选树
                    PlanNode[] cands = {
                            candidateFor(mask, best[aMask], best[bMask], aMask, bMask, crossing),
                            candidateFor(mask, best[bMask], best[aMask], bMask, aMask, crossing)
                    };
                    for (PlanNode candidate : cands) {
                        if (candidate.cost < winnerCost
                                || (candidate.cost == winnerCost
                                    && winnerCanonical != null
                                    && candidate.canonical(tables).compareTo(winnerCanonical) < 0)) {
                            winner = candidate;
                            winnerCost = candidate.cost;
                            winnerCanonical = candidate.canonical(tables);
                        }
                    }
                }
            }
            if (lSub == 0) break;
        }
        return winner;
    }

    private PlanNode candidateFor(int mask, PlanNode leftPlan, PlanNode rightPlan,
                                  int leftMask, int rightMask, List<JoinPred> crossing) {
        boolean cartesian = crossing.isEmpty();
        double outRows = estimator.joinRows(
                estimator.estimate(leftMask), estimator.estimate(rightMask),
                leftMask, rightMask, crossing);
        double nodeCost = PlanNode.nodeCost(
                cartesian ? PlanNode.CARTESIAN : PlanNode.HASH_JOIN,
                leftPlan.estRows, rightPlan.estRows, outRows);
        double total = leftPlan.cost + rightPlan.cost + nodeCost;
        return PlanNode.join(cartesian, mask, crossing, leftPlan, rightPlan, outRows, total);
    }

    private static int minTable(int mask) {
        return Integer.numberOfTrailingZeros(mask);
    }

    public PlanNode bestPlan() {
        PlanNode p = best[full];
        if (p == null) {
            throw new IllegalStateException("无法为全部表构造连接计划（不应发生：分量之并总可做笛卡尔积）");
        }
        return p;
    }

    public Estimator estimator() {
        return estimator;
    }

    public Components components() {
        return components;
    }

    public List<JoinPred> predicates() {
        return preds;
    }

    /** 连通分量掩码列表（多于 1 个即连接图断开，完整计划必含笛卡尔积）。 */
    public List<Integer> connectedComponents() {
        return components.componentMasks;
    }

    /** 最优计划中是否包含笛卡尔积节点（连接图断开的直接标志）。 */
    public static boolean containsCartesian(PlanNode node) {
        if (node.isLeaf()) return false;
        return node.kind.equals(PlanNode.CARTESIAN)
                || containsCartesian(node.left) || containsCartesian(node.right);
    }
}
