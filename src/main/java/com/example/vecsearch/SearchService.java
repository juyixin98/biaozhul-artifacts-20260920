package com.example.vecsearch;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Search service: validates requests, runs exact brute-force search or
 * approximate IVF search, enforces metadata filters and search budgets,
 * and owns the lazily rebuilt approximate index.
 */
public class SearchService {

    /** Result bundle: hits plus counters describing the work performed. */
    public static final class Outcome {
        public final List<SearchHit> hits;
        public final long vectorDistances;
        public final long centroidDistances;
        public final boolean indexBuilt;
        public final int nprobeUsed;

        Outcome(List<SearchHit> hits, Distance.Counter counter,
                boolean indexBuilt, int nprobeUsed) {
            this.hits = hits;
            this.vectorDistances = counter.vectorDistances();
            this.centroidDistances = counter.centroidDistances();
            this.indexBuilt = indexBuilt;
            this.nprobeUsed = nprobeUsed;
        }
    }

    private final VectorStore store;

    // Approximate index, one per metric. Any mutation marks it dirty.
    private IvfIndex l2Index;
    private IvfIndex cosineIndex;
    private boolean dirty = false;

    // Index configuration.
    private final int nlist;
    private final int kmeansIterations;
    private final long kmeansSeed;

    public SearchService(VectorStore store, int nlist, int kmeansIterations, long kmeansSeed) {
        this.store = store;
        this.nlist = nlist;
        this.kmeansIterations = kmeansIterations;
        this.kmeansSeed = kmeansSeed;
    }

    // ------------------------------------------------------------------
    // Mutation
    // ------------------------------------------------------------------

    public synchronized boolean upsert(String id, double[] vector,
                                       Map<String, Object> metadata, Metric metric) {
        double norm = Distance.norm(vector);
        boolean replaced = store.upsert(id, vector, norm, metadata, metric);
        dirty = true;
        return replaced;
    }

    public synchronized boolean delete(String id) {
        boolean removed = store.delete(id);
        if (removed) {
            dirty = true;
        }
        return removed;
    }

    /** Force a rebuild of both metric indices now. */
    public synchronized void rebuildIndex() {
        List<VectorStore.Entry> all = store.snapshot();
        if (all.isEmpty()) {
            l2Index = null;
            cosineIndex = null;
            dirty = false;
            return;
        }
        l2Index = IvfIndex.build(all, Metric.L2, nlist, kmeansIterations, kmeansSeed);
        cosineIndex = IvfIndex.build(all, Metric.COSINE, nlist, kmeansIterations, kmeansSeed);
        dirty = false;
    }

    private IvfIndex indexFor(Metric metric) {
        return metric == Metric.L2 ? l2Index : cosineIndex;
    }

    // ------------------------------------------------------------------
    // Search
    // ------------------------------------------------------------------

    /**
     * @param mode   "exact" (brute force baseline) or "approx" (IVF)
     * @param budget optional cap on vector-vector distance calculations;
     *               null means unlimited
     * @param nprobe number of clusters to probe (approx only);
     *               if 0, a default of nlist/8 is used
     */
    public synchronized Outcome search(double[] query, Metric metric, int k,
                                       Filter filter, String mode,
                                       Long budget, int nprobe) {
        if (store.size() == 0) {
            throw new ApiException(409, "index is empty; insert vectors first");
        }
        if (query.length != store.dimension()) {
            throw new ApiException(400,
                    "dimension mismatch: query has " + query.length
                            + " dimensions, index has " + store.dimension());
        }
        double queryNorm = Distance.norm(query);
        if (metric == Metric.COSINE && queryNorm == 0.0) {
            throw new ApiException(400,
                    "zero query vector is not allowed under COSINE (cosine distance is undefined)");
        }
        if (k <= 0) {
            throw new ApiException(400, "k must be a positive integer");
        }
        if (budget != null && budget < 0) {
            throw new ApiException(400, "budget must be non-negative");
        }

        Distance.Counter counter = new Distance.Counter();
        List<SearchHit> hits;
        boolean built = false;
        int nprobeUsed = 0;

        if (mode == null || mode.equals("exact")) {
            hits = exactSearch(query, queryNorm, metric, k, filter, budget, counter);
        } else if (mode.equals("approx")) {
            if (dirty || indexFor(metric) == null) {
                rebuildIndex();
                built = true;
            }
            IvfIndex index = indexFor(metric);
            int effectiveNprobe = nprobe > 0
                    ? Math.min(nprobe, index.clusterCount())
                    : Math.max(1, index.clusterCount() / 8);
            nprobeUsed = effectiveNprobe;
            hits = approxSearch(index, query, queryNorm, metric, k, filter,
                    budget, effectiveNprobe, counter);
        } else {
            throw new ApiException(400, "unknown mode \"" + mode + "\" (use exact or approx)");
        }
        return new Outcome(hits, counter, built, nprobeUsed);
    }

