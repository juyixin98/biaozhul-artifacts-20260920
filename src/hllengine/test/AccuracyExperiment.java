package hllengine.test;

import hllengine.hll.HllConfig;
import hllengine.hll.HllSketch;
import hllengine.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Fixed-seed empirical error-distribution experiment.
 *
 * <p>For each (precision, cardinality) pair we run many independent trials.
 * Every trial is reproducible: trial {@code t} derives its SplitMix64 stream
 * from {@code (MASTER_SEED, p, n, t)} and draws {@code n} <b>distinct</b>
 * 64-bit values (the first {@code n} outputs of a permutation generator are
 * distinct by construction within a trial). This measures exactly the
 * estimator's multiplicative error on distinct keys.
 *
 * <p>Outputs (under {@code reports/} by default):
 * <ul>
 *   <li>{@code accuracy_trials.csv} &ndash; one row per trial;</li>
 *   <li>{@code accuracy_summary.csv} &ndash; per-group distribution stats;</li>
 *   <li>{@code accuracy_report.md} &ndash; human-readable summary including
 *       empirical standard deviation vs the nominal 1.04/&radic;m.</li>
 * </ul>
 */
public final class AccuracyExperiment {

    /** Fixed master seed; the whole experiment is deterministic from this constant. */
    public static final long MASTER_SEED = 0x5DEECE66DL;

    static final int[] CARDINALITIES = {0, 1, 10, 100, 1_000, 10_000, 100_000, 1_000_000};
    static final int[] MAIN_PRECISIONS = {10, 12, 14};
    // Extra single-precision rows to show the trend with n cheaply.
    static final int DEFAULT_TRIALS = 40;

    /** Deterministic 64-bit permutation (SplitMix64). */
    static long splitMix64(long z) {
        z += 0x9e3779b97f4a7c15L;
        z = (z ^ (z >>> 30)) * 0xbf58476d1ce4e5b9L;
        z = (z ^ (z >>> 27)) * 0x94d049bb133111ebL;
        return z ^ (z >>> 31);
    }

    static long trialSeed(int precision, long cardinality, int trial) {
        long h = MASTER_SEED;
        h ^= splitMix64(precision * 0x9E3779B97F4A7C15L);
        h ^= splitMix64(cardinality ^ 0xC2B2AE3D27D4EB4FL);
        h ^= splitMix64((long) trial * 0xD1B54A32D192ED03L);
        return splitMix64(h);
    }

    /** Runs one estimation: n distinct values, precision p, trial stream t. */
    static double runTrial(int precision, long n, int trial) {
        HllSketch sketch = new HllSketch(HllConfig.of(precision, HllConfig.DEFAULT_SEED));
        long z = trialSeed(precision, n, trial);
        for (long k = 0; k < n; k++) {
            z += 0x9e3779b97f4a7c15L;
            sketch.offerLong(splitMix64(z));
        }
        return sketch.estimate().rawEstimate();
    }

    public Map<String, Object> run(int trialsPerGroup, Path outDir) {
        List<Map<String, Object>> trialRows = new ArrayList<>();
        List<Map<String, Object>> summaryRows = new ArrayList<>();

        for (int p : MAIN_PRECISIONS) {
            for (long n : CARDINALITIES) {
                List<Double> relErrors = new ArrayList<>();
                double sumEstimate = 0;
                for (int t = 0; t < trialsPerGroup; t++) {
                    double estimate = runTrial(p, n, t);
                    sumEstimate += estimate;
                    double rel = n == 0 ? 0.0 : (estimate - n) / (double) n;
                    relErrors.add(rel);
                    Map<String, Object> row = new LinkedHashMap<>();
                    row.put("precision", p);
                    row.put("cardinality", n);
                    row.put("trial", t);
                    row.put("estimate", estimate);
                    row.put("absoluteError", estimate - n);
                    row.put("relativeError", rel);
                    trialRows.add(row);
                }
                summaryRows.add(summarize(p, n, trialsPerGroup, relErrors, sumEstimate));
            }
        }

        try {
            Files.createDirectories(outDir);
            writeCsv(outDir.resolve("accuracy_trials.csv"), List.of(
                    "precision", "cardinality", "trial", "estimate",
                    "absoluteError", "relativeError"), trialRows);
            writeCsv(outDir.resolve("accuracy_summary.csv"), List.of(
                    "precision", "cardinality", "trials", "meanEstimate",
                    "meanRelativeBias", "stddevRelativeError", "minRelativeError",
                    "maxRelativeError", "abs95thPercentileRelativeError",
                    "nominalRelativeStandardError", "empiricalOverNominalSigma"), summaryRows);
            writeMarkdown(outDir.resolve("accuracy_report.md"), summaryRows, trialsPerGroup);
        } catch (IOException ioe) {
            throw new RuntimeException("failed writing reports: " + ioe.getMessage(), ioe);
        }

        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("ok", true);
        summary.put("masterSeed", String.format("0x%016X", MASTER_SEED));
        summary.put("trialsPerGroup", trialsPerGroup);
        summary.put("precisions", MAIN_PRECISIONS);
        summary.put("cardinalities", CARDINALITIES);
        summary.put("groups", summaryRows.size());
        summary.put("totalTrials", trialRows.size());
        summary.put("outDir", outDir.toString());
        summary.put("summary", summaryRows);
        return summary;
    }

