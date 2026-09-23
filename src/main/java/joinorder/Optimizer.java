package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 稠密（bushy）连接顺序动态规划优化器。
 *
 * dp[mask] = 表集合 mask 上的候选计划集合。枚举 mask 的全部非空真二分：
 *   二分跨越连接边 => 等值连接（使用该边上的全部谓词）；
 *   二分不跨越任何边 => 笛卡尔积。
 * 复杂度 O(3^n ·候选数²)，n<=8。
 *
 * 候选保留（正确性关键）：
 *   两个子计划根代价相同/相近但输出统计不同，放到更大的计划里可能优劣互换，
 *   因此经典“每子问题只留最小代价计划”是错的（本项目的随机图测试曾暴露该错误）。
 *   但只有“边界列”的 NDV 会影响该子计划之上的连接——边界列 = 出现在跨越 mask
 *   与外部表的连接边上、且属于 mask 一侧的列；内部列的 NDV 在 mask 之上的任何
 *   连接中都不会被读取。因此按“边界签名”（输出行数 + 各边界列 NDV）分组，
 *   每组只保留累计代价最小的候选。这既保证最优性，又把候选数压到 n<=8 可忽略。
 *
 * 语义安全性：仅当二分不跨越任何连接边时才生成笛卡尔积节点，等值谓词不会丢失
 * （跨边谓词要么在两侧子树内施加，要么在本次等值连接施加）。连接图的连通分量
 * 之间没有边，其最终组合必然是笛卡尔积，计划中标记 cartesian=true；连通分量
 * 内部的计划也可能包含降低总代价的中间笛卡尔积（该次分割同样不跨越任何边）。
 */
public final class Optimizer {

    private final Model model;
    private final CostModel cm;
    private final boolean useProvided;

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

    public Optimizer(Model model, CostModel cm, boolean useProvided) {
        this.model = model;
        this.cm = cm;
        this.useProvided = useProvided;
    }

    public static Stats baseStats(Model model, int idx, boolean useProvided) {
        Table t = model.table(idx);
        Stats src = useProvided && t.providedStats != null ? t.providedStats : t.actualStats;
        return src.copy();
    }

    public static final class Result {
        public PlanNode plan;
        public Map<Integer, OptEntry> memo;
        public long candidatesConsidered;
        public int cartesianJoins;
        public int connectedComponents;
        public boolean disconnected;
        public int maxCandidatesPerSubproblem;
        public List<String> warnings = new ArrayList<>();
    }

    @SuppressWarnings("unchecked")
    public Result optimize() {
        Result r = new Result();
        int n = model.n();
        Map<Integer, List<Cand>> dp = new LinkedHashMap<>();
        long considered = 0;

        for (int i = 0; i < n; i++) {
            Stats s = baseStats(model, i, useProvided);
            ScanNode scan = new ScanNode(i);
            scan.estimated = s;
            scan.estimatedCost = cm.scanCost(s);
            List<Cand> one = new ArrayList<>();
            one.add(new Cand(scan, s, scan.estimatedCost));
            dp.put(1 << i, one);
        }

        int full = model.fullMask();
        int maxCands = 1;
        for (int target = 1; target <= full; target++) {
            if (Integer.bitCount(target) < 2) continue;
            List<String> boundary = model.boundaryColumns(target);
            Map<BoundarySig, Cand> bestBySig = new LinkedHashMap<>();

            int sub = (target - 1) & target;
            while (sub != 0) {
                int rest = target ^ sub;
                if (rest != 0 && sub < rest) {
                    List<Cand> aList = dp.get(sub);
                    List<Cand> bList = dp.get(rest);
                    if (aList != null && bList != null) {
                        List<Edge> cross = model.crossingEdges(sub, rest);
                        for (Cand a : aList) {
                            for (Cand b : bList) {
                                considered++;
                                Stats s = cm.estimateJoin(sub, rest, a.stats, b.stats, cross);
                                double cost = cm.joinCost(a.cost, b.cost, s);
                                BoundarySig sig = new BoundarySig(s, boundary);
                                Cand cur = bestBySig.get(sig);
                                if (cur == null || cost < cur.cost - 1e-9) {
                                    JoinNode j = new JoinNode(a.plan, b.plan, edgeIds(cross));
                                    j.estimated = s;
                                    j.estimatedCost = cost;
                                    bestBySig.put(sig, new Cand(j, s, cost));
                                }
                            }
                        }
                    }
                }
                sub = (sub - 1) & target;
            }
            if (!bestBySig.isEmpty()) {
                List<Cand> cands = new ArrayList<>(bestBySig.values());
                dp.put(target, cands);
                maxCands = Math.max(maxCands, cands.size());
            }
        }

        List<int[]> comps = model.components();
        boolean disconnected = comps.size() > 1;

        List<Cand> topCands = dp.get(full);
        if (topCands == null) throw new EngineException("内部错误：未能生成完整查询计划");
        Cand top = cheapest(topCands);

        Map<Integer, OptEntry> memoDump = new LinkedHashMap<>();
        for (Map.Entry<Integer, List<Cand>> e : dp.entrySet()) {
            Cand c = cheapest(e.getValue());
            memoDump.put(e.getKey(), new OptEntry(e.getKey(), c.plan, c.stats, c.cost));
        }

        countCartesian(top.plan, r);
        r.plan = top.plan;
        r.memo = memoDump;
        r.candidatesConsidered = considered;
        r.connectedComponents = comps.size();
        r.disconnected = disconnected;
        r.maxCandidatesPerSubproblem = maxCands;
        if (r.disconnected) {
            List<String> names = new ArrayList<>();
            for (int[] c : comps) {
                List<String> tn = new ArrayList<>();
                for (int i : c) tn.add(model.table(i).name);
                names.add(tn.toString());
            }
            r.warnings.add("连接图不连通，检测到 " + comps.size() + " 个连通分量 " + names
                    + "；分量之间没有等值条件，将以笛卡尔积连接（计划中已标记 cartesian=true）");
        }
        return r;
    }

    private Cand cheapest(List<Cand> cands) {
        Cand best = cands.get(0);
        for (Cand c : cands) {
            if (c.cost < best.cost - 1e-9
                    || (Math.abs(c.cost - best.cost) <= 1e-9
                        && c.stats.rowCount < best.stats.rowCount)) best = c;
        }
        return best;
    }

    private void countCartesian(PlanNode p, Result r) {
        if (p instanceof JoinNode) {
            JoinNode j = (JoinNode) p;
            if (j.edgeIds.isEmpty()) r.cartesianJoins++;
            countCartesian(j.left, r);
            countCartesian(j.right, r);
        }
    }

    private List<Integer> edgeIds(List<Edge> cross) {
        List<Integer> ids = new ArrayList<>();
        for (Edge e : cross) ids.add(model.edges.indexOf(e));
        return ids;
    }
}
