package vecsearch.index;

import vecsearch.core.DistanceMeter;
import vecsearch.core.Distances;
import vecsearch.core.Metric;
import vecsearch.core.SearchHit;
import vecsearch.core.TaggedVector;
import vecsearch.core.TopK;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 自行实现的近似索引：IVF（Inverted File Index）。
 *
 * <h3>原理</h3>
 * <ol>
 *   <li>用固定种子的 k-means++ 初始化 + Lloyd 迭代，把向量空间划分为 {@code nlist} 个簇；</li>
 *   <li>每个向量（副本）挂到最近簇中心的倒排链上；</li>
 *   <li>查询时先用相同距离找最近的 {@code nprobe} 个簇，只扫描这些簇里的点；</li>
 *   <li>预算 {@code nprobe} 越小，距离计算越少、召回越低；{@code nprobe=nlist} 时扫描全集，
 *       召回回到精确水平。</li>
 * </ol>
 *
 * <h3>两种度量</h3>
 * <ul>
 *   <li>L2：簇中心为普通质心（均值），链中存原始向量；内部用平方 L2 比较，
 *       返回前对最终命中换算回真实 L2（开根号）；</li>
 *   <li>COSINE：簇中心与向量都使用单位化版本（球面 k-means：均值后再归一化），
 *       单位向量间 1-dot 即余弦距离；零向量在建集合阶段就已拒绝。</li>
 * </ul>
 *
 * <h3>删除正确性</h3>
 * 倒排链直接存主数据的 id；{@link #remove} 会从链上物理移除该 id，
 * 因此过滤与否，已删除 ID 都不可能出现在结果中。
 */
public final class IvfIndex implements SearchIndex {

    private final Metric metric;

    /** 每个簇的中心（余弦模式下为单位向量）。 */
    private float[][] centroids = new float[0][];
    /** 倒排链：每条存 (id, 用于距离计算的向量)。 */
    private final List<List<Entry>> postings = new ArrayList<>();
    /** id -> 簇编号，支撑增量 upsert/remove 的 O(1) 定位。 */
    private final LinkedHashMap<String, Integer> idToCluster = new LinkedHashMap<>();

    private int nlist;
    private int maxIters;
    private long seed;
    private int trainedSize;

    private record Entry(String id, float[] vec, Map<String, String> filter) {
    }

    public IvfIndex(Metric metric) {
        this.metric = metric;
    }

    @Override
    public String name() {
        return "ivf";
    }

    @Override
    public int indexedCount() {
        return idToCluster.size();
    }

    @Override
    public int clusterCount() {
        return centroids.length;
    }

    // ------------------------------------------------------------------
    // 训练
    // ------------------------------------------------------------------

    @Override
    public void rebuild(StoreView view, IndexOptions options) {
        List<StoreView.Item> items = view.items();
        this.maxIters = options.maxIters();
        this.seed = options.seed();

        // 准备用于聚类的向量表示（余弦：单位化；L2：原样）
        float[][] reps = new float[items.size()][];
        for (int i = 0; i < items.size(); i++) {
            reps[i] = asRep(items.get(i).vector());
        }

        int k = options.nlist() > 0 ? Math.min(options.nlist(), items.size()) : defaultNList(items.size());
        this.nlist = k;
        this.centroids = new float[0][];
        this.postings.clear();
        this.idToCluster.clear();

        if (items.isEmpty()) {
            this.trainedSize = 0;
            return;
        }
        if (k == 1 || items.size() == 1) {
            // 单点/单簇：中心取唯一表示即可
            float[][] c = new float[][]{normalizeIfCosine(reps[0].clone())};
            applyCentroids(c);
        } else {
            float[][] trained = kmeans(reps, k, options.maxIters(), options.seed());
            applyCentroids(trained);
        }

        // 全量挂载
        for (int i = 0; i < items.size(); i++) {
            StoreView.Item it = items.get(i);
            addToCluster(it.id(), reps[i], it.filter());
        }
        this.trainedSize = items.size();
    }

    private void applyCentroids(float[][] c) {
        this.centroids = c;
        this.postings.clear();
        for (int i = 0; i < c.length; i++) {
            this.postings.add(new ArrayList<>());
        }
    }

    private static int defaultNList(int n) {
        // 与常见经验值一致：约 sqrt(n)，夹在 [1, 64]
        int k = (int) Math.round(Math.sqrt(n));
        return Math.max(1, Math.min(64, k));
    }

    /**
     * k-means++ 初始化 + Lloyd 迭代。入参 reps 与返回中心都使用"表示向量"。
     */
    private float[][] kmeans(float[][] reps, int k, int iters, long rngSeed) {
        Random rnd = new Random(rngSeed);
        int n = reps.length;
        float[][] centers = new float[k][];

        // ---- k-means++ 初始化 ----
        double[] closestDist2 = new double[n];
        java.util.Arrays.fill(closestDist2, Double.POSITIVE_INFINITY);

        int first = rnd.nextInt(n);
        centers[0] = reps[first].clone();
        for (int c = 1; c < k; c++) {
            float[] prev = centers[c - 1];
            double total = 0.0;
            for (int i = 0; i < n; i++) {
                double d = repSquaredDistance(reps[i], prev);
                if (d < closestDist2[i]) {
                    closestDist2[i] = d;
                }
                total += closestDist2[i];
            }
            int chosen;
            if (total <= 0.0) {
                // 所有点重合：随机选一个（随机仍来自固定种子）
                chosen = rnd.nextInt(n);
            } else {
                double r = rnd.nextDouble() * total;
                chosen = n - 1;
                for (int i = 0; i < n; i++) {
                    r -= closestDist2[i];
                    if (r <= 0.0) {
                        chosen = i;
                        break;
                    }
                }
            }
            centers[c] = reps[chosen].clone();
        }

        // ---- Lloyd 迭代 ----
        int[] assign = new int[n];
        int dim = reps[0].length;
        for (int iter = 0; iter < iters; iter++) {
            boolean changed = false;
            for (int i = 0; i < n; i++) {
                int best = nearestCentroid(reps[i], centers);
                if (assign[i] != best) {
                    assign[i] = best;
                    changed = true;
                }
            }
            if (!changed && iter > 0) {
                break;
            }
            float[][] sums = new float[k][dim];
            int[] counts = new int[k];
            for (int i = 0; i < n; i++) {
                int c = assign[i];
                counts[c]++;
                for (int d = 0; d < dim; d++) {
                    sums[c][d] += reps[i][d];
                }
            }
            for (int c = 0; c < k; c++) {
                if (counts[c] == 0) {
                    // 空簇：抢一个离"该簇当前中心"最远的点过来（按当前度量），
                    // 退化情形（中心与所有点重合）退化为固定种子随机点
                    int farthest = pickFarthest(reps, centers[c], rnd);
                    centers[c] = reps[farthest].clone();
                    if (metric == Metric.COSINE) {
                        centers[c] = Distances.normalize(centers[c]);
                    }
                } else {
                    float[] mean = sums[c];
                    if (metric == Metric.COSINE) {
                        // 球面 k-means：均值向量重新归一化为单位中心
                        centers[c] = Distances.normalize(mean);
                    } else {
                        float inv = 1.0f / counts[c];
                        for (int d = 0; d < dim; d++) {
                            mean[d] *= inv;
                        }
                        centers[c] = mean;
                    }
                }
            }
        }
        return centers;
    }

    private int pickFarthest(float[][] reps, float[] center, Random rnd) {
        int best = -1;
        double bestD = -1.0;
        for (int i = 0; i < reps.length; i++) {
            double d = repSquaredDistance(reps[i], center);
            if (d > bestD) {
                bestD = d;
                best = i;
            }
        }
        if (best < 0 || bestD == 0.0) {
            // 全部点与中心重合：随机选点
            return rnd.nextInt(reps.length);
        }
        return best;
    }

    // ------------------------------------------------------------------
    // 增量维护
    // ------------------------------------------------------------------

    @Override
    public void upsert(TaggedVector v) {
        float[] rep = asRep(v.vector());
        Integer old = idToCluster.remove(v.id());
        if (old != null) {
            postings.get(old).removeIf(e -> e.id().equals(v.id()));
        }
        int target;
        if (centroids.length == 0) {
            // 空索引：为新点建立单簇
            float[][] c = new float[][]{normalizeIfCosine(rep.clone())};
            applyCentroids(c);
            target = 0;
        } else {
            target = nearestCentroid(rep, centroids);
        }
        addToCluster(v.id(), rep, v.filter());
    }

    @Override
    public boolean remove(String id) {
        Integer c = idToCluster.remove(id);
        if (c == null) {
            return false;
        }
        postings.get(c).removeIf(e -> e.id().equals(id));
        return true;
    }

    private void addToCluster(String id, float[] rep, Map<String, String> filter) {
        int c = nearestCentroid(rep, centroids);
        postings.get(c).add(new Entry(id, rep, filter));
        idToCluster.put(id, c);
    }

    // ------------------------------------------------------------------
    // 检索
    // ------------------------------------------------------------------

    @Override
    public List<SearchHit> search(float[] rawQuery, int k, Map<String, String> filter,
                                  int nprobe, DistanceMeter meter) {
        if (centroids.length == 0) {
            return List.of();
        }
        float[] q = asRep(rawQuery);

        // 1) 查询到所有簇心的距离，选最近的 nprobe 个
        int probes = Math.min(nprobe, centroids.length);
        TopK centroidTop = new TopK(probes);
        for (int c = 0; c < centroids.length; c++) {
            float d = pairDistance(q, centroids[c], meter);
            centroidTop.offer(d, Integer.toString(c));
        }
        int[] probeClusters = new int[probes];
        int idx = 0;
        for (TopK.Node node : centroidTop.drain()) {
            probeClusters[idx++] = Integer.parseInt(node.id());
        }

        // 2) 只扫描被探测簇的倒排链
        TopK top = new TopK(k);
        for (int c : probeClusters) {
            for (Entry e : postings.get(c)) {
                if (!filterMatches(e.filter(), filter)) {
                    continue;
                }
                float internal = pairDistance(q, e.vec(), meter);
                top.offer(internal, e.id());
            }
        }

        // 3) 换算回对外的真实距离（L2 内部用了平方距离；余弦两者相同）
        List<SearchHit> out = new ArrayList<>(top.size());
        for (TopK.Node n : top.drain()) {
            float d = n.distance();
            if (metric == Metric.L2) {
                d = (float) Math.sqrt(d);
            }
            Entry e = findEntry(n.id(), probeClusters);
            out.add(new SearchHit(n.id(), d, e == null ? Map.of() : e.filter()));
        }
        return out;
    }

    private Entry findEntry(String id, int[] clusters) {
        Integer c = idToCluster.get(id);
        if (c == null) {
            return null;
        }
        for (Entry e : postings.get(c)) {
            if (e.id().equals(id)) {
                return e;
            }
        }
        return null;
    }

    private static boolean filterMatches(Map<String, String> tags,
                                         Map<String, String> requested) {
        if (requested == null || requested.isEmpty()) {
            return true;
        }
        for (Map.Entry<String, String> e : requested.entrySet()) {
            String got = tags.get(e.getKey());
            if (got == null || !got.equals(e.getValue())) {
                return false;
            }
        }
        return true;
    }

    // ------------------------------------------------------------------
    // 表示向量 / 距离小工具
    // ------------------------------------------------------------------

    /** 索引内部使用的向量表示：余弦单位化，L2 原样。 */
    private float[] asRep(float[] raw) {
        if (metric == Metric.COSINE) {
            return Distances.normalize(raw);
        }
        return raw;
    }

    private float[] normalizeIfCosine(float[] v) {
        return metric == Metric.COSINE ? Distances.normalize(v) : v;
    }

    /**
     * 检索时的成对距离，同时计入 meter：
     * L2 用平方 L2（内部排序）；余弦用单位向量 1-dot。
     */
    private float pairDistance(float[] repA, float[] repB, DistanceMeter meter) {
        if (metric == Metric.L2) {
            return meter.squaredL2(repA, repB);
        }
        return meter.normalizedCosine(repA, repB);
    }

    /** 训练期不计检索成本：平方 L2 或单位向量 (1-dot) 的平方形式（单调等价，用平方即可）。 */
    private static double repSquaredDistance(float[] a, float[] b) {
        double sum = 0.0;
        for (int i = 0; i < a.length; i++) {
            double d = a[i] - b[i];
            sum += d * d;
        }
        return sum;
    }

    private static int nearestCentroid(float[] rep, float[][] centers) {
        int best = 0;
        double bestD = Double.MAX_VALUE;
        for (int c = 0; c < centers.length; c++) {
            double d = repSquaredDistance(rep, centers[c]);
            if (d < bestD) {
                bestD = d;
                best = c;
            }
        }
        return best;
    }

    @Override
    public Map<String, Object> describe() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", "ivf");
        m.put("metric", metric.name());
        m.put("nlist", nlist);
        m.put("actualClusters", centroids.length);
        m.put("indexedCount", indexedCount());
        m.put("trainedOn", trainedSize);
        m.put("maxIters", maxIters);
        m.put("seed", seed);
        List<Integer> sizes = new ArrayList<>(postings.size());
        for (List<Entry> l : postings) {
            sizes.add(l.size());
        }
        m.put("postingSizes", sizes);
        return m;
    }
}
