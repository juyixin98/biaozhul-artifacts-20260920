package tvl.engine;

import java.util.ArrayList;
import java.util.List;

import tvl.evaluator.Evaluator;
import tvl.expr.Tri;

/**
 * 单机内存查询引擎：不依赖任何 SQL/关系引擎，直接在 {@link Catalog} 的内存行上执行。
 *
 * 执行管线（自底向上）：
 * SCAN -> FILTER（3VL：仅 TRUE 通过，FALSE/UNKNOWN 均被剔除）-> PROJECT -> LIMIT。
 * 每个节点记录 rowsIn/rowsOut，形成可导出的执行计划。
 */
public final class QueryEngine {

    private final Catalog catalog;
    private final Evaluator evaluator = new Evaluator();

    public QueryEngine(Catalog catalog) {
        this.catalog = catalog;
    }

    public QueryResult execute(QuerySpec spec) {
        Table table = catalog.get(spec.table());
        if (table == null) {
            throw new EngineException("unknown table '" + spec.table() + "'");
        }

        List<String> outputColumns = resolveOutputColumns(spec.select(), table);
        if (spec.limit() != null && spec.limit() < 0) {
            throw new EngineException("limit must be non-negative");
        }

        // ---- 构建执行计划树（自底向上），同时持有各节点引用以便回填统计 ----
        PlanNode scan = new PlanNode("SCAN", "table=" + table.name());
        PlanNode root = scan;
        PlanNode filterNode = null;
        if (spec.filter() != null) {
            filterNode = new PlanNode("FILTER", "where=" + spec.filterText());
            filterNode.addChild(root);
            root = filterNode;
        }
        PlanNode projectNode = new PlanNode(
                "PROJECT", "columns=" + String.join(", ", outputColumns));
        projectNode.addChild(root);
        root = projectNode;
        PlanNode limitNode = null;
        if (spec.limit() != null) {
            limitNode = new PlanNode("LIMIT", "limit=" + spec.limit());
            limitNode.addChild(root);
            root = limitNode;
        }

        // ---- SCAN ----
        List<Row> current = table.rows();
        long scanned = current.size();
        scan.setRowsIn(scanned);
        scan.setRowsOut(scanned);

        // ---- FILTER（三值逻辑：只有 TRUE 让行通过）----
        long matched = scanned;
        if (filterNode != null) {
            List<Row> kept = new ArrayList<>();
            for (Row row : current) {
                Tri tri = evaluator.evalPredicate(spec.filter(), row);
                if (tri == Tri.TRUE) {
                    kept.add(row);
                }
            }
            filterNode.setRowsIn(current.size());
            filterNode.setRowsOut(kept.size());
            current = kept;
            matched = kept.size();
        }

        // ---- PROJECT ----
        List<Row> projected = new ArrayList<>();
        for (Row row : current) {
            Row pruned = new Row();
            for (String col : outputColumns) {
                pruned.set(col, row.get(col));
            }
            projected.add(pruned);
        }
        projectNode.setRowsIn(current.size());
        projectNode.setRowsOut(projected.size());
        current = projected;

        // ---- LIMIT ----
        if (limitNode != null) {
            long lim = spec.limit();
            List<Row> limited = lim >= current.size()
                    ? current
                    : new ArrayList<>(current.subList(0, (int) lim));
            limitNode.setRowsIn(current.size());
            limitNode.setRowsOut(limited.size());
            current = limited;
        }

        return new QueryResult(outputColumns, current, root, scanned, matched);
    }

    private static List<String> resolveOutputColumns(List<String> select, Table table) {
        if (select == null || select.isEmpty()
                || (select.size() == 1 && "*".equals(select.get(0)))) {
            return new ArrayList<>(table.columns().keySet());
        }
        for (String col : select) {
            if ("*".equals(col)) {
                throw new EngineException("'*' cannot be combined with explicit columns");
            }
            if (!table.columns().containsKey(col)) {
                throw new EngineException("unknown output column '" + col + "'");
            }
        }
        return new ArrayList<>(select);
    }
}
