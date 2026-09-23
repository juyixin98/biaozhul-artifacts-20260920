package vecsearch.eval;

import vecsearch.core.Metric;
import vecsearch.core.SearchHit;
import vecsearch.core.SearchOutcome;
import vecsearch.core.TaggedVector;
import vecsearch.core.VectorStore;
import vecsearch.index.IndexOptions;
import vecsearch.json.JsonWriter;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * 验收程序（可直接运行，也被测试引用）。
 *
 * <p>对 L2 与 COSINE 两种度量：
 * <ol>
 *   <li>用固定种子生成"聚簇 + 离群点"数据并写入；</li>
 *   <li>以暴力精确检索为基线，对 IVF 在不同 nprobe（搜索预算）下测量：
 *       平均 recall@k、平均距离计算次数、扫描点占比；</li>
 *   <li>nprobe=nlist 时必须达到 recall=1.0（正确性回归）；</li>
 *   <li>验证过滤结果全部满足过滤条件、删除 ID 在任何预算下都不出现。</li>
 * </ol>
 */
public final class Evaluation {

    public record Config(int dim, int clusters, int perCluster, int outliers,
                         int queries, int k, int nlist, long seed,
                         double clusterSpread, double pointNoise,
                         double boundaryQueryFraction) {
        static Config l2() {
            // 16 个有重叠的高斯簇 + 40% 簇间边界查询，使 nprobe 预算对召回的影响清晰可见
            return new Config(24, 16, 110, 32, 120, 10, 32, 20260923L,
                    4.0, 1.1, 0.4);
        }

        static Config cosine() {
            // 余弦场景：簇心随机后由度量单位化保证角度聚簇；同样加入边界查询
            return new Config(24, 16, 110, 32, 120, 10, 32, 20260923L,
                    1.2, 0.22, 0.4);
        }
    }

    public record BudgetRow(int nprobe, double recall, double exactHits,
                            long approxComps, long exactComps,
                            double pointScanRatio, double centroidCompsShare) {
    }

    public record MetricReport(Metric metric, Config config, int n,
                               List<BudgetRow> rows,
                               boolean filterSatisfied,
                               boolean deletedNeverReturned,
                               long violations) {
    }

    // ------------------------------------------------------------------
    // 运行
    // ------------------------------------------------------------------

