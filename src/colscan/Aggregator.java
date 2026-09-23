package colscan;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Aggregations. NULL inputs are SKIPPED for every function (count, sum, min,
 * max, avg) — only {@code COUNT_ROWS} (count(*)) counts every matched row,
 * NULLs included. avg is emitted as a double; over zero non-null inputs it is
 * null, never 0.
 */
public final class Aggregator {

    public enum Kind { COUNT_ROWS, COUNT, SUM, MIN, MAX, AVG }

    public final Kind kind;
    public final String column;   // null for COUNT_ROWS
    public final String alias;

    private Aggregator(Kind kind, String column, String alias) {
        this.kind = kind;
        this.column = column;
        this.alias = alias;
    }

    public static List<Aggregator> parseAll(Object json) {
        List<Aggregator> out = new java.util.ArrayList<>();
        if (json == null) return out;
        if (!(json instanceof List)) {
            throw new IllegalArgumentException("'aggregates' must be an array");
        }
        for (Object o : (List<?>) json) {
            if (!(o instanceof Map)) {
                throw new IllegalArgumentException("each aggregate must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) o;
            String fn = ((String) m.getOrDefault("fn", "")).toUpperCase();
            String col = (String) m.get("column");
            String alias = (String) m.get("alias");
            Kind kind;
            switch (fn) {
                case "COUNT_ROWS": case "COUNT_STAR": case "COUNT(*)":
                    kind = Kind.COUNT_ROWS;
                    if (alias == null) alias = "count_rows";
                    break;
                case "COUNT": kind = Kind.COUNT; break;
                case "SUM": kind = Kind.SUM; break;
                case "MIN": kind = Kind.MIN; break;
                case "MAX": kind = Kind.MAX; break;
                case "AVG": case "MEAN": kind = Kind.AVG; break;
                default: throw new IllegalArgumentException("unknown aggregate fn: " + fn);
            }
            if (kind != Kind.COUNT_ROWS && col == null) {
                throw new IllegalArgumentException(fn + " requires a column");
            }
            if (alias == null) alias = fn.toLowerCase() + "_" + (col == null ? "" : col);
            out.add(new Aggregator(kind, col, alias));
        }
        return out;
    }

    /** Mutable accumulator with correct NULL semantics. */
    public static final class Acc {
        long countRows;
        long countNonNull;
        long sum;
        Long min;
        Long max;

        void addRow(Value v) {
            countRows++;
            if (!v.present) return; // NULL: counted by count(*) only, never treated as 0
            countNonNull++;
            sum += v.longValue;
            if (min == null || v.longValue < min) min = v.longValue;
            if (max == null || v.longValue > max) max = v.longValue;
        }

        Object value(Kind kind) {
            switch (kind) {
                case COUNT_ROWS: return countRows;
                case COUNT: return countNonNull;
                case SUM: return countNonNull == 0 ? null : sum;
                case MIN: return min;
                case MAX: return max;
                case AVG: return countNonNull == 0 ? null : ((double) sum) / countNonNull;
                default: throw new IllegalStateException();
            }
        }

        void merge(Acc other) {
            countRows += other.countRows;
            countNonNull += other.countNonNull;
            sum += other.sum;
            if (other.min != null && (min == null || other.min < min)) min = other.min;
            if (other.max != null && (max == null || other.max > max)) max = other.max;
        }
    }

    /**
     * Per-shard accumulator map keyed by aggregate alias. COUNT_ROWS does not
     * need any column; every other aggregate reads exactly one column.
     */
    public static Map<String, Acc> newAccumulators(List<Aggregator> aggs) {
        Map<String, Acc> m = new LinkedHashMap<>();
        for (Aggregator a : aggs) m.put(a.alias, new Acc());
        return m;
    }
}
