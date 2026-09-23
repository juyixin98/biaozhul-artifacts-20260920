package qsummary;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Greenwald-Khanna (2001) epsilon-approximate quantile summary.
 *
 * <p>Rank convention: the rank of a value v is the number of observed values
 * that are {@code <= v} (1-based, values in [1, n]). For a requested quantile
 * phi in [0,1] the target rank is {@code ceil(phi * n)}.
 *
 * <p>Guarantee (the central contract of this class): after every update and
 * compress, the summary satisfies the GK per-tuple invariant
 * <pre>
 *     g_i + delta_i &lt;= floor(2 * epsilon * n)
 * </pre>
 * for every stored tuple. Proposition 1 of the paper then guarantees that for
 * every phi in [0,1] some stored tuple v has both r-rmin(v) &le; epsilon*n and
 * rmax(v)-r &le; epsilon*n, i.e. an additive rank error of at most
 * epsilon*n. Equivalently, the returned value lies between the true order
 * statistics at ranks (phi-epsilon)n and (phi+epsilon)n.
 *
 * <p>Heavy ties: when many observations share one value, rankLE(v) can sit
 * anywhere in a wide tie block; the sketch never stores raw multiplicities, so
 * the value-space guarantee above (the answer is a genuine order statistic
 * spanning the target rank) is the right contract, and is what the tests check.
 * On continuous / distinct-valued data this coincides with the rankLE distance
 * bound. {@link #rankOf(double)} provides a CDF estimate (rankLE of a value),
 * which is within epsilon*n on distinct-valued data.
 *
 * <p>The stored structure is a sorted list of tuples (value, g, delta), each
 * bracketing a rank interval [rmin, rmin+delta]; g is the increment in rmin
 * over the previous tuple. The minimum and maximum are always retained as
 * exact (delta 0) sentinels.
 *
 * <p>Merging: two summaries built with the SAME epsilon are merged by the
 * weighted-tuple construction. The tuples of both inputs are interleaved in
 * value order; a tuple (v,g,d) from one input is carried into the union with
 * its weight g and uncertainty increased by the other input's rank slack
 * 2*epsilon*n_other (the global min/max stay exact). Every pre-compression
 * tuple then satisfies g+delta &le; 2*epsilon*(n1+n2), and a compress at the
 * combined length restores the invariant, preserving the epsilon*N quantile
 * guarantee on the union. Chained merges (8-shard left folds and balanced
 * trees) and snapshot round-trips are covered by the tests.
 *
 * <p>Space is O((1/epsilon) log(epsilon*n)) tuples, independent of the number
 * of observations: this class never retains the raw samples.
 */
public final class GKQuantileSummary {

    static final String ALGORITHM = "gk";
    static final int FORMAT_VERSION = 1;
    /** Only the natural ordering on finite doubles is supported. */
    static final String ORDER = "double-natural";

    private final double epsilon;
    private final List<Tuple> tuples = new ArrayList<>();
    private long n = 0;

    public GKQuantileSummary(double epsilon) {
        if (!(epsilon > 0d) || !(epsilon < 1d)) {
            throw new BadRequestException("epsilon must be in the open interval (0,1), got " + epsilon);
        }
        this.epsilon = epsilon;
    }

    public double epsilon() {
        return epsilon;
    }

    public long count() {
        return n;
    }

    public int storedTuples() {
        return tuples.size();
    }

    // ------------------------------------------------------------------
    // Insert
    // ------------------------------------------------------------------

    /** Insert one observed value. Non-finite values are rejected. */
    public synchronized void insert(double value) {
        if (!Double.isFinite(value)) {
            throw new BadRequestException("only finite values are allowed, got " + value);
        }
        // Paper Figure 3: COMPRESS runs BEFORE the insert, every 1/(2*epsilon)
        // observations, using the capacity of the current n.
        long period = period();
        if (period > 0 && n % period == 0 && tuples.size() > 1) {
            compress();
        }
        n++;

        if (tuples.isEmpty()) {
            // First observation: exact minimum sentinel, delta = 0.
            tuples.add(new Tuple(value, 1, 0));
            return;
        }

        // Paper INSERT(v): find smallest i with v_{i-1} <= v < v_i and insert
        // between them. With strict inequality a duplicate of a stored value
        // is placed BEFORE the first tuple holding that value (lowerBound is
        // the first tuple strictly greater than v). New min/max are exact
        // (g,delta)=(1,0) sentinels.
        int pos = upperBound(value); // first stored value > v

        if (pos == 0) {
            // New minimum: exact sentinel.
            tuples.add(0, new Tuple(value, 1, 0));
        } else if (pos == tuples.size()) {
            // New maximum: exact sentinel.
            tuples.add(new Tuple(value, 1, 0));
        } else {
            // Interior insertion: delta = floor(2*epsilon*n), evaluated on
            // the stream length AFTER inserting (this n).
            int delta = (int) Math.floor(2d * epsilon * n);
            tuples.add(pos, new Tuple(value, 1, delta));
        }
    }

    private long period() {
        // Paper Figure 3: compress when n ≡ 0 (mod floor(1/(2*epsilon))).
        return Math.max(1L, (long) Math.floor(1d / (2d * epsilon)));
    }

    /** First index i with tuples.get(i).value > value (tuples sorted ascending). */
    private int upperBound(double value) {
        int lo = 0, hi = tuples.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (tuples.get(mid).value <= value) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }
    /** First index i with tuples.get(i).value >= value (tuples sorted ascending). */
    private int lowerBound(double value) {
        int lo = 0, hi = tuples.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (tuples.get(mid).value < value) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    // ------------------------------------------------------------------
    // Compress (GK Invariant 1)
    // ------------------------------------------------------------------

    /**
     * One GK compression sweep (paper Figure 2), scanned from the maximum
     * downwards. Tuple i is removed (its g folded into tuple i+1) when
     * <pre>
     *     band(delta_i) <= band(delta_{i+1})
     *     and g_i + g_{i+1} + delta_{i+1} &lt; 2*epsilon*n .
     * </pre>
     *
     * The loop keeps index 0 (the minimum) as well as the maximum: section
     * 2.1 promises the extrema are always part of the summary, and retaining
     * the exact minimum is required for quantile queries at small ranks
     * (the paper's Figure 2 loop runs i down to 0 but Corollary 1's bound at
     * r=1 implicitly depends on the minimum tuple surviving).
     */
    synchronized void compress() {
        if (tuples.size() <= 2 || n == 0) {
            return;
        }
        double capacity = 2d * epsilon * n;

        for (int i = tuples.size() - 2; i >= 1; i--) {
            Tuple cur = tuples.get(i);
            Tuple next = tuples.get(i + 1);
            if (band(cur.delta) <= band(next.delta)
                    && (double) cur.g + next.g + next.delta < capacity) {
                next.g += cur.g;
                tuples.remove(i);
            }
        }
    }

    /**
     * GK band function. band 0 holds delta 0 (sentinels and the first
     * 1/(2*epsilon) observations); for delta &gt; 0, band(delta) =
     * ceil(log2(delta)). This is the standard practical definition used by
     * production implementations (equivalent in the invariant it enforces).
     */
    static int band(int delta) {
        if (delta <= 0) {
            return 0;
        }
        // ceil(log2(delta)): 32 - numberOfLeadingZeros(delta - 1).
        return 32 - Integer.numberOfLeadingZeros(delta - 1);
    }

    // ------------------------------------------------------------------
    // Queries
    // ------------------------------------------------------------------

    /**
     * Estimate the phi-quantile (phi in [0,1]).
     *
     * @throws IllegalStateException if the summary is empty
     */
    public synchronized double quantile(double phi) {
        if (!(phi >= 0d && phi <= 1d)) {
            throw new BadRequestException("q must be in [0,1], got " + phi);
        }
        if (n == 0) {
            throw new IllegalStateException("cannot query an empty summary");
        }
        // Endpoints are exact: the minimum/maximum tuples are retained with
        // delta 0.
        if (phi == 0d) {
            return tuples.get(0).value;
        }
        if (phi == 1d) {
            return tuples.get(tuples.size() - 1).value;
        }
        // Paper QUANTILE(phi): target rank r = max(1, ceil(phi*n)); find
        // tuple i with r - rmin(v_i) <= epsilon*n AND rmax(v_i) - r <=
        // epsilon*n and return v_i. Corollary 1 guarantees one exists because
        // every tuple satisfies g+delta <= 2*epsilon*n. Returning the first
        // qualifying tuple (smallest rmin) matches the paper's search.
        long target = Math.max(1L, (long) Math.ceil(phi * n));
        double allowance = epsilon * n;
        // Among all tuples whose rank interval covers target within the
        // allowance, return the one whose interval is closest to target.
        // Corollary 1 guarantees at least one qualifier; the nearest choice
        // is tight at both ends (exact min for phi->0, exact max for phi->1).
        int chosen = -1;
        long bestDist = Long.MAX_VALUE;
        long rmin = 0;
        for (int k = 0; k < tuples.size(); k++) {
            Tuple t = tuples.get(k);
            rmin += t.g;
            long rmax = rmin + t.delta;
            if (target - rmin <= allowance && rmax - target <= allowance) {
                long dist;
                if (target < rmin) {
                    dist = rmin - target;
                } else if (target > rmax) {
                    dist = target - rmax;
                } else {
                    dist = 0; // interval covers target
                }
                if (dist < bestDist) {
                    bestDist = dist;
                    chosen = k;
                }
            }
        }
        if (chosen >= 0) {
            return tuples.get(chosen).value;
        }
        // Defensive fallback: tuple whose interval is closest to r.
        long bestDistance = Long.MAX_VALUE;
        int best = tuples.size() - 1;
        rmin = 0;
        for (int i = 0; i < tuples.size(); i++) {
            Tuple t = tuples.get(i);
            rmin += t.g;
            long rmax = rmin + t.delta;
            long d = (target < rmin) ? rmin - target
                    : (target > rmax ? target - rmax : 0);
            if (d < bestDistance) {
                bestDistance = d;
                best = i;
            }
        }
        return tuples.get(best).value;
    }

    /**
     * Estimate the rank (number of observations {@code <= value}) of value.
     * Returns 0 for values below the observed minimum and n above the maximum.
     * The estimate is within epsilon*n of the true rank.
     */
    public synchronized long rankOf(double value) {
        if (!Double.isFinite(value)) {
            throw new BadRequestException("only finite values are allowed, got " + value);
        }
        if (n == 0) {
            throw new IllegalStateException("cannot query an empty summary");
        }
        int idx = lowerBound(value); // first stored value >= value

        if (idx >= tuples.size()) {
            // Above the stored maximum (always retained).
            return n;
        }
        long rminIdx = 0;
        for (int i = 0; i <= idx; i++) {
            rminIdx += tuples.get(i).g;
        }
        Tuple t = tuples.get(idx);

        if (idx == 0 && t.value > value) {
            return 0;
        }

        // Rank estimate from the tuple's OWN certified rank interval. A
        // stored value v_i brackets its rank as [rmin_i, rmax_i] with
        // rmax_i - rmin_i = delta_i <= 2*epsilon*n; the midpoint has error at
        // most epsilon*n. With several surviving tuples sharing a value the
        // interval is extended across the whole equal-value group. For very
        // heavy ties the multiplicity interval can be wide — the quantile
        // value guarantee is unaffected (documented in the README).
        if (t.value == value) {
            // idx is the first tuple equal to value; its rmin is rminIdx.
            long groupEndRmax = rminIdx + t.delta;
            long running = rminIdx;
            int e = idx + 1;
            while (e < tuples.size() && tuples.get(e).value == value) {
                running += tuples.get(e).g;
                groupEndRmax = Math.max(groupEndRmax, running + tuples.get(e).delta);
                e++;
            }
            if (idx == 0) {
                // Observed minimum: nothing is strictly smaller, so the
                // group-end rmax is the exact rank of the last occurrence.
                return groupEndRmax;
            }
            return (rminIdx + groupEndRmax) / 2;
        }
        // Non-stored value v in the gap before tuple idx. The GK rank
        // estimate is the predecessor's rmax: the proposition guarantees
        // rmax(prev) <= rank(v) and rmin(idx)-rmax(prev) <= 2*epsilon*n,
        // so the additive error is bounded by 2*epsilon*n. This is the
        // standard rank-query bound for GK (the tighter epsilon*n bound is
        // specific to the QUANTILE procedure on target ranks, see README).
        return (idx == 0) ? 0 : rminIdx - t.g + tuples.get(idx - 1).delta;
    }

    // ------------------------------------------------------------------
    // Merge (weighted-tuple construction)
    // ------------------------------------------------------------------

    /**
     * Merge this summary with {@code other} and return a NEW summary
     * representing the union of both streams. Neither input is modified.
     *
     * <p>Requires identical epsilon, algorithm format and ordering; otherwise
     * an {@link IncompatibleSummaryException} is thrown. Empty summaries
     * merge freely (union with an empty stream).
     *
     * <p>Weighted-tuple construction: the two tuple sequences are interleaved
     * in value order. A tuple (v,g,d) contributed by stream X is carried into
     * the union with its weight g and its rank uncertainty raised by the other
     * stream's rank slack {@code 2*epsilon*n_Y}, which bounds how many of Y's
     * observations can rank below v. Each pre-compression tuple therefore
     * satisfies {@code g+delta <= 2*epsilon*(n_X+n_Y)}; the global min/max
     * stay exact. A compress() at the combined length restores the GK
     * invariant and preserves the epsilon*N quantile guarantee.
     */
    public static GKQuantileSummary merge(GKQuantileSummary a, GKQuantileSummary b) {
        checkCompatible(a, b);
        GKQuantileSummary out = new GKQuantileSummary(a.epsilon);
        if (a.n == 0) {
            for (Tuple t : b.tuples) out.tuples.add(new Tuple(t.value, t.g, t.delta));
            out.n = b.n;
            out.validateInternals();
            return out;
        }
        if (b.n == 0) {
            for (Tuple t : a.tuples) out.tuples.add(new Tuple(t.value, t.g, t.delta));
            out.n = a.n;
            out.validateInternals();
            return out;
        }

        // Weighted-tuple merge (GK, section 4). Each tuple of A is carried
        // over unchanged; each tuple (v,g,d) of B is inserted into the value
        // order as a WEIGHTED tuple (v, g, d + 2*epsilon*n_A). The added
        // slack absorbs the uncertainty in how many A observations rank
        // below v. Every pre-compression tuple therefore satisfies
        //   g + delta <= 2*epsilon*n_A + 2*epsilon*n_B = 2*epsilon*N,
        // so compress() with capacity 2*epsilon*N preserves Invariant 1 and
        // the merged summary answers quantiles to within epsilon*N of the
        // union. The new global minimum and maximum are exact (delta 0).
        List<Tuple> A = a.tuples;
        List<Tuple> B = b.tuples;
        // Each tuple gains the rank slack of the OTHER stream: its rank in
        // the union is its within-stream rank plus an uncertain cross-stream
        // count bounded by that stream's error allowance 2*epsilon*n_X.
        int slackA = (int) Math.floor(2d * a.epsilon * a.n);
        int slackB = (int) Math.floor(2d * a.epsilon * b.n);

        List<Tuple> seq = new ArrayList<>(A.size() + B.size());
        int i = 0, j = 0;
        while (i < A.size() || j < B.size()) {
            boolean takeA;
            if (i >= A.size()) {
                takeA = false;
            } else if (j >= B.size()) {
                takeA = true;
            } else {
                takeA = A.get(i).value <= B.get(j).value;
            }
            if (takeA) {
                Tuple t = A.get(i++);
                seq.add(new Tuple(t.value, t.g, t.delta + slackB));
            } else {
                Tuple t = B.get(j++);
                seq.add(new Tuple(t.value, t.g, t.delta + slackA));
            }
        }

        // Normalise: no rank interval may extend past N, and the first/last
        // tuples are exact global min/max sentinels (delta 0).
        long cum = 0;
        for (int k = 0; k < seq.size(); k++) {
            Tuple t = seq.get(k);
            cum += t.g;
            int maxDelta = (int) Math.max(0, (a.n + b.n) - cum);
            if (t.delta > maxDelta) {
                t.delta = maxDelta;
            }
        }
        if (!seq.isEmpty()) {
            seq.get(0).delta = 0;
            seq.get(seq.size() - 1).delta = 0;
        }

        out.tuples.addAll(seq);
        out.n = a.n + b.n;
        out.compress();
        // Re-pin the extrema after compression (compress preserves them but
        // the last tuple keeps delta 0 by construction).
        out.validateInternals();
        return out;
    }

    private static void checkCompatible(GKQuantileSummary a, GKQuantileSummary b) {
        if (Math.abs(a.epsilon - b.epsilon) > 1e-15) {
            throw new IncompatibleSummaryException(String.format(
                    "epsilon mismatch: %.6g vs %.6g", a.epsilon, b.epsilon));
        }
        // ORDER and FORMAT are fixed for this implementation; the check exists
        // so that serialized summaries produced by a future version are
        // rejected rather than mis-merged.
    }

    /**
     * Structural self-check used in tests: g positive, g sum == n,
     * deltas non-negative, sorted values.
     */
    synchronized void validateInternals() {
        if (n == 0) {
            if (!tuples.isEmpty()) {
                throw new IllegalStateException("empty summary must hold no tuples");
            }
            return;
        }
        long sum = 0;
        double prev = -Double.MAX_VALUE;
        for (int i = 0; i < tuples.size(); i++) {
            Tuple t = tuples.get(i);
            if (t.g <= 0) {
                throw new IllegalStateException("non-positive g at index " + i);
            }
            if (t.delta < 0) {
                throw new IllegalStateException("negative delta at index " + i);
            }
            // Non-strict: adjacent tuples with equal values are legal
            // (duplicates inserted per the paper; folded together later).
            if (t.value < prev) {
                throw new IllegalStateException("tuple order violation at index " + i);
            }
            prev = t.value;
            sum += t.g;
        }
        if (sum != n) {
            throw new IllegalStateException("g sum " + sum + " != n " + n);
        }
    }

    /**
     * Worst realised additive rank error across the quantile grid, measured
     * against the exact rank computed by the caller's oracle. Test support.
     */
    synchronized List<Tuple> snapshotTuples() {
        List<Tuple> copy = new ArrayList<>(tuples.size());
        for (Tuple t : tuples) {
            copy.add(new Tuple(t.value, t.g, t.delta));
        }
        return copy;
    }

    synchronized void loadTuples(List<Tuple> stored, long count) {
        tuples.clear();
        tuples.addAll(stored);
        n = count;
        validateInternals();
    }

    // ------------------------------------------------------------------
    // Serialization (self-describing, versioned snapshot)
    // ------------------------------------------------------------------

    /**
     * Serialize to a self-describing snapshot map. The snapshot records the
     * algorithm, format version, ordering, epsilon, observation count and the
     * stored (value,g,delta) tuples — never the raw observations.
     */
    public synchronized Map<String, Object> toSnapshot() {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("algorithm", ALGORITHM);
        root.put("version", FORMAT_VERSION);
        root.put("order", ORDER);
        root.put("epsilon", epsilon);
        root.put("n", n);
        List<Map<String, Object>> entries = new ArrayList<>(tuples.size());
        for (Tuple t : tuples) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("v", t.value);
            e.put("g", t.g);
            e.put("d", t.delta);
            entries.add(e);
        }
        root.put("tuples", entries);
        return root;
    }

    /**
     * Parse a snapshot, validating declared parameters and internal
     * consistency (sorted values, positive g, g sum == n, non-negative d).
     * Throws {@link IncompatibleSummaryException} for unknown algorithm,
     * version or ordering so that foreign/older snapshots are never merged
     * blindly.
     */
    @SuppressWarnings("unchecked")
    public static GKQuantileSummary fromSnapshot(Object raw) {
        Map<String, Object> m = Json.asObject(raw);
        Object algo = m.get("algorithm");
        Object ver = m.get("version");
        Object order = m.get("order");
        if (!ALGORITHM.equals(algo)
                || !(ver instanceof Number n2 && n2.intValue() == FORMAT_VERSION)
                || !ORDER.equals(order)) {
            throw new IncompatibleSummaryException(
                    "unsupported snapshot (algorithm=" + algo + ", version=" + ver
                            + ", order=" + order + ")");
        }
        double eps;
        try {
            eps = Json.getDouble(m, "epsilon");
        } catch (BadRequestException e) {
            throw new BadRequestException("snapshot: " + e.getMessage());
        }
        Object nRaw = m.get("n");
        if (!(nRaw instanceof Number nn)) {
            throw new BadRequestException("snapshot: 'n' must be a number");
        }
        long count = nn.longValue();
        Object tRaw = m.get("tuples");
        if (!(tRaw instanceof List<?> list)) {
            throw new BadRequestException("snapshot: 'tuples' must be an array");
        }
        List<Tuple> stored = new ArrayList<>(list.size());
        double lastV = -Double.MAX_VALUE;
        for (Object item : list) {
            Map<String, Object> e = Json.asObject(item);
            Object vv = e.get("v");
            Object gg = e.get("g");
            Object dd = e.get("d");
            if (!(vv instanceof Number vn) || !(gg instanceof Number gn)
                    || !(dd instanceof Number dn)) {
                throw new BadRequestException("snapshot tuple must have numeric v,g,d");
            }
            double v = vn.doubleValue();
            if (!Double.isFinite(v)) {
                throw new BadRequestException("snapshot tuple v non-finite");
            }
            if (v < lastV) {
                throw new BadRequestException("snapshot tuples not sorted");
            }
            lastV = v;
            int g = gn.intValue();
            int d = dn.intValue();
            if (g <= 0 || d < 0) {
                throw new BadRequestException("snapshot tuple has bad g/d");
            }
            stored.add(new Tuple(v, g, d));
        }
        GKQuantileSummary s = new GKQuantileSummary(eps);
        s.loadTuples(stored, count); // throws if g-sum != n, etc.
        return s;
    }
}
