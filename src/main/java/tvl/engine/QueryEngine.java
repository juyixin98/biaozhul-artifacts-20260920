package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.TriBool;
import tvl.core.Value;
import tvl.eval.Evaluator;
import tvl.parser.Expr;

/**
 * 查询执行器：对一张内存表逐行执行已编译的过滤表达式。
 *
 * 三值逻辑过滤语义：仅表达式结果为 TRUE 的行被选中；
 * FALSE 与 UNKNOWN（NULL 参与比较等情况）均被排除，
 * 同时在结果中给出每行的三值判定，便于核对 3VL 行为。
 *
 * 执行结果包含一个可导出的执行计划：扫描节点 + 过滤 AST。
 */
public final class QueryEngine {

    private final Table table;
    private final Expr filter;
    private final Evaluator evaluator;

    public QueryEngine(Table table, Expr filter) {
        this.table = table;
        this.filter = filter;
        this.evaluator = new Evaluator(table.columnNames());
    }

    /** 单行求值结果：三值判定 + 是否选中。 */
    public static final class RowOutcome {
        public final int rowIndex;
        public final TriBool tri;
        public final boolean selected;

        RowOutcome(int rowIndex, TriBool tri) {
            this.rowIndex = rowIndex;
            this.tri = tri;
            this.selected = tri == TriBool.TRUE;
        }

        public Map<String, Object> toJson(Value[] row) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("rowIndex", rowIndex);
            m.put("triValue", tri.name());
            m.put("selected", selected);
            List<Object> r = new ArrayList<>();
            for (Value v : row) {
                r.add(v.toJson());
            }
            m.put("row", r);
            return m;
        }
    }

    /** 执行结果：选中行 + 全部行的三值判定 + 统计。 */
    public static final class QueryResult {
        public final List<Value[]> selectedRows;
        public final List<RowOutcome> outcomes;

        QueryResult(List<Value[]> selectedRows, List<RowOutcome> outcomes) {
            this.selectedRows = selectedRows;
            this.outcomes = outcomes;
        }

        public Map<String, Object> toJson(Table table) {
            Map<String, Object> m = new LinkedHashMap<>();

            List<Object> sel = new ArrayList<>();
            for (Value[] row : selectedRows) {
                List<Object> r = new ArrayList<>();
                for (Value v : row) {
                    r.add(v.toJson());
                }
                sel.add(r);
            }
            m.put("selectedCount", selectedRows.size());
            m.put("selectedRows", sel);

            List<Object> detail = new ArrayList<>();
            for (RowOutcome o : outcomes) {
                detail.add(o.toJson(table.rows().get(o.rowIndex)));
            }
            m.put("evaluatedRows", detail);

            int t = 0, f = 0, u = 0;
            for (RowOutcome o : outcomes) {
                switch (o.tri) {
                    case TRUE: t++; break;
                    case FALSE: f++; break;
                    default: u++; break;
                }
            }
            Map<String, Object> stats = new LinkedHashMap<>();
            stats.put("TRUE", t);
            stats.put("FALSE", f);
            stats.put("UNKNOWN", u);
            m.put("triStats", stats);
            return m;
        }
    }

    public QueryResult execute() {
        List<Value[]> selected = new ArrayList<>();
        List<RowOutcome> outcomes = new ArrayList<>();
        List<Value[]> rows = table.rows();
        for (int i = 0; i < rows.size(); i++) {
            Value[] row = rows.get(i);
            TriBool tri = evaluator.evalLogic(filter, row);
            RowOutcome outcome = new RowOutcome(i, tri);
            outcomes.add(outcome);
            if (outcome.selected) {
                selected.add(row);
            }
        }
        return new QueryResult(selected, outcomes);
    }

    /** 构造执行计划（物理算子：TableScan -> Filter）。 */
    public Map<String, Object> explain() {
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("operator", "Filter");
        plan.put("predicate", filter.toJson());

        Map<String, Object> scan = new LinkedHashMap<>();
        scan.put("operator", "TableScan");
        scan.put("table", table.name());
        scan.put("columns", java.util.Arrays.asList(table.columnNames()));
        scan.put("estimatedRows", table.rows().size());
        plan.put("input", scan);

        return plan;
    }
}
