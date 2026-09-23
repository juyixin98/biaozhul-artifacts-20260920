package hllengine.engine;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Result of executing a plan: column order, output rows, and stats. */
public final class QueryResult {

    private final List<String> columns;
    private final List<Map<String, Object>> rows;
    private final long rowsScanned;
    private final long rowsAfterFilter;
    private final boolean truncatedByLimit;
    private final boolean grouped;
    private final List<String> groupBy;
    private final List<AggSpec> aggregates;

    private QueryResult(List<String> columns, List<Map<String, Object>> rows,
                        long rowsScanned, long rowsAfterFilter, boolean truncated,
                        boolean grouped, List<String> groupBy, List<AggSpec> aggregates) {
        this.columns = columns;
        this.rows = rows;
        this.rowsScanned = rowsScanned;
        this.rowsAfterFilter = rowsAfterFilter;
        this.truncatedByLimit = truncated;
        this.grouped = grouped;
        this.groupBy = groupBy;
        this.aggregates = aggregates;
    }

    static QueryResult ofRows(List<String> columns, List<Map<String, Object>> rows,
                              long scanned, long afterFilter, boolean truncated) {
        return new QueryResult(columns, rows, scanned, afterFilter, truncated,
                false, List.of(), List.of());
    }

    static QueryResult ofGroups(List<String> columns, List<Map<String, Object>> rows,
                                long scanned, long afterFilter, boolean truncated,
                                List<String> groupBy, List<AggSpec> aggregates) {
        return new QueryResult(columns, rows, scanned, afterFilter, truncated,
                true, groupBy, aggregates);
    }

    public List<String> columns() {
        return columns;
    }

    public List<Map<String, Object>> rows() {
        return rows;
    }

    public boolean grouped() {
        return grouped;
    }

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("columns", columns);
        m.put("rows", rows);
        m.put("rowCount", rows.size());
        m.put("grouped", grouped);
        m.put("truncatedByLimit", truncatedByLimit);
        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("rowsScanned", rowsScanned);
        stats.put("rowsAfterFilter", rowsAfterFilter);
        stats.put("rowsReturned", (long) rows.size());
        m.put("stats", stats);

        // Surface which output columns are approximate, so consumers never have
        // to infer it from the aggregate name.
        if (!aggregates.isEmpty()) {
            Map<String, Object> approx = new LinkedHashMap<>();
            for (AggSpec s : aggregates) {
                if (s.approximate()) {
                    Map<String, Object> info = new LinkedHashMap<>();
                    info.put("valueColumn", s.alias());
                    info.put("errorColumn", s.errorAlias());
                    info.put("isEstimate", true);
                    info.put("isExact", false);
                    approx.put(s.alias(), info);
                }
            }
            m.put("approximateColumns", approx);
        }
        return m;
    }
}
