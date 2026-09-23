package bitmapindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Offline multi-dimensional bitmap index over an immutable columnar dataset,
 * plus a live/dead deletion mask.
 *
 * <p>Each indexed column stores one bitmap per distinct value: bit i is set in
 * column c's bitmap for value v iff row i has value v in column c. Row ids are
 * the original 0-based positions in the loaded dataset — nothing ever
 * compacts or renumbers rows, so ids returned by queries always correspond to
 * the same records the client loaded (this is asserted by the acceptance
 * tests after deletions).
 *
 * <p>A query is a nested boolean expression over predicates
 * {@code {"op":"eq","column":"city","value":"X"}}:
 * <ul>
 *   <li>{"op":"and","args":[...]} / {"op":"or","args":[...]}</li>
 *   <li>{"op":"not","arg":{...}} — complement inside the <b>currently live</b>
 *       universe only; deleted rows never appear in a NOT result.</li>
 * </ul>
 * Every query result is finally intersected with the live-universe mask,
 * which is the single source of truth for deletion.
 */
public final class BitMapIndex {

    /** column name -> (value string -> bitmap of row positions) */
    private final Map<String, LinkedHashMap<String, RoaringBitmap>> columns = new LinkedHashMap<>();
    /** rows ever loaded */
    private int totalRows;
    /** bit i set iff row i currently live */
    private RoaringBitmap live = new RoaringBitmap();
    /** row id -> original row payload (retained for scan verification & echo) */
    private List<Map<String, String>> rows = new ArrayList<>();

    public static final class BuildResult {
        public final int rows;
        public final List<String> columns;

        BuildResult(int rows, List<String> columns) {
            this.rows = rows;
            this.columns = columns;
        }
    }

    /**
     * Build the index from an offline dataset. Any previously loaded data is
     * replaced; ids restart at 0 for the new dataset.
     */
    public synchronized BuildResult load(List<Map<String, Object>> rawRows, List<String> columnNames) {
        columns.clear();
        rows = new ArrayList<>(rawRows.size());

        List<String> cols = new ArrayList<>(columnNames);
        if (cols.isEmpty()) {
            for (Map<String, Object> r : rawRows) {
                for (String k : r.keySet()) {
                    if (!cols.contains(k)) {
                        cols.add(k);
                    }
                }
            }
        }
        // Per (column,value) growable list of row positions. Rows are appended in
        // ascending row-id order, so every list is already sorted and unique;
        // RoaringBitmap.fromSorted builds compressed containers in one pass
        // without O(n log n) inserts or array churn.
        Map<String, Map<String, IntBuffer>> builders = new LinkedHashMap<>();
        for (String c : cols) {
            columns.put(c, new LinkedHashMap<>());
            builders.put(c, new LinkedHashMap<>());
        }

        for (int rowId = 0; rowId < rawRows.size(); rowId++) {
            Map<String, Object> raw = rawRows.get(rowId);
            Map<String, String> stored = new LinkedHashMap<>();
            for (String c : cols) {
                Object v = raw.get(c);
                String sv = v == null ? null : String.valueOf(v);
                stored.put(c, sv);
                if (sv != null) {
                    builders.get(c).computeIfAbsent(sv, k -> new IntBuffer()).add(rowId);
                }
            }
            rows.add(stored);
        }

        for (String c : cols) {
            Map<String, IntBuffer> valueBuilders = builders.get(c);
            LinkedHashMap<String, RoaringBitmap> valueIndex = columns.get(c);
            for (Map.Entry<String, IntBuffer> e : valueBuilders.entrySet()) {
                valueIndex.put(e.getKey(), RoaringBitmap.fromSorted(e.getValue().toArray()));
            }
        }

        totalRows = rawRows.size();
        live = RoaringBitmap.range(0, totalRows);
        return new BuildResult(totalRows, cols);
    }

    /** Minimal growable int[]. */
    private static final class IntBuffer {
        private int[] data = new int[16];
        private int size;

        void add(int v) {
            if (size == data.length) {
                data = java.util.Arrays.copyOf(data, data.length * 2);
            }
            data[size++] = v;
        }

        int[] toArray() {
            return java.util.Arrays.copyOf(data, size);
        }
    }

    /* ------------------------------------------------------------------ */
    /* deletion                                                            */
    /* ------------------------------------------------------------------ */

