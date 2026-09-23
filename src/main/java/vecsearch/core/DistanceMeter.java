package vecsearch.core;

/**
 * 带计数的距离计算器：检索过程中每调用一次 {@link #distance} 即计一次"距离计算"，
 * 精确基线与近似索引使用同一套统计口径，保证两者成本可比。
 *
 * <p>非线程安全，每次检索创建一个实例（检索在 store 读锁内执行）。
 */
public final class DistanceMeter {
    private final Metric metric;
    private long computations;

    public DistanceMeter(Metric metric) {
        this.metric = metric;
    }

    public Metric metric() {
        return metric;
    }

    /** 计算两个原始向量之间的距离，计数 +1。 */
    public float distance(float[] a, float[] b) {
        computations++;
        return switch (metric) {
            case L2 -> Distances.l2(a, b);
            case COSINE -> Distances.cosine(a, b);
        };
    }

    /**
     * 两个已单位化向量之间的余弦距离（1 - dot）。与原始 cosine 数学等价，
     * 用于余弦索引内部；仍计一次距离计算。
     */
    public float normalizedCosine(float[] unitA, float[] unitB) {
        computations++;
        double dot = 0.0;
        for (int i = 0; i < unitA.length; i++) {
            dot += (double) unitA[i] * unitB[i];
        }
        if (dot > 1.0) {
            dot = 1.0;
        } else if (dot < -1.0) {
            dot = -1.0;
        }
        return (float) (1.0 - dot);
    }

    /** 平方 L2（仅 L2 索引内部使用），计一次距离计算。 */
    public float squaredL2(float[] a, float[] b) {
        computations++;
        return Distances.squaredL2(a, b);
    }

    public long computations() {
        return computations;
    }
}