    /** Exact brute-force baseline over every live vector. */
    private List<SearchHit> exactSearch(double[] query, double queryNorm, Metric metric,
                                        int k, Filter filter, Long budget,
                                        Distance.Counter counter) {
        // Max-heap of the k best (highest distance on top).
        PriorityQueue<SearchHit> heap = newTopKHeap(k);
        for (VectorStore.Entry e : store.snapshot()) {
            if (!filter.matches(e.metadata)) {
                continue; // filters skip without distance work
            }
            if (budget != null && counter.vectorDistances() >= budget) {
                break;
            }
            double dist = Distance.between(metric, query, queryNorm,
                    e.vector, e.norm, counter);
            offerIfBetter(heap, e, dist, k);
        }
        return drainOrdered(heap);
    }

    /** Approximate IVF search: probe nearest clusters only. */
    private List<SearchHit> approxSearch(IvfIndex index, double[] query, double queryNorm,
                                         Metric metric, int k, Filter filter,
                                         Long budget, int nprobe,
                                         Distance.Counter counter) {
        int[] order = index.rankClusters(query, queryNorm, counter);
        PriorityQueue<SearchHit> heap = newTopKHeap(k);
        outer:
        for (int p = 0; p < nprobe; p++) {
            for (VectorStore.Entry e : index.cluster(order[p])) {
                if (!filter.matches(e.metadata)) {
                    continue;
                }
                if (budget != null && counter.vectorDistances() >= budget) {
                    break outer;
                }
                double dist = Distance.between(metric, query, queryNorm,
                        e.vector, e.norm, counter);
                offerIfBetter(heap, e, dist, k);
            }
        }
        return drainOrdered(heap);
    }

    // ------------------------------------------------------------------
    // Top-k helpers
    // ------------------------------------------------------------------

    /** Max-heap keyed by distance; ties broken by id ascending for stable output. */
    private static PriorityQueue<SearchHit> newTopKHeap(int k) {
        return new PriorityQueue<>(k + 1, (a, b) -> {
            int cmp = Double.compare(b.distance(), a.distance());
            if (cmp != 0) return cmp;
            return b.id().compareTo(a.id());
        });
    }

    private static void offerIfBetter(PriorityQueue<SearchHit> heap,
                                      VectorStore.Entry e, double dist, int k) {
        if (heap.size() < k) {
            heap.add(new SearchHit(e.id, dist, e.metadata));
        } else if (dist < heap.peek().distance()
                || (dist == heap.peek().distance() && e.id.compareTo(heap.peek().id()) < 0)) {
            heap.poll();
            heap.add(new SearchHit(e.id, dist, e.metadata));
        }
    }

    private static List<SearchHit> drainOrdered(PriorityQueue<SearchHit> heap) {
        List<SearchHit> hits = new ArrayList<>(heap.size());
        while (!heap.isEmpty()) {
            hits.add(heap.poll());
        }
        java.util.Collections.reverse(hits);
        return hits;
    }

    // ------------------------------------------------------------------
    // Introspection
    // ------------------------------------------------------------------

    public synchronized Map<String, Object> stats() {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("vectors", store.size());
        s.put("dimension", store.dimension() < 0 ? null : store.dimension());
        s.put("nlist", nlist);
        s.put("indexDirty", dirty);
        IvfIndex ix = l2Index;
        s.put("indexTrained", ix != null && !dirty);
        s.put("indexClusters", ix == null ? 0 : ix.clusterCount());
        return s;
    }
}
