package dev.dedup.hll;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 固定种子、多组基数的 HLL 误差分布实验。
 *
 * 实验设计（全部确定性可复现）：
 *  - 每组基数 n ∈ {100, 1 000, 10 000, 100 000, 1 000 000}，精度 p=12（m=4096，经典理论 RSE≈1.625%）；
 *  - trials=20 次独立试验，试验数据由 Random(trialSeed=20260922L + trialIndex) 顺序产出 64 位 h1，
 *    直接通过 add-hash 注入（不经过字符串层），模拟理想均匀哈希；
 *  - 每个试验同时验证“分片合并”：把同一条流切成 4 个分片草图，逐个 merge，
 *    合并结果必须与整体单草图完全一致（逐寄存器相同），并以合并结果计算误差；
 *  - 报告 mean/median/min/max/相对误差绝对值 p95、落在 ±1σ/±1.96σ 名义区间的比例，
 *    以及与理论 RSE 的样本标准差对比。
 *
 * 输出：results/error-distribution/report.json 与 report.md（命令行参数可覆盖）。
 */
public final class ErrorDistributionMain {

    static final long BASE_SEED = 20260922L;
    static final int PRECISION = 12;
    static final int SHARDS = 4;
    static final long[] CARDINALITIES = {100L, 1_000L, 10_000L, 100_000L, 1_000_000L};
    static final int TRIALS = 20;

    private ErrorDistributionMain() {
    }

    public static void main(String[] args) throws Exception {
        String outDir = args.length > 0 ? args[0] : "results/error-distribution";
        Files.createDirectories(Paths.get(outDir));

        List<Map<String, Object>> groups = new ArrayList<>();
        for (long n : CARDINALITIES) {
            groups.add(runGroup(n));
        }

        Map<String, Object> report = new LinkedHashMap<>();
        report.put("report", "HLL error distribution (fixed seeds, deterministic)");
        report.put("hashAlgorithm", Murmur3Hash128.HASH_ID);
        report.put("note",
                "本实验直接注入 Random 产出的 64 位 h1 模拟均匀哈希；误差含 HLL 自身波动与 64 位哈希碰撞（可忽略）。"
              + "所有数字均为估计值的统计，不是精确计数。");
        Map<String, Object> setup = new LinkedHashMap<>();
        setup.put("precision", PRECISION);
        setup.put("registerCount", 1 << PRECISION);
        setup.put("theoreticalRse", round6(1.04 / Math.sqrt(1 << PRECISION)));
        setup.put("theoreticalRsePct", round6(100 * 1.04 / Math.sqrt(1 << PRECISION)));
        setup.put("trialsPerCardinality", TRIALS);
        setup.put("shardsPerTrial", SHARDS);
        setup.put("trialSeedRule", "Random(20260922 + trialIndex)，顺序产出 n 个 64 位 h1");
        report.put("setup", setup);
        report.put("groups", groups);

        String json = Json.write(report, true);
        Files.write(Paths.get(outDir, "report.json"), json.getBytes(StandardCharsets.UTF_8));
        Files.write(Paths.get(outDir, "report.md"), renderMarkdown(report).getBytes(StandardCharsets.UTF_8));

        System.out.println(json);
        System.out.println();
        System.out.println("已写出: " + outDir + "/report.json, " + outDir + "/report.md");
    }

    private static Map<String, Object> runGroup(long n) {
        double[] relErrors = new double[TRIALS];
        int shardMismatchCount = 0;
        long[] estimates = new long[TRIALS];

        for (int t = 0; t < TRIALS; t++) {
            Random rnd = new Random(BASE_SEED + t);
            HllSketch whole = new HllSketch(new HllConfig(PRECISION));
            HllSketch[] shards = new HllSketch[SHARDS];
            for (int s = 0; s < SHARDS; s++) {
                shards[s] = new HllSketch(new HllConfig(PRECISION));
            }
            for (long i = 0; i < n; i++) {
                long h = rnd.nextLong();
                whole.addHash(h);
                shards[(int) (i % SHARDS)].addHash(h);
            }
            HllSketch merged = new HllSketch(new HllConfig(PRECISION));
            for (HllSketch sh : shards) {
                merged.mergeWith(sh);
            }
            if (!merged.equals(whole)) {
                shardMismatchCount++;
            }
            long est = merged.estimate();
            estimates[t] = est;
            relErrors[t] = (est - n) / (double) n;
        }

        Map<String, Object> g = new LinkedHashMap<>();
        g.put("trueCardinality", n);
        g.put("trials", TRIALS);
        g.put("shardMergeMismatches", shardMismatchCount);
        g.put("shardMergeExact", shardMismatchCount == 0);
        g.put("relativeErrorStats", relErrorStats(relErrors));
        double rse = 1.04 / Math.sqrt(1 << PRECISION);
        g.put("within1Sigma", countWithin(relErrors, rse) + "/" + TRIALS);
        g.put("within1_96Sigma", countWithin(relErrors, 1.96 * rse) + "/" + TRIALS);
        g.put("estimates", toBoxed(estimates));
        return g;
    }

