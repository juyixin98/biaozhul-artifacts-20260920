package com.example.edcand;

/**
 * Unicode 码点 Levenshtein（编辑）距离。
 *
 * <p>操作集合为标准三项：插入、删除、替换，代价均为 1。
 * 计算单位是 {@link CodePoints} 展开的 <b>Unicode 码点</b>：
 * emoji 等增补平面字符由代理对表示，但只算 1 个单元；
 * 绝不使用 {@code String#getBytes()} 得到的字节序列。
 */
public final class Levenshtein {

    private Levenshtein() {
    }

    /** 两个字符串的码点 Levenshtein 距离。 */
    public static int distance(String a, String b) {
        return distance(CodePoints.of(a), CodePoints.of(b));
    }

    /** 两个码点序列的 Levenshtein 距离，经典两行 DP，O(m*n) 时间、O(min(m,n)) 空间。 */
    public static int distance(int[] a, int[] b) {
        if (a.length == 0) {
            return b.length;
        }
        if (b.length == 0) {
            return a.length;
        }
        // 让 b 是较短的序列以减小行宽
        if (b.length > a.length) {
            int[] t = a;
            a = b;
            b = t;
        }
        int[] prev = new int[b.length + 1];
        int[] cur = new int[b.length + 1];
        for (int j = 0; j <= b.length; j++) {
            prev[j] = j;
        }
        for (int i = 1; i <= a.length; i++) {
            cur[0] = i;
            int ca = a[i - 1];
            for (int j = 1; j <= b.length; j++) {
                int cost = (ca == b[j - 1]) ? 0 : 1;
                cur[j] = Math.min(
                        Math.min(cur[j - 1] + 1, prev[j] + 1),
                        prev[j - 1] + cost);
            }
            int[] tmp = prev;
            prev = cur;
            cur = tmp;
        }
        return prev[b.length];
    }

    /**
     * 阈值受限（saturating）距离：
     * <ul>
     *   <li>若真实距离 d &le; threshold，返回精确的 d（供候选的最终精算确认）；</li>
     *   <li>若 d &gt; threshold，返回 threshold + 1（仅表示“超阈”，具体值无意义）。</li>
     * </ul>
     *
     * <p>实现为完整 DP 矩阵的“饱和”版本：每个单元以 threshold+1 为上限。
     * 饱和是安全的——加法的各项都在同一上限截断，min-plus 递推不会把
     * &le; threshold 的真实值误判成 threshold+1（任何最优路径经过的中间状态
     * 都不超过最终距离 d）。此外若某一行的所有单元都 &gt; threshold，
     * 之后每行第 0 列单调递增（i），不可能再回落，可提前退出。
     *
     * <p>这不是近似算法：对“是否在阈值内”的判定与完整 DP 完全一致，
     * 并且在阈值内时给出精确距离。
     */
    public static int cappedDistance(int[] a, int[] b, int threshold) {
        if (threshold < 0) {
            throw new IllegalArgumentException("threshold must be >= 0");
        }
        if (Math.abs(a.length - b.length) > threshold) {
            return threshold + 1;
        }
        final int cap = threshold + 1;
        int[] prev = new int[b.length + 1];
        int[] cur = new int[b.length + 1];
        for (int j = 0; j <= b.length; j++) {
            prev[j] = Math.min(j, cap);
        }
        for (int i = 1; i <= a.length; i++) {
            cur[0] = Math.min(i, cap);
            int rowMin = cur[0];
            int ca = a[i - 1];
            for (int j = 1; j <= b.length; j++) {
                int cost = (ca == b[j - 1]) ? 0 : 1;
                int v = Math.min(
                        Math.min(cur[j - 1] + 1, prev[j] + 1),
                        prev[j - 1] + cost);
                if (v > cap) {
                    v = cap;
                }
                cur[j] = v;
                if (v < rowMin) {
                    rowMin = v;
                }
            }
            if (rowMin >= cap) {
                return cap; // 本行已全部超阈，后续只会更差
            }
            int[] tmp = prev;
            prev = cur;
            cur = tmp;
        }
        return prev[b.length];
    }

    /** 字符串版本的阈值受限距离。 */
    public static int cappedDistance(String a, String b, int threshold) {
        return cappedDistance(CodePoints.of(a), CodePoints.of(b), threshold);
    }
}
