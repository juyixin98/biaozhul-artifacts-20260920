package vecsearch.core;

/**
 * 支持的距离（相似度）度量。
 *
 * <ul>
 *   <li>L2：欧氏距离 sqrt(sum((a-b)^2))。任何向量都合法，包括零向量。</li>
 *   <li>COSINE：余弦距离 1 - dot(a,b)/(|a||b|)。零向量没有定义，插入/检索一律拒绝。</li>
 * </ul>
 */
public enum Metric {
    L2,
    COSINE;

    public static Metric fromString(String s) {
        if (s == null) {
            return null;
        }
        String up = s.trim().toUpperCase();
        switch (up) {
            case "L2":
            case "EUCLIDEAN":
                return L2;
            case "COSINE":
            case "COS":
                return COSINE;
            default:
                return null;
        }
    }

    /** 判断向量是否为零向量（所有分量都为 0）。 */
    public static boolean isZeroVector(float[] v) {
        for (float x : v) {
            if (x != 0.0f) {
                return false;
            }
        }
        return true;
    }
}