    public synchronized int delete(int rowId) {
        if (rowId < 0 || rowId >= totalRows) {
            throw new IllegalArgumentException("rowId out of range [0," + totalRows + "): " + rowId);
        }
        boolean wasLive = live.contains(rowId);
        if (wasLive) {
            live.remove(rowId);
        }
        return live.cardinality();
    }

    public synchronized int restore(int rowId) {
        if (rowId < 0 || rowId >= totalRows) {
            throw new IllegalArgumentException("rowId out of range [0," + totalRows + "): " + rowId);
        }
        if (!live.contains(rowId)) {
            live.add(rowId);
        }
        return live.cardinality();
    }

    public synchronized boolean isLive(int rowId) {
        return live.contains(rowId);
    }

    /* ------------------------------------------------------------------ */
    /* queries                                                             */
    /* ------------------------------------------------------------------ */

    public synchronized RoaringBitmap query(Object expr) {
        RoaringBitmap result = eval(expr);
        // hard guarantee: nothing deleted can ever be returned
        return RoaringBitmap.and(result, live);
    }

    private RoaringBitmap eval(Object node) {
        if (!(node instanceof Map)) {
            throw new IllegalArgumentException("expression node must be an object, got: " + typeName(node));
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> n = (Map<String, Object>) node;
        Object opObj = n.get("op");
        if (!(opObj instanceof String)) {
            throw new IllegalArgumentException("node missing string 'op': " + n);
        }
        String op = (String) opObj;
        switch (op) {
            case "eq":
                return evalEq(n);
            case "and":
                return evalBoolList(n, true);
            case "or":
                return evalBoolList(n, false);
            case "not": {
                Object arg = n.get("arg");
                if (arg == null) {
                    throw new IllegalArgumentException("'not' node requires 'arg'");
                }
                // NOT is taken against the current live universe ONLY.
                return RoaringBitmap.notWithin(eval(arg), live);
            }
            default:
                throw new IllegalArgumentException("unknown op: " + op);
        }
    }

    private RoaringBitmap evalEq(Map<String, Object> n) {
        String col = requireString(n, "column");
        if (!columns.containsKey(col)) {
            throw new IllegalArgumentException("unknown column: " + col
                    + " (known: " + columns.keySet() + ")");
        }
        Object v = n.get("value");
        if (v == null) {
            throw new IllegalArgumentException("'eq' node requires non-null 'value'");
        }
        RoaringBitmap bm = columns.get(col).get(String.valueOf(v));
        return bm == null ? new RoaringBitmap() : bm.copy();
    }

    private RoaringBitmap evalBoolList(Map<String, Object> n, boolean isAnd) {
        Object argsObj = n.get("args");
        if (!(argsObj instanceof List)) {
            throw new IllegalArgumentException("'" + n.get("op") + "' node requires 'args' array");
        }
        @SuppressWarnings("unchecked")
        List<Object> args = (List<Object>) argsObj;
        if (args.isEmpty()) {
            // Identity elements: AND over no predicates = live universe;
            // OR over no predicates = empty.
            return isAnd ? live.copy() : new RoaringBitmap();
        }
        RoaringBitmap acc = eval(args.get(0));
        for (int i = 1; i < args.size(); i++) {
            RoaringBitmap next = eval(args.get(i));
            acc = isAnd ? RoaringBitmap.and(acc, next) : RoaringBitmap.or(acc, next);
        }
        return acc;
    }

    /* ------------------------------------------------------------------ */
    /* brute-force reference scan (used for acceptance verification)       */
    /* ------------------------------------------------------------------ */

    /** Line-by-line scan over currently live rows; the ground-truth oracle. */
    public synchronized List<Integer> scan(Object expr) {
        List<Integer> out = new ArrayList<>();
        for (int rowId = 0; rowId < totalRows; rowId++) {
            if (!live.contains(rowId)) {
                continue;
            }
            if (evalScan(expr, rows.get(rowId))) {
                out.add(rowId);
            }
        }
        return out;
    }

    private boolean evalScan(Object node, Map<String, String> row) {
        @SuppressWarnings("unchecked")
        Map<String, Object> n = (Map<String, Object>) node;
        String op = (String) n.get("op");
        switch (op) {
            case "eq": {
                String col = (String) n.get("column");
                String want = String.valueOf(n.get("value"));
                return want.equals(row.get(col));
            }
            case "and": {
                for (Object a : (List<?>) n.get("args")) {
                    if (!evalScan(a, row)) {
                        return false;
                    }
                }
                return true;
            }
            case "or": {
                for (Object a : (List<?>) n.get("args")) {
                    if (evalScan(a, row)) {
                        return true;
                    }
                }
                return false;
            }
            case "not":
                return !evalScan(n.get("arg"), row);
            default:
                throw new IllegalArgumentException("unknown op: " + op);
        }
    }

    /* ------------------------------------------------------------------ */
    /* introspection / stats                                               */
    /* ------------------------------------------------------------------ */

    public synchronized int totalRows() {
        return totalRows;
    }

    public synchronized int liveCount() {
        return live.cardinality();
    }

    public synchronized int[] liveRowIds() {
        return live.toArray();
    }

    public synchronized Map<String, String> getRow(int rowId) {
        if (rowId < 0 || rowId >= totalRows) {
            throw new IllegalArgumentException("rowId out of range: " + rowId);
        }
        return new LinkedHashMap<>(rows.get(rowId));
    }

    public static final class ColumnStat {
        public final String name;
        public final long distinctValues;
        public final long bitmapBytes;
        public final long setBits;
        public final int arrayContainers;
        public final int bitmapContainers;

        ColumnStat(String name, long distinctValues, long bitmapBytes, long setBits,
                   int arrayContainers, int bitmapContainers) {
            this.name = name;
            this.distinctValues = distinctValues;
            this.bitmapBytes = bitmapBytes;
            this.setBits = setBits;
            this.arrayContainers = arrayContainers;
            this.bitmapContainers = bitmapContainers;
        }
    }

    public synchronized List<ColumnStat> columnStats() {
        List<ColumnStat> stats = new ArrayList<>();
        for (Map.Entry<String, LinkedHashMap<String, RoaringBitmap>> e : columns.entrySet()) {
            long bytes = 0;
            long bits = 0;
            int arr = 0, bm = 0;
            for (RoaringBitmap b : e.getValue().values()) {
                bytes += b.serializedSizeBytes();
                bits += b.cardinality();
                int[] tc = b.containerTypeCounts();
                arr += tc[0];
                bm += tc[1];
            }
            stats.add(new ColumnStat(e.getKey(), e.getValue().size(), bytes, bits, arr, bm));
        }
        return stats;
    }

    public synchronized Map<String, Object> stats() {
        List<ColumnStat> cs = columnStats();
        long indexBytes = 0;
        long naiveBytes = 0;
        long distinctTotal = 0;
        List<Map<String, Object>> colJson = new ArrayList<>();
        for (ColumnStat c : cs) {
            indexBytes += c.bitmapBytes;
            distinctTotal += c.distinctValues;
            // uncompressed reference: each value bitmap as one bit per row
            naiveBytes += (c.distinctValues * totalRows + 7) / 8;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("column", c.name);
            m.put("distinctValues", c.distinctValues);
            m.put("setBits", c.setBits);
            m.put("arrayContainers", c.arrayContainers);
            m.put("bitmapContainers", c.bitmapContainers);
            m.put("indexBytes", c.bitmapBytes);
            m.put("uncompressedBytes", (c.distinctValues * totalRows + 7) / 8);
            colJson.add(m);
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("totalRows", totalRows);
        out.put("liveRows", live.cardinality());
        out.put("deletedRows", totalRows - live.cardinality());
        out.put("columns", columns.size());
        out.put("totalDistinctValues", distinctTotal);
        out.put("liveMaskBytes", live.serializedSizeBytes());
        out.put("indexBytes", indexBytes);
        out.put("uncompressedBitmapBytes", naiveBytes);
        out.put("compressionRatioVsUncompressed",
                naiveBytes == 0 ? 1.0 : round3((double) indexBytes / naiveBytes));
        out.put("perColumn", colJson);
        return out;
    }

    private static double round3(double v) {
        return Math.round(v * 1000.0) / 1000.0;
    }

    private static String requireString(Map<String, Object> n, String key) {
        Object v = n.get(key);
        if (!(v instanceof String)) {
            throw new IllegalArgumentException("missing string '" + key + "' in: " + n);
        }
        return (String) v;
    }

    private static String typeName(Object o) {
        return o == null ? "null" : o.getClass().getSimpleName();
    }
}
