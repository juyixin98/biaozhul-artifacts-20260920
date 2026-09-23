package vecq;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 投影 + 聚合执行器，直接消费 {@link SelectionVector}（稀疏 / 重复下标）。
 *
 * 正确性要点：
 *  - 只通过 Column.gather(rows) 按选择下标取值，绝不扫描整列，因此稀疏选择不会混入未选中行；
 *  - 每个下标处理一次，重复下标天然被重复投影 / 重复计入聚合（多重集语义）；
 *  - NULL 以位图为准，聚合忽略 NULL（count(*) 例外），空集 / 全 NULL 结果为 null；
 *  - 按 batchSize 对选择向量本身切批（而不是按源表行号），保证批边界独立于稀疏度。
 */
public final class ProjectAggregate {

    private final QueryPlan plan;
    private final Table table;

    public ProjectAggregate(QueryPlan plan) {
        this.plan = plan;
        this.table = plan.table();
    }

    // ---------------- 投影 ----------------

    /** 列式投影结果：保持请求列顺序；值数组与选择向量等长（含重复）。 */
    public Map<String, List<Object>> project(SelectionVector sv, ExecStats stats) {
        int[] rows = sv.toArray();
        Map<String, List<Object>> out = new LinkedHashMap<>();
        List<Column> cols = new ArrayList<>();
        for (String name : plan.projection()) {
            Column c = table.column(name);
            cols.add(c);
            out.put(name, new ArrayList<>(rows.length));
        }
        int batches = 0;
        for (int start = 0; start < rows.length; start += plan.batchSize()) {
            batches++;
            int len = Math.min(plan.batchSize(), rows.length - start);
            int[] chunk = new int[len];
            System.arraycopy(rows, start, chunk, 0, len);
            for (int k = 0; k < cols.size(); k++) {
                Object[] vals = cols.get(k).gather(chunk);
                List<Object> sink = out.get(plan.projection().get(k));
                for (Object v : vals) sink.add(v);
            }
        }
        if (stats != null) stats.downstreamBatches += batches;
        return out;
    }

    // ---------------- 聚合 ----------------

    /**
     * 聚合入口。
     * 无 groupBy：返回单行 Map（列名 -> 值）；
     * 有 groupBy：返回 groups（有序，按组在选择向量中首次出现的顺序）与 groupColumns。
     */
    public AggResult aggregate(SelectionVector sv, ExecStats stats) {
        if (plan.groupBy().isEmpty()) {
            return global(sv, stats);
        }
        return grouped(sv, stats);
    }

    private AggResult global(SelectionVector sv, ExecStats stats) {
        int[] rows = sv.toArray();
        Accumulator[] accs = buildAccumulators();
        int batches = 0;
        for (int start = 0; start < rows.length; start += plan.batchSize()) {
            batches++;
            int len = Math.min(plan.batchSize(), rows.length - start);
            for (Accumulator a : accs) a.update(rows, start, len);
        }
        if (stats != null) stats.downstreamBatches += batches;
        Map<String, Object> row = new LinkedHashMap<>();
        for (Accumulator a : accs) row.put(a.name, a.finish());
        return AggResult.single(row);
    }

    private AggResult grouped(SelectionVector sv, ExecStats stats) {
        int[] rows = sv.toArray();
        List<Column> groupCols = new ArrayList<>();
        for (String g : plan.groupBy()) groupCols.add(table.column(g));

        LinkedHashMap<List<Object>, Accumulator[]> groups = new LinkedHashMap<>();
        int batches = 0;
        for (int start = 0; start < rows.length; start += plan.batchSize()) {
            batches++;
            int len = Math.min(plan.batchSize(), rows.length - start);
            for (int i = 0; i < len; i++) {
                int row = rows[start + i];
                List<Object> key = new ArrayList<>(groupCols.size());
                for (Column c : groupCols) key.add(c.isNull(row) ? null : scalarValue(c, row));
                Accumulator[] accs = groups.get(key);
                if (accs == null) {
                    accs = buildAccumulators();
                    // 新建 List 作为长期持有的 key（不能用 List.copyOf：分组键允许为 null）
                    groups.put(new ArrayList<>(key), accs);
                }
                for (Accumulator a : accs) a.update(new int[]{row}, 0, 1);
            }
        }
        if (stats != null) stats.downstreamBatches += batches;

        // 输出
        List<Map<String, Object>> outRows = new ArrayList<>();
        for (Map.Entry<List<Object>, Accumulator[]> e : groups.entrySet()) {
            Map<String, Object> r = new LinkedHashMap<>();
            List<Object> key = e.getKey();
            for (int k = 0; k < groupCols.size(); k++) {
                r.put(plan.groupBy().get(k), key.get(k));
            }
            for (Accumulator a : e.getValue()) r.put(a.name, a.finish());
            outRows.add(r);
        }
        return AggResult.grouped(plan.groupBy(), outRows);
    }

    private static Object scalarValue(Column c, int row) {
        if (c instanceof IntColumn ic) return ic.getInt(row);
        return ((StringColumn) c).getString(row);
    }

    private Accumulator[] buildAccumulators() {
        Accumulator[] accs = new Accumulator[plan.aggregates().size()];
        for (int i = 0; i < accs.length; i++) {
            accs[i] = Accumulator.create(plan.aggregates().get(i), table);
        }
        return accs;
    }

    /** 聚合结果（两种形态之一）。 */
    public static final class AggResult {
        public final Map<String, Object> globalRow;          // 非 null：全局聚合单行
        public final List<String> groupColumns;              // 非 null：分组聚合
        public final List<Map<String, Object>> groupRows;

        private AggResult(Map<String, Object> g, List<String> gc, List<Map<String, Object>> gr) {
            this.globalRow = g;
            this.groupColumns = gc;
            this.groupRows = gr;
        }

        static AggResult single(Map<String, Object> row) {
            return new AggResult(row, null, null);
        }

        static AggResult grouped(List<String> gc, List<Map<String, Object>> rows) {
            return new AggResult(null, gc, rows);
        }
    }
}
