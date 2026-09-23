package vecsearch.core;

import vecsearch.index.IndexOptions;
import vecsearch.index.IvfIndex;
import vecsearch.index.SearchIndex;
import vecsearch.index.StoreView;
import vecsearch.util.ApiException;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * 向量存储：单集合，单一距离度量（建集合时固定），支持：
 *
 * <ul>
 *   <li>维度校验：首次插入锁定维度，后续维度不符返回 400；</li>
 *   <li>余弦模式拒绝零向量（插入与检索都拒绝），L2 允许零向量；</li>
 *   <li>upsert / delete，近似索引随写操作同步更新（删除后绝不可被检索到）；</li>
 *   <li>精确暴力基线 {@link #exactSearch} 与近似索引 {@link #approxSearch}，
 *       两者共用 {@link DistanceMeter} 统计口径。</li>
 * </ul>
 *
 * <p>并发模型：ReentrantReadWriteLock，写串行、读（检索/快照）并发；
 * 每个 HttpServer worker 线程独立使用 meter 实例。
 */
public final class VectorStore {

    private final Metric metric;
    private final ReentrantReadWriteLock lock = new ReentrantReadWriteLock();

    /** id -> 向量（LinkedHashMap 保证快照顺序稳定，便于复现）。 */
    private final LinkedHashMap<String, TaggedVector> vectors = new LinkedHashMap<>();
    private int dimension = -1;

    private SearchIndex index;
    private IndexOptions indexOptions = IndexOptions.defaults();

    public VectorStore(Metric metric) {
        this.metric = metric;
    }

    public Metric metric() {
        return metric;
    }

    /** 当前锁定的维度；尚未写入任何向量时为 -1。 */
    public int dimension() {
        lock.readLock().lock();
        try {
            return dimension;
        } finally {
            lock.readLock().unlock();
        }
    }

    public int size() {
        lock.readLock().lock();
        try {
            return vectors.size();
        } finally {
            lock.readLock().unlock();
        }
    }

    private void validate(TaggedVector v) {
        if (dimension > 0 && v.vector().length != dimension) {
            throw ApiException.badRequest(
                    "dimension mismatch: collection dim=" + dimension
                            + " but vector '" + v.id() + "' has dim=" + v.vector().length);
        }
        if (metric == Metric.COSINE && Metric.isZeroVector(v.vector())) {
            throw ApiException.badRequest(
                    "zero vector is not allowed under COSINE metric (undefined angle): id="
                            + v.id());
        }
    }

    /** 插入或替换一条。返回 true 表示新增，false 表示覆盖已有 ID。 */
    public boolean upsert(TaggedVector v) {
        lock.writeLock().lock();
        try {
            validate(v);
            boolean isNew = !vectors.containsKey(v.id());
            vectors.put(v.id(), v);
            dimension = v.vector().length;
            if (index != null) {
                index.upsert(v);
            }
            return isNew;
        } finally {
            lock.writeLock().unlock();
        }
    }

    /** 批量 upsert，整体校验（任一条非法则全部不写入）。返回新增条数。 */
    public int upsertAll(Collection<TaggedVector> batch) {
        if (batch == null || batch.isEmpty()) {
            throw ApiException.badRequest("empty batch");
        }
        lock.writeLock().lock();
        try {
            // 先全部校验，再全部提交，保证批量原子性
            for (TaggedVector v : batch) {
                validate(v);
            }
            int inserted = 0;
            for (TaggedVector v : batch) {
                if (!vectors.containsKey(v.id())) {
                    inserted++;
                }
                vectors.put(v.id(), v);
                dimension = v.vector().length;
            }
            if (index != null) {
                for (TaggedVector v : batch) {
                    index.upsert(v);
                }
            }
            return inserted;
        } finally {
            lock.writeLock().unlock();
        }
    }

    /** 删除一条。返回 true 表示存在并删除。 */
    public boolean delete(String id) {
        lock.writeLock().lock();
        try {
            boolean removed = vectors.remove(id) != null;
            if (removed && index != null) {
                index.remove(id);
            }
            return removed;
        } finally {
            lock.writeLock().unlock();
        }
    }

    public TaggedVector get(String id) {
        lock.readLock().lock();
        try {
            return vectors.get(id);
        } finally {
            lock.readLock().unlock();
        }
    }

    public Map<String, Object> stats() {
        lock.readLock().lock();
        try {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("metric", metric.name());
            m.put("dimension", dimension < 0 ? null : dimension);
            m.put("count", vectors.size());
            m.put("index", index == null ? null : index.describe());
            return m;
        } finally {
            lock.readLock().unlock();
        }
    }

    // ------------------------------------------------------------------
    // 检索
    // ------------------------------------------------------------------

    private float[] validateQuery(float[] q) {
        if (q == null || q.length == 0) {
            throw ApiException.badRequest("query vector must be a non-empty array");
        }
        if (dimension > 0 && q.length != dimension) {
            throw ApiException.badRequest(
                    "query dimension mismatch: collection dim=" + dimension
                            + " but query dim=" + q.length);
        }
        if (metric == Metric.COSINE && Metric.isZeroVector(q)) {
            throw ApiException.badRequest(
                    "zero query vector is not allowed under COSINE metric");
        }
        if (dimension < 0) {
            // 空集合：维度还没锁定，不报错，返回空结果
        }
        return q;
    }

    /**
     * 精确基线：线性扫描全部存活向量，过滤后逐个算真实距离。
     */
    public SearchOutcome exactSearch(float[] query, int k, Map<String, String> filter) {
        if (k <= 0) {
            throw ApiException.badRequest("k must be positive");
        }
        lock.readLock().lock();
        try {
            validateQuery(query);
            DistanceMeter meter = new DistanceMeter(metric);
            TopK topk = new TopK(k);
            for (TaggedVector v : vectors.values()) {
                if (!v.matches(filter)) {
                    continue;
                }
                float d = meter.distance(query, v.vector());
                topk.offer(d, v.id());
            }
            return new SearchOutcome(materialize(topk), meter.computations(), true);
        } finally {
            lock.readLock().unlock();
        }
    }

    /**
     * 近似检索：必要时用默认参数懒构建 IVF 索引。
     *
     * @param nprobe 预算——探测最近的簇数；&lt;=0 时取全部簇（等价于精确扫描，
     *               但仍带簇中心比较成本，便于观察预算-召回曲线）
     */
    public SearchOutcome approxSearch(float[] query, int k, Map<String, String> filter,
                                      int nprobe) {
        if (k <= 0) {
            throw ApiException.badRequest("k must be positive");
        }
        // 1) 读锁内校验并检查索引是否存在
        lock.readLock().lock();
        boolean needsBuild;
        try {
            validateQuery(query);
            needsBuild = (index == null);
        } finally {
            lock.readLock().unlock();
        }
        // 2) 索引缺失：写锁双重检查后懒构建（默认参数）
        if (needsBuild) {
            lock.writeLock().lock();
            try {
                if (index == null) {
                    IvfIndex fresh = new IvfIndex(metric);
                    fresh.rebuild(snapshotUnderLock(), indexOptions);
                    index = fresh;
                }
            } finally {
                lock.writeLock().unlock();
            }
        }
        // 3) 读锁内执行检索
        lock.readLock().lock();
        try {
            validateQuery(query);
            int probes = nprobe;
            int clusters = index.clusterCount();
            if (probes <= 0 || probes > clusters) {
                probes = Math.max(clusters, 1);
            }
            DistanceMeter meter = new DistanceMeter(metric);
            List<SearchHit> hits = index.search(query, k, filter, probes, meter);
            return new SearchOutcome(hits, meter.computations(), false);
        } finally {
            lock.readLock().unlock();
        }
    }

    /** 用指定参数（重新）训练索引。返回索引描述信息。 */
    public Map<String, Object> rebuildIndex(IndexOptions options) {
        lock.writeLock().lock();
        try {
            this.indexOptions = options;
            IvfIndex fresh = new IvfIndex(metric);
            fresh.rebuild(snapshotUnderLock(), options);
            index = fresh;
            return fresh.describe();
        } finally {
            lock.writeLock().unlock();
        }
    }

    // ------------------------------------------------------------------
    // 内部
    // ------------------------------------------------------------------

    private List<SearchHit> materialize(TopK topk) {
        List<SearchHit> out = new ArrayList<>(topk.size());
        for (TopK.Node n : topk.drain()) {
            TaggedVector v = vectors.get(n.id());
            // drain 在同一读锁内，向量必然还在
            out.add(new SearchHit(n.id(), n.distance(), v.filter()));
        }
        return out;
    }

    /** 调用时必须持有（读或写）锁。 */
    private StoreView snapshotUnderLock() {
        List<StoreView.Item> items = new ArrayList<>(vectors.size());
        for (TaggedVector v : vectors.values()) {
            items.add(new StoreView.Item(v.id(), v.vector(), v.filter()));
        }
        int dim = dimension;
        Metric m = metric;
        return new StoreView() {
            @Override
            public List<Item> items() {
                return items;
            }

            @Override
            public int dimension() {
                return dim;
            }

            @Override
            public Metric metric() {
                return m;
            }
        };
    }
}