    private static Map<String, Object> relErrorStats(double[] rel) {
        double[] sorted = rel.clone();
        Arrays.sort(sorted);
        double sum = 0;
        double absSum = 0;
        double sqSum = 0;
        double minAbs = Double.MAX_VALUE;
        double maxAbs = 0;
        for (double e : rel) {
            sum += e;
            absSum += Math.abs(e);
            sqSum += e * e;
            minAbs = Math.min(minAbs, Math.abs(e));
            maxAbs = Math.max(maxAbs, Math.abs(e));
        }
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("meanRelError", round6(sum / rel.length));
        s.put("meanAbsRelError", round6(absSum / rel.length));
        s.put("medianRelError", round6(median(sorted)));
        s.put("sampleStdRelError", round6(Math.sqrt(sqSum / rel.length)));
        s.put("minAbsRelError", round6(minAbs));
        s.put("p95AbsRelError", round6(absP95(sorted)));
        s.put("maxAbsRelError", round6(maxAbs));
        s.put("biasDirectionNote", "meanRelError<0 表示系统性低估，>0 表示高估");
        return s;
    }

    /** p95 绝对相对误差：对绝对值排序后取 95 分位。 */
    private static double absP95(double[] sortedRel) {
        double[] abs = new double[sortedRel.length];
        for (int i = 0; i < abs.length; i++) {
            abs[i] = Math.abs(sortedRel[i]);
        }
        Arrays.sort(abs);
        int idx = (int) Math.ceil(0.95 * abs.length) - 1;
        return abs[idx];
    }

    private static double median(double[] sorted) {
        int mid = sorted.length / 2;
        if (sorted.length % 2 == 0) {
            return (sorted[mid - 1] + sorted[mid]) / 2;
        }
        return sorted[mid];
    }

    private static int countWithin(double[] rel, double bound) {
        int c = 0;
        for (double e : rel) {
            if (Math.abs(e) <= bound) {
                c++;
            }
        }
        return c;
    }

    private static List<Long> toBoxed(long[] a) {
        List<Long> l = new ArrayList<>(a.length);
        for (long v : a) {
            l.add(v);
        }
        return l;
    }

    private static double round6(double d) {
        return Math.round(d * 1_000_000d) / 1_000_000d;
    }

    @SuppressWarnings("unchecked")
    private static String renderMarkdown(Map<String, Object> report) {
        Map<String, Object> setup = (Map<String, Object>) report.get("setup");
        StringBuilder sb = new StringBuilder();
        sb.append("# HLL 误差分布实验报告\n\n");
        sb.append("> 固定种子、确定性可复现。所有数值为**估计值的统计**，非精确计数。\n\n");
        sb.append("- 精度 p=").append(setup.get("precision"))
                .append("，m=").append(setup.get("registerCount"))
                .append("，理论 RSE ≈ ").append(setup.get("theoreticalRsePct")).append("%\n");
        sb.append("- 每组试验次数: ").append(setup.get("trialsPerCardinality"))
                .append("；每试验分片数: ").append(setup.get("shardsPerTrial")).append("\n");
        sb.append("- 种子规则: ").append(setup.get("trialSeedRule")).append("\n\n");
        sb.append("| 真实基数 | 平均相对误差 | 平均绝对相对误差 | 中位相对误差 | 样本Std | 最小绝对 | p95绝对 | 最大绝对 | ±1σ | ±1.96σ | 分片合并不一致次数 |\n");
        sb.append("|---|---|---|---|---|---|---|---|---|---|---|\n");
        for (Object go : (List<?>) report.get("groups")) {
            Map<String, Object> g = (Map<String, Object>) go;
            Map<String, Object> st = (Map<String, Object>) g.get("relativeErrorStats");
            sb.append("| ").append(g.get("trueCardinality"))
                    .append(" | ").append(pct(st.get("meanRelError")))
                    .append(" | ").append(pct(st.get("meanAbsRelError")))
                    .append(" | ").append(pct(st.get("medianRelError")))
                    .append(" | ").append(pct(st.get("sampleStdRelError")))
                    .append(" | ").append(pct(st.get("minAbsRelError")))
                    .append(" | ").append(pct(st.get("p95AbsRelError")))
                    .append(" | ").append(pct(st.get("maxAbsRelError")))
                    .append(" | ").append(g.get("within1Sigma"))
                    .append(" | ").append(g.get("within1_96Sigma"))
                    .append(" | ").append(g.get("shardMergeMismatches"))
                    .append(" |\n");
        }
        sb.append("\n±1σ/±1.96σ 列为落入名义对称区间的试验数（名义覆盖分别约 68%/95%，小样本下会有波动）。\n");
        return sb.toString();
    }

    private static String pct(Object fraction) {
        double v = ((Number) fraction).doubleValue() * 100;
        return String.format("%+.3f%%", v);
    }
}
