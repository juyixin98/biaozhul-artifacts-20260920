package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 暴力枚举：递归枚举“全部合法二叉计划”，求最小估计代价，与 DP 比对。
 *
 * 合法计划 = 任意把表集合分成两个非空子集，两侧各取任意合法子计划：
 *   有连接边 => 等值连接；无连接边 => 笛卡尔积。
 * 该空间完整覆盖稠密 DP 与左深 DP。二叉计划拓扑数按
 *   t(S) = Σ_{A 为 S 的真二分, A &lt; S\\A} t(A)·t(S\\A)
 * 递推：n=1..8 约为 1,1,3,15,105,945,10395,135135。
 *
 * 子问题候选使用与 {@link Optimizer} 完全相同的“边界签名”去重
 * （边界签名相同的子计划对任何上层连接等价，只留最小代价者，不改变最小代价）。
 * 因此枚举得到的最小值是无损失的全空间答案；Optimizer 与之相等即证明
 * 稠密 DP 没有因任何剪枝而损失最优值。
 */
public final class BruteForce {

    private final Model model;
    private final CostModel cm;
    private final boolean useProvided;

    private static final class Cand {
        final Stats stats;
        final double cost;
        Cand(Stats stats, double cost) { this.stats = stats; this.cost = cost; }
    }

    private List<Cand>[] dp;
    private long[] count;
    private long totalTrees;
    private long limit;
    private boolean capped;

    public BruteForce(Model model, CostModel cm, boolean useProvided, long maxTrees) {
        this.model = model;
        this.cm = cm;
        this.useProvided = useProvided;
        this.limit = maxTrees;
    }

    public static final class Report {
        public boolean run;
        public boolean capped;
        public long planCount;
        public double minCost;
        public double topRows;
        public int tables;
        public List<String> optimalSplits = new ArrayList<>();
    }

    public Report enumerate() {
        int total = 1 << model.n();
        dp = new List[total];
        count = new long[total];
        totalTrees = 0;
        capped = false;

        for (int i = 0; i < model.n(); i++) {
            int mask = 1 << i;
            Stats s = Optimizer.baseStats(model, i, useProvided);
            List<Cand> l = new ArrayList<>();
            l.add(new Cand(s, cm.scanCost(s)));
            dp[mask] = l;
            count[mask] = 1;
        }
        for (int mask = 1; mask < total; mask++) solve(mask);

        Report rep = new Report();
        int full = model.fullMask();
        rep.run = !capped;
        rep.capped = capped;
        rep.planCount = totalTrees;
        if (!capped) {
            double min = Double.POSITIVE_INFINITY;
            double rows = Double.POSITIVE_INFINITY;
            for (Cand c : dp[full]) {
                if (c.cost < min - 1e-9
                        || (Math.abs(c.cost - min) <= 1e-9 && c.stats.rowCount < rows)) {
                    min = c.cost;
                    rows = c.stats.rowCount;
                }
            }
            rep.minCost = min;
            rep.topRows = rows;
            rep.tables = model.n();
            rep.optimalSplits = traceOptimal(full, min);
        }
        return rep;
    }

    private void solve(int mask) {
        if (dp[mask] != null) return;
        Map<BoundarySig, Cand> bySig = new LinkedHashMap<>();
        long cnt = 0;

        int sub = (mask - 1) & mask;
        while (sub != 0) {
            int rest = mask ^ sub;
            if (rest != 0 && sub < rest) {
                solve(sub);
                solve(rest);
                if (capped) return;
                List<Edge> cross = model.crossingEdges(sub, rest);
                for (Cand a : dp[sub]) {
                    for (Cand b : dp[rest]) {
                        Stats stats = cm.estimateJoin(sub, rest, a.stats, b.stats, cross);
                        double cost = cm.joinCost(a.cost, b.cost, stats);
                        BoundarySig sig = new BoundarySig(stats, model.boundaryColumns(mask));
                        Cand cur = bySig.get(sig);
                        if (cur == null || cost < cur.cost - 1e-9) {
                            bySig.put(sig, new Cand(stats, cost));
                        }
                    }
                }
                long ways = count[sub] * count[rest];
                cnt += ways;
                totalTrees += ways;
                if (totalTrees > limit) { capped = true; return; }
            }
            sub = (sub - 1) & mask;
        }
        dp[mask] = new ArrayList<>(bySig.values());
        count[mask] = cnt;
    }

    private List<String> traceOptimal(int mask, double minCost) {
        List<String> out = new ArrayList<>();
        if (Integer.bitCount(mask) == 1) return out;
        int sub = (mask - 1) & mask;
        while (sub != 0) {
            int rest = mask ^ sub;
            if (rest != 0 && sub < rest) {
                for (Cand a : dp[sub]) {
                    for (Cand b : dp[rest]) {
                        Stats stats = cm.estimateJoin(sub, rest, a.stats, b.stats,
                                model.crossingEdges(sub, rest));
                        double cost = cm.joinCost(a.cost, b.cost, stats);
                        if (Math.abs(cost - minCost) <= 1e-6) {
                            String label = names(sub) + "  ⋈  " + names(rest)
                                    + (model.crossingEdges(sub, rest).isEmpty() ? "  (笛卡尔积)" : "");
                            if (!out.contains(label)) out.add(label);
                        }
                    }
                }
            }
            sub = (sub - 1) & mask;
        }
        return out;
    }

    private String names(int mask) {
        List<String> l = new ArrayList<>();
        int bits = mask;
        while (bits != 0) {
            int b = bits & -bits;
            l.add(model.table(Integer.numberOfTrailingZeros(b)).name);
            bits ^= b;
        }
        return l.toString();
    }
}
