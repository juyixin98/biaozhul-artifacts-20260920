package vecsearch.core;

/**
 * 距离计算工具。
 *
 * <p>所有距离比较逻辑都通过本类完成，便于统计"距离计算次数"（每次两向量间的距离计算计 1 次），
 * 作为检索预算/成本的客观指标。
 */
public final class Distances {

    private Distances() {
    }

    /** 欧氏距离（L2）：sqrt(sum((a_i-b_i)^2))。平方 L2 与 L2 排序一致，但这里返回真实 L2 距离。 */
    public static float l2(float[] a, float[] b) {
        checkSameDim(a, b);
        double sum = 0.0;
        for (int i = 0; i < a.length; i++) {
            double d = (double) a[i] - b[i];
            sum += d * d;
        }
        return (float) Math.sqrt(sum);
    }

    /** 平方 L2，供索引内部排序使用（少开一次根号，不改变近邻顺序）。 */
    public static float squaredL2(float[] a, float[] b) {
        checkSameDim(a, b);
        double sum = 0.0;
        for (int i = 0; i < a.length; i++) {
            double d = (double) a[i] - b[i];
            sum += d * d;
        }
        return (float) sum;
    }

    /**
     * 余弦距离 = 1 - cos，取值范围约 [0, 2]，越小越相似。
     *
     * @throws IllegalArgumentException 任一向量为零向量（余弦距离对零向量没有定义）
     */
    public static float cosine(float[] a, float[] b) {
        checkSameDim(a, b);
        double dot = 0.0;
        double na = 0.0;
        double nb = 0.0;
        for (int i = 0; i < a.length; i++) {
            double x = a[i];
            double y = b[i];
            dot += x * y;
            na += x * x;
            nb += y * y;
        }
        if (na == 0.0 || nb == 0.0) {
            throw new IllegalArgumentException(
                    "cosine distance is undefined for zero vectors");
        }
        double cos = dot / (Math.sqrt(na) * Math.sqrt(nb));
        // 浮点误差可能略微越界，钳制到理论范围
        if (cos > 1.0) {
            cos = 1.0;
        } else if (cos < -1.0) {
            cos = -1.0;
        }
        return (float) (1.0 - cos);
    }

    /** L2 范数。 */
    public static float norm(float[] v) {
        double sum = 0.0;
        for (float x : v) {
            sum += (double) x * x;
        }
        return (float) Math.sqrt(sum);
    }

    /** 返回单位化后的新向量；零向量会被拒绝。 */
    public static float[] normalize(float[] v) {
        double sum = 0.0;
        for (float x : v) {
            sum += (double) x * x;
        }
        if (sum == 0.0) {
            throw new IllegalArgumentException("cannot normalize a zero vector");
        }
        float inv = (float) (1.0 / Math.sqrt(sum));
        float[] out = new float[v.length];
        for (int i = 0; i < v.length; i++) {
            out[i] = v[i] * inv;
        }
        return out;
    }

    private static void checkSameDim(float[] a, float[] b) {
        if (a.length != b.length) {
            throw new IllegalArgumentException(
                    "dimension mismatch: " + a.length + " vs " + b.length);
        }
    }
}
