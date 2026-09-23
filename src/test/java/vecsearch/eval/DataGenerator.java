package vecsearch.eval;

import java.util.Random;

/**
 * 固定种子的合成数据生成器：多个高斯聚簇 + 少量离群点。
 *
 * <p>生成结果完全可复现：同一个 seed/dim/cluster 配置两次运行得到完全一致的数据。
 * 向量按 {@link DataPoint} 返回，携带标签（cluster 编号 / outlier 标记 / 组），供过滤检索使用。
 */
public final class DataGenerator {

    private DataGenerator() {
    }

    public record DataPoint(String id, float[] vector, int cluster, boolean outlier,
                            java.util.Map<String, String> tags) {
    }

    public record Dataset(java.util.List<DataPoint> points,
                          java.util.List<float[]> queries,
                          int dim, int clusters, int outliers, long seed) {
        public int size() {
            return points.size();
        }
    }

    /** 便捷重载：不含边界查询。 */
    public static Dataset generate(int dim, int clusters, int perCluster, int outliers,
                                   int queries, double clusterSpread, double pointNoise,
                                   long seed) {
        return generate(dim, clusters, perCluster, outliers, queries,
                clusterSpread, pointNoise, 0.0, seed);
    }

    /**
     * @param dim            向量维度
     * @param clusters       聚簇数量
     * @param perCluster     每个簇的点数
     * @param outliers       离群点数量（在空间中独立均匀散布）
     * @param queries        查询数量
     * @param clusterSpread  簇中心之间的距离尺度
     * @param pointNoise     簇内点的高斯噪声标准差
     * @param boundaryQueryFraction 查询中"边界查询"的比例（位于两个簇心之间，
     *                       其真近邻天然跨簇，用于暴露 nprobe 预算对召回的影响）；0~1
     * @param seed           随机种子
     */
    public static Dataset generate(int dim, int clusters, int perCluster, int outliers,
                                   int queries, double clusterSpread, double pointNoise,
                                   double boundaryQueryFraction, long seed) {
        Random rnd = new Random(seed);

        // 1) 簇中心：各分量 ~ N(0, clusterSpread^2)
        float[][] centers = new float[clusters][dim];
        for (int c = 0; c < clusters; c++) {
            for (int d = 0; d < dim; d++) {
                centers[c][d] = (float) (rnd.nextGaussian() * clusterSpread);
            }
        }

        java.util.List<DataPoint> points = new java.util.ArrayList<>(
                clusters * perCluster + outliers);
        int seq = 0;
        // 2) 簇内点：中心 + 高斯噪声
        for (int c = 0; c < clusters; c++) {
            for (int i = 0; i < perCluster; i++) {
                float[] v = gaussianAround(rnd, centers[c], pointNoise);
                java.util.Map<String, String> tags = new java.util.LinkedHashMap<>();
                tags.put("cluster", Integer.toString(c));
                tags.put("kind", "core");
                tags.put("group", (seq % 4 == 0) ? "A" : "B");
                points.add(new DataPoint("c%02d-%04d".formatted(c, i), v, c, false, tags));
                seq++;
            }
        }
        // 3) 离群点：大范围均匀分布，远在簇之外
        double outlierRange = clusterSpread * (2.0 + clusters * 0.5);
        for (int i = 0; i < outliers; i++) {
            float[] v = new float[dim];
            // 每个离群点先随机选一个"角落方向"，再放大，确保与簇分得开
            for (int d = 0; d < dim; d++) {
                double sign = rnd.nextBoolean() ? 1.0 : -1.0;
                v[d] = (float) (sign * (outlierRange + rnd.nextDouble() * outlierRange));
            }
            java.util.Map<String, String> tags = new java.util.LinkedHashMap<>();
            tags.put("cluster", "out");
            tags.put("kind", "outlier");
            tags.put("group", "O");
            points.add(new DataPoint("o-%04d".formatted(i), v, -1, true, tags));
            seq++;
        }

        // 4) 查询：
        //    边界查询（指定比例）：两簇心之间的插值点 + 噪声，真近邻天然跨簇；
        //    离群查询（约 10%）：指向离群区域；
        //    其余：贴着簇中心（模拟"有明确归属"的查询）。
        java.util.List<float[]> qs = new java.util.ArrayList<>(queries);
        for (int i = 0; i < queries; i++) {
            double roll = rnd.nextDouble();
            if (roll < boundaryQueryFraction && clusters >= 2) {
                int c1 = rnd.nextInt(clusters);
                int c2 = rnd.nextInt(clusters);
                while (c2 == c1) {
                    c2 = rnd.nextInt(clusters);
                }
                float[] q = new float[dim];
                double t = 0.3 + rnd.nextDouble() * 0.4; // 偏向 c1 的簇间点
                for (int d = 0; d < dim; d++) {
                    double val = centers[c1][d] * (1 - t) + centers[c2][d] * t;
                    q[d] = (float) (val + rnd.nextGaussian() * pointNoise * 0.5);
                }
                qs.add(q);
            } else if (roll < boundaryQueryFraction + 0.1) {
                float[] q = new float[dim];
                for (int d = 0; d < dim; d++) {
                    double sign = rnd.nextBoolean() ? 1.0 : -1.0;
                    q[d] = (float) (sign * (outlierRange + rnd.nextDouble() * outlierRange * 0.3));
                }
                qs.add(q);
            } else {
                int c = rnd.nextInt(clusters);
                qs.add(gaussianAround(rnd, centers[c], pointNoise * 0.5));
            }
        }

        return new Dataset(java.util.Collections.unmodifiableList(points),
                java.util.Collections.unmodifiableList(qs),
                dim, clusters, outliers, seed);
    }

    private static float[] gaussianAround(Random rnd, float[] center, double noise) {
        float[] v = new float[center.length];
        for (int d = 0; d < center.length; d++) {
            v[d] = (float) (center[d] + rnd.nextGaussian() * noise);
        }
        return v;
    }
}
