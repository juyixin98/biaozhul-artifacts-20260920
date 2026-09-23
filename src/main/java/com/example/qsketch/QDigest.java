package com.example.qsketch;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * q-digest mergeable quantile summary for integer keys over a fixed universe.
 *
 * <p>Reference: N. Shrivastava, C. Buragohain, D. Agrawal, S. Suri,
 * "Medians and Other Aggregates over Data Streams" (SIGMOD 2004).
 *
 * <p>The summary is a set of <em>weighted nodes</em> in a binary tree over
 * the value universe; it never stores raw samples. Node ids follow heap
 * indexing (root 1, children 2i/2i+1) over {@code S} leaves where
 * {@code S} is the smallest power of two &ge; the user-facing universe
 * {@code u}. Leaf {@code S + x} carries observations equal to {@code x};
 * leaves {@code S+u .. 2S-1} are padding and always empty.
 *
 * <h2>Compression invariant</h2>
 * With {@code K = log2 S} and threshold {@code T = floor(eps*n/K)}, every
 * non-root node v satisfies: {@code count(v) = 0} or
 * {@code count(v) + count(left) + count(right) > T}. Whenever the condition
 * fails, all weight at v is promoted to its parent (one bottom-up pass per
 * insert/merge).
 *
 * <h2>Rank-error guarantee</h2>
 * For every value x the summary returns an interval [L(x), U(x)] that is
 * guaranteed to contain the true cumulative rank {@code F(x) = #{i : xi <= x}}:
 * <ul>
 *   <li>L(x) = weight sitting in tree nodes whose range is entirely &le; x,
 *   <li>L(x) + path mass &ge; F(x), where path mass is the weight on the
 *       root&rarr;leaf(x) path.
 * </ul>
 * The classic q-digest path lemma bounds the path mass by {@code K*(T+1)}:
 * <pre>  U(x) - L(x) &le; K*(floor(eps*n/K) + 1) &le; eps*n + K.</pre>
 * So the midpoint answer has additive rank error at most {@code (eps*n+K)/2}
 * and — the usual q-digest statement, exact when {@code K | eps*n} —
 * additive error {@code eps*n}. A quantile q is answered (by binary-search
 * inversion of L/U) with a value v whose true rank is within the same bound
 * of {@code q*n}.
 *
 * <h2>Space</h2>
 * Families {v, child1, child2} of active nodes on one level are disjoint and
 * each contains &gt; T &ge; eps*n/K - 1 weight, so at most
 * {@code K*n/(T+1) &le; K*K/eps + K} nodes are stored overall. This is a
 * fixed bound independent of n (no raw retention).
 *
 * <h2>Merge</h2>
 * Merging adds node weights pointwise and runs one compression pass; the
 * result is exactly the summary of the union of the two streams. Merges are
 * refused unless eps and the universe size match exactly.
 */
public final class QDigest {

    private final double eps;
    private final int universe; // user-facing keys: 0 .. universe-1
    private final int size;     // leaves of the tree, power of two
    private final int k;        // tree height, log2(size)
    private long n;
    private final Map<Integer, Long> nodes = new HashMap<>();

    // lazy cache of subtree mass, invalidated by every mutation
    private transient Map<Integer, Long> subtotal;
    // true when leaf weight has been added since the last compression;
    // compression is deferred until a read/merge/serialize, which is exact:
    // a bottom-up compression sweep distributes accumulated weight the same
    // way regardless of how many insertions preceded it.
    private transient boolean dirty;

    public QDigest(double eps, int universe) {
        if (!(eps > 0.0 && eps < 1.0)) {
            throw new IllegalArgumentException("eps must be in (0,1), got " + eps);
        }
        if (universe <= 0) {
            throw new IllegalArgumentException("universe must be positive, got " + universe);
        }
        this.eps = eps;
        this.universe = universe;
        int s = 1;
        int kk = 0;
        while (s < universe) {
            s <<= 1;
            kk++;
        }
        this.size = Math.max(1, s);
        this.k = Math.max(1, kk);
    }

    private QDigest(double eps, int universe, int size, int k, long n,
                    Map<Integer, Long> nodes) {
        this.eps = eps;
        this.universe = universe;
        this.size = size;
        this.k = k;
        this.n = n;
        this.nodes.putAll(nodes);
    }

    public double eps() {
        return eps;
    }

    public int universe() {
        return universe;
    }

    public long count() {
        return n;
    }

    /** Number of weighted tree nodes actually stored (memory proxy). */
    public int nodeCount() {
        ensureCompressed();
        return nodes.size();
    }

    /**
     * Conservative worst-case bound on stored nodes for the current length.
     * Per level, active-node families each hold &ge; T+1 weight and any two
     * such families intersect at most pairwise, so at most
     * {@code 2*ceil(n/(T+1))} nodes are active per level (plus the root);
     * capped by the physical tree size.
     */
    public long nodeCountBound() {
        long t1 = (long) Math.floor(eps * n / k) + 1;
        long perLevel = 2 * ((n + t1 - 1) / t1) + 1;
        return Math.min(2L * size, (long) k * perLevel + 1);
    }

    public void insert(long key) {
        if (key < 0 || key >= universe) {
            throw new IllegalArgumentException(
                    "key " + key + " outside universe [0," + universe + ")");
        }
        add(size + (int) key, 1L);
        n++;
        dirty = true;
    }

    public void insertAll(long[] keys) {
        for (long key : keys) {
            if (key < 0 || key >= universe) {
                throw new IllegalArgumentException(
                        "key " + key + " outside universe [0," + universe + ")");
            }
            add(size + (int) key, 1L);
        }
        n += keys.length;
        dirty = true;
    }

    /** Compress now if weight has accumulated since the last sweep. */
    private void ensureCompressed() {
        if (dirty) {
            compress();
            dirty = false;
            subtotal = null;
        }
    }
    private void add(int id, long delta) {
        long v = nodes.getOrDefault(id, 0L) + delta;
        if (v == 0) {
            nodes.remove(id);
        } else {
            nodes.put(id, v);
        }
        // subtotal is only consumed after ensureCompressed(); compression
        // itself mutates nodes directly, so invalidate lazily there instead
        // of on every weight change (otherwise read loops become O(n^2)).
    }

    private long threshold() {
        return (long) Math.floor(eps * n / k);
    }

    /**
     * Bottom-up restoration of the compression invariant, one sweep per
     * tree level (leaves first). A node whose family sum is &le; T pushes
     * all of its own weight to its parent; each node is visited once per
     * pass, and weight promoted one level is reconsidered at the next level.
     */
    void compress() {
        long t = threshold();
        if (t <= 0) {
            return;
        }
        for (int level = 0; level < k; level++) {
            // Nodes at level l have ids in [size >> l, (2*size) >> l).
            int lo = size >> level;
            int hi = (2 * size) >> level;
            List<Integer> snapshot = new ArrayList<>();
            for (Integer id : nodes.keySet()) {
                if (id >= lo && id < hi) {
                    snapshot.add(id);
                }
            }
            for (int id : snapshot) {
                if (id == 1) {
                    continue; // root is never promoted
                }
                long family = nodes.getOrDefault(id, 0L)
                        + nodes.getOrDefault(2 * id, 0L)
                        + nodes.getOrDefault(2 * id + 1, 0L);
                if (family <= t) {
                    long w = nodes.getOrDefault(id, 0L);
                    if (w != 0) {
                        add(id, -w);
                        add(id / 2, w);
                    }
                }
            }
        }
    }

    /** Refuse merge unless the other summary shares eps and universe. */
    public void requireCompatible(QDigest other) {
        if (other == null) {
            throw new IllegalArgumentException("cannot merge a null summary");
        }
        if (Double.compare(eps, other.eps) != 0) {
            throw new IllegalArgumentException(String.format(
                    "incompatible summaries: eps %.6g != %.6g", eps, other.eps));
        }
        if (universe != other.universe) {
            throw new IllegalArgumentException(String.format(
                    "incompatible summaries: universe %d != %d",
                    universe, other.universe));
        }
    }

    public boolean isCompatibleWith(QDigest other) {
        try {
            requireCompatible(other);
            return true;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    /** Merge {@code other} into this summary; parameters must match. */
    public void merge(QDigest other) {
        requireCompatible(other);
        // Snapshot under the other summary's own lock so a concurrent insert
        // cannot mutate the node map while we read it. Callers in the server
        // synchronize on each digest separately (no nested locking), which
        // also rules out lock-ordering deadlocks.
        Map<Integer, Long> snapshot;
        long otherN;
        synchronized (other) {
            other.ensureCompressed();
            snapshot = new HashMap<>(other.nodes);
            otherN = other.n;
        }
        mergeSnapshot(snapshot, otherN);
    }

    /** Merge a node-weight snapshot taken by {@link #snapshot()}. */
    public void mergeSnapshot(Map<Integer, Long> snapshot, long otherN) {
        for (Map.Entry<Integer, Long> e : snapshot.entrySet()) {
            add(e.getKey(), e.getValue());
        }
        n += otherN;
        dirty = true;
        ensureCompressed();
    }

    /**
     * Compress and return a defensive copy of the node map together with the
     * observation count, safe to merge later while new inserts arrive.
     */
    public synchronized Snapshot snapshot() {
        ensureCompressed();
        return new Snapshot(eps, universe, n, new HashMap<>(nodes));
    }

    /** Immutable point-in-time copy of a summary's weights. */
    public static final class Snapshot {
        private final double eps;
        private final int universe;
        private final long n;
        private final Map<Integer, Long> nodes;

        Snapshot(double eps, int universe, long n, Map<Integer, Long> nodes) {
            this.eps = eps;
            this.universe = universe;
            this.n = n;
            this.nodes = nodes;
        }

        public double eps() {
            return eps;
        }

        public int universe() {
            return universe;
        }

        public long count() {
            return n;
        }

        public Map<Integer, Long> nodes() {
            return nodes;
        }
    }

    private Map<Integer, Long> subtotals() {
        Map<Integer, Long> cache = subtotal;
        if (cache != null) {
            return cache;
        }
        cache = new HashMap<>();
        fillSubtotal(1, cache);
        subtotal = cache;
        return cache;
    }

    private long fillSubtotal(int id, Map<Integer, Long> cache) {
        long w = nodes.getOrDefault(id, 0L);
        if (id < size) {
            w += fillSubtotal(2 * id, cache) + fillSubtotal(2 * id + 1, cache);
        }
        if (w != 0) {
            cache.put(id, w);
        }
        return w;
    }

    /**
     * Guaranteed interval containing F(x) = number of observations &le; x.
     *
     * @return {@code [lower, upper]}, upper - lower is the path mass
     */
    public long[] rankBounds(long x) {
        ensureCompressed();
        if (x < 0) {
            return new long[]{0L, 0L};
        }
        if (x >= universe) {
            return new long[]{n, n};
        }
        Map<Integer, Long> sub = subtotals();
        long lower = 0;
        long path = 0;
        int id = 1;
        long lo = 0, hi = size; // half-open range of id
        while (id < size) {
            long mid = (lo + hi) >>> 1;
            path += nodes.getOrDefault(id, 0L);
            if (x < mid) {
                id = 2 * id;
                hi = mid;
            } else {
                // whole left subtree is <= mid-1 <= x
                lower += sub.getOrDefault(2 * id, 0L);
                id = 2 * id + 1;
                lo = mid;
            }
        }
        // leaf: its own weight is exactly at x -> lower, not path uncertainty
        lower += nodes.getOrDefault(id, 0L);
        return new long[]{lower, lower + path};
    }

    /** Midpoint estimate of F(x) = number of observations &le; x. */
    public double estimatedRank(long x) {
        long[] b = rankBounds(x);
        return 0.5 * (b[0] + b[1]);
    }

    /**
     * Answer an eps-approximate quantile: a value v whose true rank is
     * within eps*n+K of {@code q*n}. Implemented as the smallest x whose
     * guaranteed upper rank reaches the target rank.
     */
    public long quantile(double q) {
        if (!(q >= 0.0 && q <= 1.0)) {
            throw new IllegalArgumentException("q must be in [0,1], got " + q);
        }
        if (n == 0) {
            throw new IllegalStateException("cannot query an empty summary");
        }
        long target = (q == 0.0) ? 1L : (long) Math.ceil(q * n);
        int lo = 0, hi = universe - 1;
        // smallest x with guaranteed-lower rank L(x) >= target; then the true
        // rank F(x) >= target and F(x-1) <= U(x-1) < target + gap, i.e. the
        // answer's rank overshoots q*n by at most the path-mass gap (and a
        // pile of duplicates at one value does not break this convention).
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (rankBounds(mid)[0] >= target) {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        return lo;
    }

    // ---- serialization (versioned big-endian binary) -------------------

    static final int FORMAT_VERSION = 1;

    public byte[] toByteArray() {
        ensureCompressed();
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        try (DataOutputStream out = new DataOutputStream(bos)) {
            writeTo(out);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
        return bos.toByteArray();
    }

    public void writeTo(DataOutputStream out) throws IOException {
        out.writeInt(FORMAT_VERSION);
        out.writeDouble(eps);
        out.writeInt(universe);
        out.writeLong(n);
        out.writeInt(nodes.size());
        List<Map.Entry<Integer, Long>> entries = new ArrayList<>(nodes.entrySet());
        entries.sort(Map.Entry.comparingByKey());
        for (Map.Entry<Integer, Long> e : entries) {
            out.writeInt(e.getKey());
            out.writeLong(e.getValue());
        }
    }

    public static QDigest fromByteArray(byte[] data) throws IOException {
        try (DataInputStream in =
                     new DataInputStream(new java.io.ByteArrayInputStream(data))) {
            return readFrom(in);
        }
    }

    public static QDigest readFrom(DataInputStream in) throws IOException {
        int version = in.readInt();
        if (version != FORMAT_VERSION) {
            throw new IOException("unsupported format version: " + version);
        }
        double eps = in.readDouble();
        int universe = in.readInt();
        long n = in.readLong();
        int numNodes = in.readInt();
        if (!(eps > 0 && eps < 1) || universe <= 0 || n < 0 || numNodes < 0) {
            throw new IOException("invalid summary header");
        }
        int s = 1;
        int kk = 0;
        while (s < universe) {
            s <<= 1;
            kk++;
        }
        s = Math.max(1, s);
        kk = Math.max(1, kk);
        if (numNodes > 2 * s) {
            throw new IOException("node count exceeds tree size");
        }
        Map<Integer, Long> map = new HashMap<>();
        long sum = 0;
        for (int i = 0; i < numNodes; i++) {
            int id = in.readInt();
            long w = in.readLong();
            if (id < 1 || id >= 2 * s || w <= 0) {
                throw new IOException("invalid node id=" + id + " w=" + w);
            }
            if (map.put(id, w) != null) {
                throw new IOException("duplicate node id " + id);
            }
            sum += w;
        }
        if (sum != n) {
            throw new IOException("stored node weights " + sum
                    + " do not add up to n=" + n);
        }
        return new QDigest(eps, universe, s, kk, n, map);
    }
}
