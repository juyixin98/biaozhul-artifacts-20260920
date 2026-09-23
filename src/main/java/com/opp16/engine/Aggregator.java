package com.opp16.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;

/**
 * Streaming aggregation driven by a {@link SelectionVector}.
 *
 * Every selected index is visited in order, so a sparse filter scans only the
 * survivors and repeated indices (e.g. {@code [3,3,3]}) are counted three
 * times - exactly as SQL semantics over a relation with duplicate rows demand.
 *
 * <p>Types: COUNT -&gt; LONG; SUM/MIN/MAX of INT -&gt; LONG; AVG -&gt; DOUBLE.
 * NULL inputs are skipped. A query without GROUP BY always emits one row even
 * when zero rows are selected (COUNT(*)=0, other aggregates NULL).
 */
public final class Aggregator {

    /** Mutable single-group accumulator. Indexed per aggregate spec. */
    private static final class Acc {
        long countRows;     // count(*) across all input rows
        long[] counts;      // non-null count per aggregate spec
        long[] sums;        // SUM / AVG running sum (AVG divides by counts at finish)
        long[] minLong;
        long[] maxLong;
        boolean[] hasValue; // whether MIN/MAX saw a non-null value

        Acc(int n) {
            counts = new long[n];
            sums = new long[n];
            minLong = new long[n];
            maxLong = new long[n];
            hasValue = new boolean[n];
        }
    }

    private final Table table;
    private final QueryPlan plan;
    private final List<Column> groupCols;

    public Aggregator(Table table, QueryPlan plan) {
        this.table = table;
        this.plan = plan;
        this.groupCols = new ArrayList<>();
        for (String g : plan.groupBy) groupCols.add(table.column(g));
        for (QueryPlan.AggregateSpec spec : plan.aggregates) {
            if (spec.column != null) table.column(spec.column).asInt();
        }
    }

    /** Output schema: group-by columns first, then aggregate aliases. */
    public List<String> outputColumns() {
        List<String> out = new ArrayList<>(plan.groupBy);
        for (QueryPlan.AggregateSpec a : plan.aggregates) out.add(a.alias);
        return out;
    }

    /**
     * Aggregate.
     *
     * @return rows; each row is {@code Object[]} with {@code Long}, {@code Double},
     *         {@code String} or {@code null}, aligned with {@link #outputColumns()}.
     */
    public List<Object[]> run(SelectionVector sv) {
        int nAgg = plan.aggregates.size();
        if (groupCols.isEmpty()) {
            Acc acc = new Acc(nAgg);
            consume(acc, sv, 0, sv.size());
            List<Object[]> rows = new ArrayList<>(1);
            rows.add(finish(acc, null));
            return rows;
        }

        LinkedHashMap<List<Object>, Acc> groups = new LinkedHashMap<>();
        for (int i = 0; i < sv.size(); i++) {
            int row = sv.get(i);
            List<Object> key = new ArrayList<>(groupCols.size());
            for (Column c : groupCols) key.add(c.boxedAt(row)); // null group key allowed
            Acc acc = groups.get(key);
            if (acc == null) {
                acc = new Acc(nAgg);
                groups.put(key, acc);
            }
            update(acc, row);
        }
        List<Object[]> rows = new ArrayList<>(groups.size());
        for (java.util.Map.Entry<List<Object>, Acc> e : groups.entrySet()) {
            rows.add(finish(e.getValue(), e.getKey()));
        }
        return rows;
    }

    private void consume(Acc acc, SelectionVector sv, int from, int to) {
        for (int i = from; i < to; i++) update(acc, sv.get(i));
    }

    private void update(Acc acc, int row) {
        for (int k = 0; k < plan.aggregates.size(); k++) {
            QueryPlan.AggregateSpec spec = plan.aggregates.get(k);
            if (spec.column == null) {
                acc.countRows++;
                continue;
            }
            Column c = table.column(spec.column);
            if (c.isNullAt(row)) continue; // NULLs skipped by all column aggregates
            long v = c.asInt().getLong(row);
            acc.counts[k]++;
            switch (spec.function) {
                case "COUNT" -> { /* counter suffices */ }
                case "SUM" -> acc.sums[k] += v; // long overflow mirrors java long semantics
                case "AVG" -> acc.sums[k] += v;
                case "MIN" -> { if (!acc.hasValue[k] || v < acc.minLong[k]) acc.minLong[k] = v; acc.hasValue[k] = true; }
                case "MAX" -> { if (!acc.hasValue[k] || v > acc.maxLong[k]) acc.maxLong[k] = v; acc.hasValue[k] = true; }
                default -> throw new EngineException("unknown aggregate function: " + spec.function);
            }
        }
    }

    private Object[] finish(Acc acc, List<Object> groupKey) {
        int width = groupCols.size() + plan.aggregates.size();
        Object[] out = new Object[width];
        if (groupKey != null) {
            for (int i = 0; i < groupKey.size(); i++) out[i] = groupKey.get(i);
        } else {
            for (int i = 0; i < groupCols.size(); i++) out[i] = null; // unreachable: groups always carry keys
        }
        int base = groupCols.size();
        for (int k = 0; k < plan.aggregates.size(); k++) {
            QueryPlan.AggregateSpec spec = plan.aggregates.get(k);
            switch (spec.function) {
                case "COUNT" -> out[base + k] = spec.column == null ? acc.countRows : acc.counts[k];
                case "SUM" -> out[base + k] = acc.counts[k] == 0 ? null : acc.sums[k];
                case "AVG" -> out[base + k] = acc.counts[k] == 0 ? null
                        : (double) acc.sums[k] / acc.counts[k];
                case "MIN" -> out[base + k] = acc.hasValue[k] ? acc.minLong[k] : null;
                case "MAX" -> out[base + k] = acc.hasValue[k] ? acc.maxLong[k] : null;
                default -> throw new EngineException("unknown aggregate function: " + spec.function);
            }
        }
        return out;
    }
}