    public static MetricReport run(Metric metric, Config cfg) {
        DataGenerator.Dataset ds = DataGenerator.generate(
                cfg.dim(), cfg.clusters(), cfg.perCluster(), cfg.outliers(),
                cfg.queries(), cfg.clusterSpread(), cfg.pointNoise(),
                cfg.boundaryQueryFraction(), cfg.seed());

        VectorStore store = new VectorStore(metric);
        List<TaggedVector> vectors = new ArrayList<>(ds.size());
        for (DataGenerator.DataPoint p : ds.points()) {
            vectors.add(new TaggedVector(p.id(), p.vector(), p.tags()));
        }
        store.upsertAll(vectors);
        store.rebuildIndex(new IndexOptions(cfg.nlist(), 30, 42));

        // 预算曲线：nlist=32 时取 {1,2,4,8,16,32}
        int[] budgets = {1, 2, 4, 8, Math.max(1, cfg.nlist() / 2), cfg.nlist()};
        List<BudgetRow> rows = new ArrayList<>();
        for (int nprobe : budgets) {
            double recallSum = 0;
            long approxCompsSum = 0;
            long exactCompsSum = 0;
            long pointScansSum = 0;
            long centroidCompsSum = 0;
            for (float[] q : ds.queries()) {
                SearchOutcome exact = store.exactSearch(q, cfg.k(), null);
                SearchOutcome approx = store.approxSearch(q, cfg.k(), null, nprobe);

                TreeSet<String> gold = new TreeSet<>();
                for (SearchHit h : exact.hits()) {
                    gold.add(h.id());
                }
                int hit = 0;
                for (SearchHit h : approx.hits()) {
                    if (gold.contains(h.id())) {
                        hit++;
                    }
                }
                recallSum += (double) hit / gold.size();
                approxCompsSum += approx.computations();
                exactCompsSum += exact.computations();
                // IVF 成本 = nlist 次簇心比较 + 被扫描点的距离计算
                centroidCompsSum += cfg.nlist();
                pointScansSum += Math.max(0, approx.computations() - cfg.nlist());
            }
            int qn = ds.queries().size();
            int n = ds.size();
            rows.add(new BudgetRow(
                    nprobe,
                    round4(recallSum / qn),
                    cfg.k(),
                    approxCompsSum / qn,
                    exactCompsSum / qn,
                    round4((double) pointScansSum / qn / n),
                    round4((double) centroidCompsSum / qn / (approxCompsSum / (double) qn))
            ));
        }

        // ---- 过滤正确性：在所有预算下结果都必须满足过滤条件 ----
        boolean filterOk = true;
        long violations = 0;
        for (int nprobe : budgets) {
            for (float[] q : ds.queries()) {
                Map<String, String> f = Map.of("group", "A");
                SearchOutcome o = store.approxSearch(q, cfg.k(), f, nprobe);
                for (SearchHit h : o.hits()) {
                    if (!"A".equals(h.filter().get("group"))) {
                        filterOk = false;
                        violations++;
                    }
                }
            }
        }

        // ---- 删除正确性：删除一批跨簇的 ID，任何预算/过滤都不得返回它们 ----
        List<String> victims = pickSpreadIds(ds, 24);
        for (String id : victims) {
            store.delete(id);
        }
        boolean deletionOk = true;
        for (int nprobe : budgets) {
            for (float[] q : ds.queries()) {
                SearchOutcome o = store.approxSearch(q, ds.size(), null, nprobe);
                for (SearchHit h : o.hits()) {
                    if (victims.contains(h.id())) {
                        deletionOk = false;
                        violations++;
                    }
                }
                // 再叠加过滤
                SearchOutcome fo = store.approxSearch(q, ds.size(), Map.of("kind", "core"), nprobe);
                for (SearchHit h : fo.hits()) {
                    if (victims.contains(h.id())) {
                        deletionOk = false;
                        violations++;
                    }
                }
            }
        }

        return new MetricReport(metric, cfg, ds.size(), rows, filterOk, deletionOk, violations);
    }

    /** 均匀跨簇挑选 victim，保证各倒排链都被覆盖到。 */
    private static List<String> pickSpreadIds(DataGenerator.Dataset ds, int count) {
        List<String> picked = new ArrayList<>();
        int per = Math.max(1, count / (ds.clusters() + 1));
        int[] seen = new int[ds.clusters()];
        for (DataGenerator.DataPoint p : ds.points()) {
            if (picked.size() >= count) {
                break;
            }
            if (p.outlier()) {
                if (picked.size() % 5 == 0) {
                    picked.add(p.id());
                }
            } else if (seen[p.cluster()] < per) {
                picked.add(p.id());
                seen[p.cluster()]++;
            }
        }
        // 不足则从头补齐
        for (DataGenerator.DataPoint p : ds.points()) {
            if (picked.size() >= count) {
                break;
            }
            if (!picked.contains(p.id())) {
                picked.add(p.id());
            }
        }
        return picked;
    }

    private static double round4(double v) {
        return Math.round(v * 10000.0) / 10000.0;
    }

    // ------------------------------------------------------------------
    // 报告
    // ------------------------------------------------------------------

