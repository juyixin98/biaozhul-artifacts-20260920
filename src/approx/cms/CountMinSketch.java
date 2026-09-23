package approx.cms;

/**
 * Count-Min Sketch (Cormode &amp; Muthukrishnan, 2005) over string items.
 *
 * <p>Guarantees:
 * <ul>
 *   <li>{@link #estimate(String)} never under-estimates the true frequency.</li>
 *   <li>For a stream of {@link #totalCount()} updates with sketch parameters
 *       {@code width = ceil(e/epsilon)} and {@code depth = ceil(ln(1/delta))},
 *       for every fixed item,
 *       {@code estimate - true <= epsilon * totalCount} with probability at
 *       least {@code 1 - delta} &mdash; a per-item, one-sided error bound
 *       <em>declared at query time</em> via {@link #errorUpperBound()}.</li>
 * </ul>
 *
 * <p>The sketch is a frequency <em>summary</em>: it says nothing about which
 * items are frequent. Candidate tracking ({@code BoundedCandidateSet}) is a
 * separate structure and its coverage of the true top-K is deliberately not
 * guaranteed (see README &sect;Guarantees).
 *
 * <p>Thread-safe: all updates/queries are synchronized on the sketch instance.
 */
public final class CountMinSketch {

    private final int width;
    private final int depth;
    private final long seed;
    private final HashFamily hashes;
    private final long[][] counters;
    private long totalCount;

    public CountMinSketch(int width, int depth, long seed) {
        if (width <= 0 || depth <= 0) {
            throw new IllegalArgumentException("width and depth must be positive");
        }
        this.width = width;
        this.depth = depth;
        this.seed = seed;
        this.hashes = new HashFamily(depth, seed);
        this.counters = new long[depth][width];
    }

    /**
     * Standard sizing: {@code width = ceil(e/epsilon)} and
     * {@code depth = ceil(ln(1/delta))} give the error/confidence guarantee.
     */
    public static CountMinSketch withErrorTarget(double epsilon, double delta, long seed) {
        if (!(epsilon > 0 && epsilon < 1) || !(delta > 0 && delta < 1)) {
            throw new IllegalArgumentException("require 0 < epsilon < 1 and 0 < delta < 1");
        }
        int width = (int) Math.ceil(Math.E / epsilon);
        int depth = (int) Math.ceil(Math.log(1.0 / delta));
        return new CountMinSketch(width, depth, seed);
    }

    public synchronized int width() {
        return width;
    }

    public synchronized int depth() {
        return depth;
    }

    public synchronized long seed() {
        return seed;
    }

    public synchronized long totalCount() {
        return totalCount;
    }

    /** Deterministic additive error bound {@code epsilon * totalCount} with width = ceil(e/epsilon). */
    public synchronized double epsilon() {
        return Math.E / width;
    }

    /** Confidence parameter: per-item bound holds with probability at least {@code 1 - delta()}. */
    public synchronized double delta() {
        // depth = ceil(ln(1/delta))  =>  delta = exp(-depth) is a valid (conservative) choice.
        return Math.exp(-depth);
    }

    /**
     * Additive error upper bound declared for every point query at the current
     * stream length: {@code estimate(item) - trueCount(item) <= bound} for each
     * fixed item with probability at least {@code 1 - delta()}.
     */
    public synchronized long errorUpperBound() {
        return (long) Math.ceil(epsilon() * totalCount);
    }

    /** Add one occurrence of {@code item}. */
    public void add(String item) {
        add(item, 1L);
    }

    /** Add {@code count} occurrences of {@code item}. */
    public synchronized void add(String item, long count) {
        if (count <= 0) {
            throw new IllegalArgumentException("count must be positive");
        }
        for (int row = 0; row < depth; row++) {
            int col = hashes.bucket(item, row, width);
            counters[row][col] = Math.addExact(counters[row][col], count);
        }
        totalCount = Math.addExact(totalCount, count);
    }

    /** Over-estimate of the frequency of {@code item} (minimum across rows). */
    public synchronized long estimate(String item) {
        long min = Long.MAX_VALUE;
        for (int row = 0; row < depth; row++) {
            int col = hashes.bucket(item, row, width);
            min = Math.min(min, counters[row][col]);
        }
        return min;
    }

    /** Bucket indices per row for {@code item}; exposed for tests and collision experiments. */
    public synchronized int[] bucketIndices(String item) {
        int[] idx = new int[depth];
        for (int row = 0; row < depth; row++) {
            idx[row] = hashes.bucket(item, row, width);
        }
        return idx;
    }

    /**
     * Merge another sketch into this one (pointwise counter addition).
     *
     * @throws SketchIncompatibleException if width, depth or seed differ, or if
     *     the other sketch's hash family cannot be proven identical &mdash;
     *     merging such sketches would corrupt every estimate silently.
     */
    public synchronized void merge(CountMinSketch other) {
        synchronized (other) {
            if (other.width != width || other.depth != depth) {
                throw new SketchIncompatibleException(
                        "sketch layout mismatch: this=" + width + "x" + depth
                                + " other=" + other.width + "x" + other.depth);
            }
            if (other.seed != seed) {
                throw new SketchIncompatibleException(
                        "sketch seed mismatch: this=" + seed + " other=" + other.seed
                                + " (different seeds hash items to different buckets)");
            }
            for (int row = 0; row < depth; row++) {
                for (int col = 0; col < width; col++) {
                    counters[row][col] = Math.addExact(counters[row][col], other.counters[row][col]);
                }
            }
            totalCount = Math.addExact(totalCount, other.totalCount);
        }
    }

    /** Whether {@code other} could be merged into this sketch. */
    public synchronized boolean compatibleWith(CountMinSketch other) {
        return other.width == width && other.depth == depth && other.seed == seed;
    }

    public synchronized SketchSnapshot snapshot() {
        long[][] copy = new long[depth][width];
        for (int row = 0; row < depth; row++) {
            System.arraycopy(counters[row], 0, copy[row], 0, width);
        }
        return new SketchSnapshot(width, depth, seed, totalCount, copy);
    }

    public static CountMinSketch fromSnapshot(SketchSnapshot s) {
        CountMinSketch sketch = new CountMinSketch(s.width, s.depth, s.seed);
        for (int row = 0; row < s.depth; row++) {
            System.arraycopy(s.counters[row], 0, sketch.counters[row], 0, s.width);
        }
        sketch.totalCount = s.totalCount;
        return sketch;
    }

    /**
     * Merge a deserialized snapshot into this sketch; same compatibility rules
     * as {@link #merge(CountMinSketch)}.
     */
    public void mergeSnapshot(SketchSnapshot other) {
        merge(fromSnapshot(other));
    }
}
