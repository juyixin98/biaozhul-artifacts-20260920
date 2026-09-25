package approxheavy.cms;

import approxheavy.hash.Hashing;

import java.util.Arrays;

/**
 * Count-Min Sketch (Cormode & Muthukrishnan 2005).
 *
 * <p>Point queries are an <em>upper</em> bound on the true count:
 * {@code estimate(x) >= trueCount(x)} always. The declared one-sided error is
 * {@code epsilon * totalCount} with probability {@code 1 - delta} for
 * {@code width = ceil(e/epsilon)}, {@code depth = ceil(ln(1/delta))}.
 *
 * <p>Sketches are mergeable only when their geometry and hash seed agree;
 * {@link #mergeWith} throws {@link IncompatibleSketchException} otherwise.
 */
public final class CountMinSketch {
    private final int width;
    private final int depth;
    private final long seed;
    final long[][] cells;
    long totalCount;

    public CountMinSketch(int width, int depth, long seed) {
        if (width < 2) {
            throw new IllegalArgumentException("width must be >= 2");
        }
        if (depth < 1) {
            throw new IllegalArgumentException("depth must be >= 1");
        }
        this.width = width;
        this.depth = depth;
        this.seed = seed;
        this.cells = new long[depth][width];
        this.totalCount = 0L;
    }

    /** Sketch sized for error {@code epsilon} with failure probability {@code delta}. */
    public static CountMinSketch withError(double epsilon, double delta, long seed) {
        if (!(epsilon > 0 && epsilon < 1)) {
            throw new IllegalArgumentException("epsilon must be in (0, 1)");
        }
        if (!(delta > 0 && delta < 1)) {
            throw new IllegalArgumentException("delta must be in (0, 1)");
        }
        int w = (int) Math.ceil(Math.E / epsilon);
        int d = (int) Math.ceil(Math.log(1.0 / delta));
        return new CountMinSketch(Math.max(w, 2), Math.max(d, 1), seed);
    }

    public void add(String key) {
        add(key, 1L);
    }

    public void add(String key, long count) {
        if (count < 0) {
            throw new IllegalArgumentException("count must be non-negative");
        }
        for (int row = 0; row < depth; row++) {
            int bucket = Hashing.bucket(Hashing.rowHash(key, seed, row), width);
            cells[row][bucket] += count;
        }
        totalCount += count;
    }

    /** Estimated count: always {@code >=} the true count, never lower. */
    public long estimate(String key) {
        long min = Long.MAX_VALUE;
        for (int row = 0; row < depth; row++) {
            int bucket = Hashing.bucket(Hashing.rowHash(key, seed, row), width);
            min = Math.min(min, cells[row][bucket]);
        }
        return min;
    }

    /**
     * Pointwise add of another compatible sketch. Rejects different width,
     * depth or seed — a different seed hashes keys into unrelated buckets and
     * a different width changes the bucket mapping, so either merge would be
     * silently meaningless.
     */
    public void mergeWith(CountMinSketch other) {
        requireCompatible(other);
        for (int row = 0; row < depth; row++) {
            for (int col = 0; col < width; col++) {
                cells[row][col] += other.cells[row][col];
            }
        }
        totalCount += other.totalCount;
    }

    public void requireCompatible(CountMinSketch other) {
        if (other.width != this.width) {
            throw new IncompatibleSketchException(
                    "sketch width mismatch: " + other.width + " != " + this.width);
        }
        if (other.depth != this.depth) {
            throw new IncompatibleSketchException(
                    "sketch depth mismatch: " + other.depth + " != " + this.depth);
        }
        if (other.seed != this.seed) {
            throw new IncompatibleSketchException(
                    "sketch seed mismatch: " + other.seed + " != " + this.seed
                            + " (different hash families cannot be merged)");
        }
    }

    public boolean isCompatibleWith(CountMinSketch other) {
        return other.width == width && other.depth == depth && other.seed == seed;
    }

    public int width() {
        return width;
    }

    public int depth() {
        return depth;
    }

    public long seed() {
        return seed;
    }

    public long totalCount() {
        return totalCount;
    }

    /** Declared additive upper bound on overestimate for any single key. */
    public long errorUpperBound() {
        return Math.round(Math.ceil((2.0 / (width - 1)) * (double) totalCount));
    }

    /** Failure probability associated with the depth. */
    public double delta() {
        return 1.0 / Math.pow(2.0, depth);
    }

    /** Compact JSON used by the service; no whitespace, rows emitted as long arrays. */
    public String toJson() {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"type\":\"CountMinSketch\",\"width\":").append(width)
                .append(",\"depth\":").append(depth)
                .append(",\"seed\":").append(seed)
                .append(",\"totalCount\":").append(totalCount)
                .append(",\"cells\":[");
        for (int row = 0; row < depth; row++) {
            if (row > 0) {
                sb.append(',');
            }
            sb.append('[');
            for (int col = 0; col < width; col++) {
                if (col > 0) {
                    sb.append(',');
                }
                sb.append(cells[row][col]);
            }
            sb.append(']');
        }
        sb.append("]}");
        return sb.toString();
    }

    /**
     * Rebuilds a sketch from external JSON (e.g. an /merge request).
     *
     * @throws IllegalArgumentException if the JSON is malformed or inconsistent
     */
    public static CountMinSketch fromJson(String json) {
        return SketchJson.parseSketch(json);
    }

    long[][] cellsCopy() {
        long[][] copy = new long[depth][];
        for (int row = 0; row < depth; row++) {
            copy[row] = Arrays.copyOf(cells[row], width);
        }
        return copy;
    }
}