    public static Map<String, Object> toJson(MetricReport r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("metric", r.metric().name());
        Config c = r.config();
        Map<String, Object> ds = new LinkedHashMap<>();
        ds.put("seed", c.seed());
        ds.put("dim", c.dim());
        ds.put("clusters", c.clusters());
        ds.put("perCluster", c.perCluster());
        ds.put("outliers", c.outliers());
        ds.put("queries", c.queries());
        ds.put("n", r.n());
        ds.put("k", c.k());
        ds.put("nlist", c.nlist());
        ds.put("clusterSpread", c.clusterSpread());
        ds.put("pointNoise", c.pointNoise());
        ds.put("boundaryQueryFraction", c.boundaryQueryFraction());
        m.put("dataset", ds);
        List<Map<String, Object>> rows = new ArrayList<>();
        for (BudgetRow b : r.rows()) {
            Map<String, Object> x = new LinkedHashMap<>();
            x.put("nprobe", b.nprobe());
            x.put("recallAtK", b.recall());
            x.put("avgDistanceComputations", b.approxComps());
            x.put("exactBaselineComputations", b.exactComps());
            x.put("pointScanRatio", b.pointScanRatio());
            x.put("centroidShareOfComps", b.centroidCompsShare());
            rows.add(x);
        }
        m.put("budgets", rows);
        m.put("filterAlwaysSatisfied", r.filterSatisfied());
        m.put("deletedIdsNeverReturned", r.deletedNeverReturned());
        m.put("violations", r.violations());
        return m;
    }

    public static String toText(MetricReport r) {
        StringBuilder sb = new StringBuilder();
        Config c = r.config();
        sb.append("\n[").append(r.metric()).append("] ")
                .append("seed=").append(c.seed())
                .append(" dim=").append(c.dim())
                .append(" n=").append(r.n())
                .append(" (clusters=").append(c.clusters())
                .append(", outliers=").append(c.outliers())
                .append("), queries=").append(c.queries())
                .append(" k=").append(c.k())
                .append(" nlist=").append(c.nlist()).append('\n');
        sb.append(String.format(
                "  %-7s %-10s %-24s %-22s %-14s%n",
                "nprobe", "recall@k", "avgDistComps(ann/exact)", "pointScanned/n", "centroidShare"));
        for (BudgetRow b : r.rows()) {
            sb.append(String.format(
                    "  %-7d %-10.4f %-24s %-22.4f %-14.4f%n",
                    b.nprobe(), b.recall(),
                    b.approxComps() + "/" + b.exactComps(),
                    b.pointScanRatio(), b.centroidCompsShare()));
        }
        sb.append("  filter always satisfied : ").append(r.filterSatisfied()).append('\n');
        sb.append("  deleted never returned  : ").append(r.deletedNeverReturned())
                .append(" (violations=").append(r.violations()).append(")\n");
        return sb.toString();
    }

    public static void main(String[] args) {
        MetricReport l2 = run(Metric.L2, Config.l2());
        MetricReport cos = run(Metric.COSINE, Config.cosine());

        System.out.println("================ Acceptance Evaluation ================");
        System.out.print(toText(l2));
        System.out.print(toText(cos));

        Map<String, Object> all = new LinkedHashMap<>();
        all.put("reports", List.of(toJson(l2), toJson(cos)));
        System.out.println("\n-------- machine-readable JSON --------");
        System.out.println(JsonWriter.write(all));

        boolean pass = l2.filterSatisfied() && l2.deletedNeverReturned()
                && cos.filterSatisfied() && cos.deletedNeverReturned();
        // 全预算（nprobe=nlist）召回必须为 1
        for (MetricReport r : List.of(l2, cos)) {
            double fullProbeRecall = r.rows().get(r.rows().size() - 1).recall();
            if (Math.abs(fullProbeRecall - 1.0) > 1e-9) {
                pass = false;
                System.out.println("!! full-probe recall != 1 for " + r.metric()
                        + ": " + fullProbeRecall);
            }
        }
        System.out.println(pass ? "ACCEPTANCE CHECKS PASSED" : "ACCEPTANCE CHECKS FAILED");
        System.exit(pass ? 0 : 1);
    }
}
