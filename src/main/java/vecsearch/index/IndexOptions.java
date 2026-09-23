package vecsearch.index;

/**
 * 索引构建参数。
 *
 * @param nlist     聚类簇数量；&lt;=0 时由实现自行选择
 * @param maxIters  k-means 最大迭代次数；&lt;=0 时使用默认值
 * @param seed      随机种子（固定后索引训练完全可复现）
 */
public record IndexOptions(int nlist, int maxIters, long seed) {

    public static final int DEFAULT_MAX_ITERS = 25;
    public static final long DEFAULT_SEED = 42L;

    public IndexOptions {
        if (maxIters <= 0) {
            maxIters = DEFAULT_MAX_ITERS;
        }
    }

    public static IndexOptions defaults() {
        return new IndexOptions(0, DEFAULT_MAX_ITERS, DEFAULT_SEED);
    }
}
