package com.opp16.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Row-at-a-time reference interpreter.
 *
 * Deliberately written independently of the vectorised path (scalar predicate
 * evaluation, its own selection build, its own accumulators). Every test query
 * runs through both engines and the results must be identical; any divergence
 * is a bug in one of the two.
 */
public final class RowInterpreter {

    private final Table table;
    private final QueryPlan plan;

    public RowInterpreter(Table table, QueryPlan plan) {
        this.table = table;
        this.plan = plan;
    }

    public ColumnarExecutor.Result execute() {
        SelectionVector sv = buildSelection();
        if (!plan.hasAggregates()) return project(sv);
        return aggregate(sv);
    }

    public SelectionVector buildSelection() {
        SelectionVector sv = new SelectionVector(table.rowCount());
        if (plan.explicitSelection != null) {
            return SelectionVector.fromIndices(table.rowCount(), plan.explicitSelection);
        }
        if (plan.predicate == null) {
            for (int r = 0; r < table.rowCount(); r++) sv.append(r);
            return sv;
        }
        plan.predicate.bind(table);
        for (int r = 0; r < table.rowCount(); r++) {
            Boolean keep = plan.predicate.evalRow(r);
            if (Boolean.TRUE.equals(keep)) sv.append(r);
        }
        return sv;
    }

    private ColumnarExecutor.Result project(SelectionVector sv) {
        List<Column> cols = new ArrayList<>();
        if (plan.projection == null) cols.addAll(table.columns());
        else for (String name : plan.projection) cols.add(table.column(name));

        List<String> names = new ArrayList<>();
        List<ColumnarExecutor.OutputType> types = new ArrayList<>();
        for (Column c : cols) {
            names.add(c.name);
            types.add(c.type == ColumnType.STRING
                    ? ColumnarExecutor.OutputType.STRING : ColumnarExecutor.OutputType.INT);
        }
        List<Object[]> rows = new ArrayList<>(sv.size());
        for (int i = 0; i < sv.size(); i++) {
            int r = sv.get(i);
            Object[] row = new Object[cols.size()];
            for (int k = 0; k < cols.size(); k++) row[k] = cols.get(k).boxedAt(r);
            rows.add(row);
        }
        return new ColumnarExecutor.Result(names, types, rows);
    }

    // ---- independent scalar aggregation ----------------------------------

    private static final class ScalarAcc {
        long countStar;
        final long[] countCol;
        final long[] sum;
        final Long[] min;
        final Long[] max;

        ScalarAcc(int n) {
            countCol = new long[n];
            sum = new long[n];
            min = new Long[n];
            max = new Long[n];
        }
    }

    private ColumnarExecutor.Result aggregate(SelectionVector sv) {
        int nAgg = plan.aggregates.size();
        List<Column> groupCols = new ArrayList<>();
        for (String g : plan.groupBy) groupCols.add(table.column(g));

        LinkedHashMap<List<Object>, ScalarAcc> groups = new LinkedHashMap<>();
        boolean noGroup = groupCols.isEmpty();
        ScalarAcc single = new ScalarAcc(nAgg);
        if (noGroup) groups.put(new ArrayList<>(), single);

        for (int i = 0; i < sv.size(); i++) {
            int row = sv.get(i);
            List<Object> key = new ArrayList<>(groupCols.size());
            for (Column c : groupCols) key.add(c.boxedAt(row));
            ScalarAcc acc = noGroup ? single : groups.get(key);
            if (acc == null && !noGroup) {
                acc = new ScalarAcc(nAgg);
                groups.put(key, acc);
            }
            acc.countStar++;
            for (int k = 0; k < nAgg; k++) {
                QueryPlan.AggregateSpec spec = plan.aggregates.get(k);
                if (spec.column == null) continue; // COUNT(*)
                Column c = table.column(spec.column);
                if (c.isNullAt(row)) continue;
                long v = c.asInt().getLong(row);
                acc.countCol[k]++;
                switch (spec.function) {
                    case "COUNT" -> { }
                    case "SUM", "AVG" -> acc.sum[k] += v;
                    case "MIN" -> acc.min[k] = (acc.min[k] == null || v < acc.min[k]) ? v : acc.min[k];
                    case "MAX" -> acc.max[k] = (acc.max[k] == null || v > acc.max[k]) ? v : acc.max[k];
                    default -> throw new EngineException("unknown aggregate: " + spec.function);
                }
            }
        }

        List<String> allNames = new ArrayList<>(plan.groupBy);
        for (QueryPlan.AggregateSpec a : plan.aggregates) allNames.add(a.alias);

        List<Object[]> raw = new ArrayList<>(groups.size());
        for (Map.Entry<List<Object>, ScalarAcc> e : groups.entrySet()) {
            Object[] r = new Object[groupCols.size() + nAgg];
            for (int i = 0; i < groupCols.size(); i++) r[i] = e.getKey().get(i);
            ScalarAcc acc = e.getValue();
            for (int k = 0; k < nAgg; k++) {
                QueryPlan.AggregateSpec spec = plan.aggregates.get(k);
                int p = groupCols.size() + k;
                switch (spec.function) {
                    case "COUNT" -> r[p] = spec.column == null ? acc.countStar : acc.countCol[k];
                    case "SUM" -> r[p] = acc.countCol[k] == 0 ? null : acc.sum[k];
                    case "AVG" -> r[p] = acc.countCol[k] == 0 ? null : (double) acc.sum[k] / acc.countCol[k];
                    case "MIN" -> r[p] = acc.min[k];
                    case "MAX" -> r[p] = acc.max[k];
                    default -> throw new EngineException("unknown aggregate: " + spec.function);
                }
            }
            raw.add(r);
        }

        List<ColumnarExecutor.OutputType> allTypes = new ArrayList<>();
        for (String name : allNames) {
            if (plan.groupBy.contains(name)) {
                allTypes.add(table.column(name).type == ColumnType.STRING
                        ? ColumnarExecutor.OutputType.STRING : ColumnarExecutor.OutputType.INT);
            } else {
                String fn = plan.aggregates.get(allNames.indexOf(name) - groupCols.size()).function;
                allTypes.add("AVG".equals(fn)
                        ? ColumnarExecutor.OutputType.DOUBLE : ColumnarExecutor.OutputType.INT);
            }
        }

        if (plan.projection == null) {
            return new ColumnarExecutor.Result(allNames, allTypes, raw);
        }
        int[] idx = new int[plan.projection.size()];
        List<ColumnarExecutor.OutputType> types = new ArrayList<>();
        for (int i = 0; i < idx.length; i++) {
            String name = plan.projection.get(i);
            int at = allNames.indexOf(name);
            if (at < 0) throw new EngineException(
                    "projection column '" + name + "' not found in aggregate output " + allNames);
            idx[i] = at;
            types.add(allTypes.get(at));
        }
        List<Object[]> rows = new ArrayList<>(raw.size());
        for (Object[] r : raw) {
            Object[] nr = new Object[idx.length];
            for (int i = 0; i < idx.length; i++) nr[i] = r[idx[i]];
            rows.add(nr);
        }
        return new ColumnarExecutor.Result(new ArrayList<>(plan.projection), types, rows);
    }
}
