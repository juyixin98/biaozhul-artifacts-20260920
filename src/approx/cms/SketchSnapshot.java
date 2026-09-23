package approx.cms;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Serialization-friendly snapshot of a {@link CountMinSketch}.
 *
 * <p>The JSON service exchanges sketches in this form. {@code counters} is a
 * list of rows, each row {@code width} longs. Merging snapshots is only valid
 * when {@code width}, {@code depth} and {@code seed} all match.
 */
public final class SketchSnapshot {

    public final int width;
    public final int depth;
    public final long seed;
    public final long totalCount;
    public final long[][] counters;

    public SketchSnapshot(int width, int depth, long seed, long totalCount, long[][] counters) {
        this.width = width;
        this.depth = depth;
        this.seed = seed;
        this.totalCount = totalCount;
        this.counters = counters;
    }

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("width", width);
        m.put("depth", depth);
        m.put("seed", seed);
        m.put("totalCount", totalCount);
        List<List<Long>> rows = new ArrayList<>(depth);
        for (long[] row : counters) {
            List<Long> r = new ArrayList<>(width);
            for (long v : row) {
                r.add(v);
            }
            rows.add(r);
        }
        m.put("counters", rows);
        return m;
    }

    @SuppressWarnings("unchecked")
    public static SketchSnapshot fromMap(Object o) {
        if (!(o instanceof Map)) {
            throw new IllegalArgumentException("sketch must be a JSON object");
        }
        Map<String, Object> m = (Map<String, Object>) o;
        int width = asInt(m.get("width"), "width");
        int depth = asInt(m.get("depth"), "depth");
        long seed = ((Number) require(m, "seed")).longValue();
        long totalCount = ((Number) require(m, "totalCount")).longValue();
        Object rowsObj = require(m, "counters");
        if (!(rowsObj instanceof List)) {
            throw new IllegalArgumentException("sketch.counters must be an array");
        }
        List<?> rows = (List<?>) rowsObj;
        if (rows.size() != depth) {
            throw new IllegalArgumentException("sketch.counters has " + rows.size() + " rows, expected " + depth);
        }
        long[][] counters = new long[depth][width];
        for (int i = 0; i < depth; i++) {
            if (!(rows.get(i) instanceof List)) {
                throw new IllegalArgumentException("sketch.counters[" + i + "] must be an array");
            }
            List<?> row = (List<?>) rows.get(i);
            if (row.size() != width) {
                throw new IllegalArgumentException(
                        "sketch.counters[" + i + "] has length " + row.size() + ", expected " + width);
            }
            for (int j = 0; j < width; j++) {
                Object cell = row.get(j);
                if (!(cell instanceof Number)) {
                    throw new IllegalArgumentException("sketch counters must be integers");
                }
                counters[i][j] = ((Number) cell).longValue();
            }
        }
        return new SketchSnapshot(width, depth, seed, totalCount, counters);
    }

    private static Object require(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) {
            throw new IllegalArgumentException("sketch." + key + " is required");
        }
        return v;
    }

    private static int asInt(Object v, String key) {
        if (!(v instanceof Number)) {
            throw new IllegalArgumentException("sketch." + key + " must be an integer");
        }
        return ((Number) v).intValue();
    }
}
