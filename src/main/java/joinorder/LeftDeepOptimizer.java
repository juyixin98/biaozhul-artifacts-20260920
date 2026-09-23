package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 左深连接顺序优化器（对照基线）。
 *
 * 状态 = 已接入的表集合；每一步只能把“一张”新基表放到右侧，与当前整棵左深树连接：
 *   新表与当前集合之间有边 => 等值连接；无边 => 笛卡尔积（笛卡尔积仍保持左深形状）。
 * 与稠密优化器一样保留“同代价、不同统计”的多个候选，避免边界 NDV 偏好反转。
 *
 * 复杂度 O(n²·2^n ·候选数)。左深是稠密搜索空间的真子集，故其最优代价 >= 稠密最优。
 */
public final class LeftDeepOptimizer {

    private final Model model;
    private final CostModel cm;
    private final boolean useProvided;

    public LeftDeepOptimizer(Model model, CostModel cm, boolean useProvided) {
        this.model = model;
        this.cm = cm;
        this.useProvided = useProvided;
    }

    private static final class Cand {
        final PlanNode plan;
        final Stats stats;
        final double cost;
        Cand(PlanNode plan, Stats stats, double cost) {
            this.plan = plan;
            this.stats = stats;
            this.cost = cost;
        }
    }

    public static final class Result {
        public PlanNode plan;
        public Map<Integer, OptEntry> memo;
        public long candidatesConsidered;
        public int cartesianJoins;
    }

    public Result optimize() {
        int n = model.n();
        int total = 1 << n;
        Map<Integer, List<Cand>> dp = new LinkedHashMap<>();
        Map<Integer, ScanNode> scans = new LinkedHashMap<>();
        long considered = 0;

        for (int i = 0; i < n; i++) {
            Stats s = Optimizer.baseStats(model, i, useProvided);
            ScanNode scan = new ScanNode(i);
            scan.estimated = s;
            scan.estimatedCost = cm.scanCost(s);
            scans.put(1 << i, scan);
            List<Cand> one = new ArrayList<>();
            one.add(new Cand(scan, s, scan.estimatedCost));
            dp.put(1 << i, one);
        }

        for (int mask = 1; mask < total; mask++) {
            List<Cand> cands = dp.get(mask);
            if (cands == null) continue;
            int remaining = model.fullMask() ^ mask;
            int bits = remaining;
            while (bits != 0) {
                int bit = bits & -bits;
                int ti = Integer.numberOfTrailingZeros(bit);
                ScanNode rightScan = scans.get(bit);
                List<Edge> cross = edgesFromMaskToTable(mask, ti);
                int key = mask | bit;
                List<Cand> target = dp.computeIfAbsent(key, k -> new ArrayList<>());
                Map<BoundarySig, Cand> bySig = new LinkedHashMap<>();
                for (Cand ex : target) {
                    bySig.put(new BoundarySig(ex.stats, model.boundaryColumns(key)), ex);
                }

                for (Cand cur : cands) {
                    considered++;
                    Stats s = cm.estimateJoin(mask, bit, cur.stats, rightScan.estimated, cross);
                    double cost = cm.joinCost(cur.cost, rightScan.estimatedCost, s);
                    BoundarySig sig = new BoundarySig(s, model.boundaryColumns(key));
                    Cand existing = bySig.get(sig);
                    if (existing == null || cost < existing.cost - 1e-9) {
                        JoinNode j = new JoinNode(cur.plan, rightScan, edgeIds(cross));
                        j.estimated = s;
                        j.estimatedCost = cost;
                        bySig.put(sig, new Cand(j, s, cost));
                    }
                }
                dp.put(key, new ArrayList<>(bySig.values()));
                bits ^= bit;
            }
        }

        List<Cand> tops = dp.get(model.fullMask());
        if (tops == null) throw new EngineException("内部错误：左深规划失败");
        Cand top = tops.get(0);
        for (Cand c : tops) {
            if (c.cost < top.cost - 1e-9
                    || (Math.abs(c.cost - top.cost) <= 1e-9
                        && c.stats.rowCount < top.stats.rowCount)) top = c;
        }

        Map<Integer, OptEntry> memo = new LinkedHashMap<>();
        for (Map.Entry<Integer, List<Cand>> e : dp.entrySet()) {
            Cand c = e.getValue().get(0);
            memo.put(e.getKey(), new OptEntry(e.getKey(), c.plan, c.stats, c.cost));
        }

        Result r = new Result();
        r.plan = top.plan;
        r.memo = memo;
        r.candidatesConsidered = considered;
        int[] cj = {0};
        countCj(top.plan, cj);
        r.cartesianJoins = cj[0];
        return r;
    }

    private List<Edge> edgesFromMaskToTable(int mask, int tableIdx) {
        List<Edge> r = new ArrayList<>();
        int bits = mask;
        while (bits != 0) {
            int bit = bits & -bits;
            int other = Integer.numberOfTrailingZeros(bit);
            Edge e = model.edgeBetween(other, tableIdx);
            if (e != null) r.add(e);
            bits ^= bit;
        }
        return r;
    }

    private List<Integer> edgeIds(List<Edge> cross) {
        List<Integer> ids = new ArrayList<>();
        for (Edge e : cross) ids.add(model.edges.indexOf(e));
        return ids;
    }

    private void countCj(PlanNode p, int[] c) {
        if (p instanceof JoinNode) {
            JoinNode j = (JoinNode) p;
            if (j.edgeIds.isEmpty()) c[0]++;
            countCj(j.left, c);
            countCj(j.right, c);
        }
    }
}
