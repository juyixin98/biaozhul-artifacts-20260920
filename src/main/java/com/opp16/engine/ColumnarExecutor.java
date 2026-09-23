package com.opp16.engine;

import java.util.ArrayList;
import java.util.List;

/**
 * Vectorised execution path.
 *
 * <ol>
 *   <li>Filter scans the table in fixed-size batches. Each batch is walked in
 *       64-row word chunks; the predicate returns a {@link Predicate.TriMask},
 *       and TRUE positions are appended into one {@link SelectionVector}.</li>
 *   <li>Projection/aggregation walk that vector, so NULL-rejected and
 *       unmatched rows are never materialised; repeated indices are visited
 *       repeatedly (sparse + duplicate semantics).</li>
 * </ol>
 */
public final class ColumnarExecutor {

    /** Logical type of a result column (storage columns only have INT/STRING). */
    public enum OutputType { INT, DOUBLE, STRING }

    /** Materialised query result (also produced by the reference interpreter). */
    public static final class Result {
        public final List<String> columns;
        public final List<OutputType> types;
        public final List<Object[]> rows;

        Result(List<String> columns, List<OutputType> types, List<Object[]> rows) {
            this.columns = columns;
            this.types = types;
            this.rows = rows;
        }
    }

    public static final class SelectionWithStats {
        public final SelectionVector selection;
        public final BatchStats stats;
        SelectionWithStats(SelectionVector selection, BatchStats stats) {
            this.selection = selection;
            this.stats = stats;
        }
    }

    private final Table table;
    private final QueryPlan plan;

    public ColumnarExecutor(Table table, QueryPlan plan) {
        this.table = table;
        this.plan = plan;
    }

    /** Build the selection vector, recording per-batch statistics. */
    public SelectionWithStats buildSelection() {
        BatchStats stats = new BatchStats();
        stats.setBatchSize(plan.batchSize);

        if (plan.explicitSelection != null) {
            // Injection hook: caller-supplied subscripts (sparse / duplicated /
            // invalid). fromIndices validates every subscript against rowCount.
            return new SelectionWithStats(
                    SelectionVector.fromIndices(table.rowCount(), plan.explicitSelection), stats);
        }

        SelectionVector sv = new SelectionVector(table.rowCount());
        int n = table.rowCount();
        int bi = 0;
        if (plan.predicate != null) plan.predicate.bind(table);

        for (int base = 0; base < n; base += plan.batchSize) {
            int batchEnd = Math.min(base + plan.batchSize, n);
            int before = sv.size();
            if (plan.predicate == null) {
                for (int r = base; r < batchEnd; r++) sv.append(r);
            } else {
                // Word chunks of up to 64 rows inside the configured batch.
                for (int w = base; w < batchEnd; w += 64) {
                    int len = Math.min(64, batchEnd - w);
                    Predicate.TriMask tri = plan.predicate.evalBatch(w, len);
                    // SQL WHERE keeps TRUE only; FALSE and UNKNOWN are dropped.
                    sv.appendMask(tri.trueMask, w, len);
                }
            }
            stats.recordBatch(bi++, base, batchEnd, sv.size() - before);
        }
        if (n == 0) stats.recordBatch(0, 0, 0, 0);
        return new SelectionWithStats(sv, stats);
    }

    /** Full plan execution: selection then projection or aggregation. */
    public Result execute() {
        return executeWith(buildSelection().selection);
    }

    public Result executeWith(SelectionVector sv) {
        if (!plan.hasAggregates()) return project(sv);

        Aggregator agg = new Aggregator(table, plan);
        List<String> allNames = agg.outputColumns();
        List<OutputType> allTypes = new ArrayList<>();
        for (String name : allNames) allTypes.add(inferOutputType(name));

        if (plan.projection != null) {
            for (String name : plan.projection) {
                if (!allNames.contains(name)) {
                    throw new EngineException("projection column '" + name
                            + "' not found in aggregate output " + allNames);
                }
            }
        }

        List<Object[]> raw = agg.run(sv);
        if (plan.projection == null) {
            return new Result(allNames, allTypes, raw);
        }
        int[] idx = new int[plan.projection.size()];
        List<OutputType> types = new ArrayList<>();
        for (int i = 0; i < idx.length; i++) {
            idx[i] = allNames.indexOf(plan.projection.get(i));
            types.add(allTypes.get(idx[i]));
        }
        List<Object[]> rows = new ArrayList<>(raw.size());
        for (Object[] r : raw) {
            Object[] nr = new Object[idx.length];
            for (int i = 0; i < idx.length; i++) nr[i] = r[idx[i]];
            rows.add(nr);
        }
        return new Result(new ArrayList<>(plan.projection), types, rows);
    }

    /** Project selected rows of the base table (no aggregates). */
    private Result project(SelectionVector sv) {
        List<Column> cols = resolveProjection();
        List<String> names = new ArrayList<>();
        List<OutputType> types = new ArrayList<>();
        for (Column c : cols) {
            names.add(c.name);
            types.add(c.type == ColumnType.STRING ? OutputType.STRING : OutputType.INT);
        }
        List<Object[]> rows = new ArrayList<>(sv.size());
        for (int i = 0; i < sv.size(); i++) {
            int r = sv.get(i); // repeated index -> repeated output row
            Object[] row = new Object[cols.size()];
            for (int k = 0; k < cols.size(); k++) row[k] = cols.get(k).boxedAt(r);
            rows.add(row);
        }
        return new Result(names, types, rows);
    }

    private List<Column> resolveProjection() {
        List<Column> cols = new ArrayList<>();
        if (plan.projection == null) {
            cols.addAll(table.columns());
        } else {
            for (String name : plan.projection) cols.add(table.column(name));
        }
        return cols;
    }

    private OutputType inferOutputType(String outputName) {
        if (plan.groupBy.contains(outputName)) {
            return table.column(outputName).type == ColumnType.STRING
                    ? OutputType.STRING : OutputType.INT;
        }
        for (QueryPlan.AggregateSpec a : plan.aggregates) {
            if (a.alias.equals(outputName)) {
                return "AVG".equals(a.function) ? OutputType.DOUBLE : OutputType.INT;
            }
        }
        throw new EngineException("internal: cannot infer output type of " + outputName);
    }
}
