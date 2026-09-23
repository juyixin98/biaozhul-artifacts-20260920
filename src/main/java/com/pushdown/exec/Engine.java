package com.pushdown.exec;

import com.pushdown.expr.Expr;
import com.pushdown.expr.Expr.Col;
import com.pushdown.plan.Plan;
import com.pushdown.plan.Plan.Filter;
import com.pushdown.plan.Plan.Join;
import com.pushdown.plan.Plan.Project;
import com.pushdown.plan.Plan.Scan;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * In-memory executor. All core computation (filter / project / nested-loop
 * inner & left join, three-valued predicate evaluation) is implemented here;
 * no SQL engine is involved.
 */
public final class Engine {

    public record Table(List<String> columns, List<Object[]> rows) {}
    public record Output(List<String> schema, List<List<Object>> rows) {}

    private Engine() {}

    /** Output schema of a plan node as qualified columns (table, name). */
    public static List<Col> schemaOf(Plan p, Map<String, Table> catalog) {
        return switch (p) {
            case Scan s -> {
                Table t = catalog.get(s.table());
                if (t == null) throw new IllegalArgumentException("unknown table: " + s.table());
                List<Col> out = new ArrayList<>();
                for (String c : t.columns()) out.add(new Col(s.table(), c));
                yield out;
            }
            case Filter f -> schemaOf(f.child(), catalog);
            case Project pr -> {
                List<Col> out = new ArrayList<>();
                for (Plan.Item it : pr.items()) out.add(new Col("", it.alias()));
                yield out;
            }
            case Join j -> {
                List<Col> out = new ArrayList<>(schemaOf(j.left(), catalog));
                out.addAll(schemaOf(j.right(), catalog));
                yield out;
            }
        };
    }

    public static List<Object[]> run(Plan p, Map<String, Table> catalog) {
        return switch (p) {
            case Scan s -> {
                Table t = catalog.get(s.table());
                if (t == null) throw new IllegalArgumentException("unknown table: " + s.table());
                yield new ArrayList<>(t.rows());
            }
            case Filter f -> {
                List<Col> schema = schemaOf(f.child(), catalog);
                List<Object[]> out = new ArrayList<>();
                for (Object[] row : run(f.child(), catalog)) {
                    if (Boolean.TRUE.equals(Expr.eval(f.pred(), schema, row))) out.add(row);
                }
                yield out;
            }
            case Project pr -> {
                List<Col> schema = schemaOf(pr.child(), catalog);
                List<Object[]> out = new ArrayList<>();
                for (Object[] row : run(pr.child(), catalog)) {
                    Object[] nr = new Object[pr.items().size()];
                    for (int i = 0; i < nr.length; i++)
                        nr[i] = Expr.eval(pr.items().get(i).expr(), schema, row);
                    out.add(nr);
                }
                yield out;
            }
            case Join j -> {
                List<Object[]> left = run(j.left(), catalog);
                List<Object[]> right = run(j.right(), catalog);
                List<Col> js = new ArrayList<>(schemaOf(j.left(), catalog));
                js.addAll(schemaOf(j.right(), catalog));
                int rightWidth = js.size() - schemaOf(j.left(), catalog).size();
                List<Object[]> out = new ArrayList<>();
                for (Object[] l : left) {
                    boolean matched = false;
                    for (Object[] r : right) {
                        Object[] joined = concat(l, r);
                        if (Boolean.TRUE.equals(Expr.eval(j.on(), js, joined))) {
                            out.add(joined);
                            matched = true;
                        }
                    }
                    // LEFT JOIN: preserve unmatched left rows, pad right side with NULLs.
                    if (!matched && j.type() == Plan.JoinType.LEFT) {
                        out.add(concat(l, new Object[rightWidth]));
                    }
                }
                yield out;
            }
        };
    }

    public static Output execute(Plan p, Map<String, Table> catalog) {
        List<Col> schema = schemaOf(p, catalog);
        List<String> names = new ArrayList<>();
        for (Col c : schema) names.add(c.table().isEmpty() ? c.name() : c.table() + "." + c.name());
        List<List<Object>> rows = new ArrayList<>();
        for (Object[] r : run(p, catalog)) rows.add(Arrays.asList(r));
        return new Output(names, rows);
    }

    private static Object[] concat(Object[] a, Object[] b) {
        Object[] out = Arrays.copyOf(a, a.length + b.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }
}