    private Map<String, Object> summarize(int p, long n, int trials,
                                          List<Double> relErrors, double sumEstimate) {
        double meanBias = 0;
        double min = Double.POSITIVE_INFINITY, max = Double.NEGATIVE_INFINITY;
        double sumSq = 0;
        List<Double> sorted = new ArrayList<>();
        for (double r : relErrors) {
            meanBias += r;
            sumSq += r * r;
            if (r < min) min = r;
            if (r > max) max = r;
            sorted.add(Math.abs(r));
        }
        meanBias /= trials;
        double variance = sumSq / trials - meanBias * meanBias;
        double stddev = Math.sqrt(Math.max(0, variance));
        sorted.sort(Double::compare);
        double p95 = sorted.get((int) Math.ceil(0.95 * trials) - 1);
        double nominal = HllConfig.of(p, 0).relativeStandardError();

        Map<String, Object> m = new LinkedHashMap<>();
        m.put("precision", p);
        m.put("cardinality", n);
        m.put("trials", trials);
        m.put("meanEstimate", sumEstimate / trials);
        m.put("meanRelativeBias", meanBias);
        m.put("stddevRelativeError", stddev);
        m.put("minRelativeError", n == 0 ? 0.0 : min);
        m.put("maxRelativeError", n == 0 ? 0.0 : max);
        m.put("abs95thPercentileRelativeError", n == 0 ? 0.0 : p95);
        m.put("nominalRelativeStandardError", nominal);
        m.put("empiricalOverNominalSigma", stddev / nominal);
        return m;
    }

    private void writeCsv(Path path, List<String> headers, List<Map<String, Object>> rows)
            throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append(String.join(",", headers)).append('\n');
        for (Map<String, Object> row : rows) {
            for (int k = 0; k < headers.size(); k++) {
                if (k > 0) sb.append(',');
                Object v = row.get(headers.get(k));
                sb.append(csv(v));
            }
            sb.append('\n');
        }
        Files.writeString(path, sb.toString(), StandardCharsets.UTF_8);
    }

    private String csv(Object v) {
        if (v == null) return "";
        if (v instanceof Double d) return String.format(Locale.ROOT, "%.8f", d);
        return String.valueOf(v);
    }

    private void writeMarkdown(Path path, List<Map<String, Object>> rows, int trials)
            throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("# HyperLogLog 固定种子误差分布实验\n\n");
        sb.append("- 哈希算法: `MURMUR3_X64_128`（固定），种子 0\n");
        sb.append("- 主随机种子: `").append(String.format("0x%016X", MASTER_SEED)).append("` (SplitMix64)\n");
        sb.append("- 每组试验数: ").append(trials).append('\n');
        sb.append("- 每组输入为 ").append(trials).append(" 组互相独立的 **精确 n 个不同 64 位值**\n");
        sb.append("- 所有数字都是**估计值的统计**，不是精确去重结果\n\n");
        sb.append("| p | m | 基数 n | 平均估计 | 平均相对偏差 | 经验相对标准差 | 理论σ=1.04/√m | 经验/理论 | 95分位\\|误差\\| |\n");
        sb.append("|---|---|--------:|----------:|-------------:|---------------:|---------------:|----------:|----------------:|\n");
        for (Map<String, Object> r : rows) {
            int p = (int) r.get("precision");
            sb.append(p).append('|').append(1 << p).append('|')
                    .append(r.get("cardinality")).append('|')
                    .append(fmt(((Number) r.get("meanEstimate")).doubleValue())).append('|')
                    .append(pct(((Number) r.get("meanRelativeBias")).doubleValue())).append('|')
                    .append(pct(((Number) r.get("stddevRelativeError")).doubleValue())).append('|')
                    .append(pct(((Number) r.get("nominalRelativeStandardError")).doubleValue())).append('|')
                    .append(fmt(((Number) r.get("empiricalOverNominalSigma")).doubleValue())).append('|')
                    .append(pct(((Number) r.get("abs95thPercentileRelativeError")).doubleValue()))
                    .append("|\n");
        }
        sb.append("\n说明：`经验相对标准差` 是 ").append(trials)
                .append(" 次试验的样本分布；理论 σ 是先验值，二者接近即说明实现符合经典 HLL 的误差特性。\n");
        Files.writeString(path, sb.toString(), StandardCharsets.UTF_8);
    }

    private String fmt(double d) {
        return String.format(Locale.ROOT, "%.4f", d);
    }

    private String pct(double d) {
        return String.format(Locale.ROOT, "%.3f%%", d * 100);
    }
}
