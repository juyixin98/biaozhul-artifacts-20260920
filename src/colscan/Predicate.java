package colscan;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Filter predicate with SQL-style three-valued logic:
 * any comparison against NULL yields UNKNOWN, which filters the row out
 * (NULL is never equal to 0 or to any other value).
 *
 * Leaf JSON:    {"column":"price", "op":"&gt;=", "value":10}
 * Null tests:   {"column":"price", "op":"IS NULL"} / "IS NOT NULL"
 * Boolean:      {"op":"AND", "predicates":[...]}  (OR / NOT likewise)
 */
public abstract class Predicate {

    public abstract boolean test(RowAccessor row);

    /** Columns referenced; used to decide which shard statistics matter. */
    public abstract List<String> referencedColumns();

    public static Predicate parse(Object json) {
        if (!(json instanceof Map)) {
            throw new IllegalArgumentException("predicate must be an object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) json;
        String op = (String) m.get("op");
        if (op == null) {
            throw new IllegalArgumentException("predicate missing 'op'");
        }
        switch (op) {
            case "AND":
            case "OR": {
                List<Object> raw = asList(m.get("predicates"));
                List<Predicate> children = new ArrayList<>(raw.size());
                for (Object o : raw) children.add(parse(o));
                return new BoolPredicate(op, children);
            }
            case "NOT":
                return new NotPredicate(parse(m.get("predicate")));
            case "IS NULL":
                return new NullTest((String) requireColumn(m), true);
            case "IS NOT NULL":
                return new NullTest((String) requireColumn(m), false);
            default:
                return new Comparison((String) requireColumn(m), op, Json.toLong(m.get("value")));
        }
    }

    private static Object requireColumn(Map<String, Object> m) {
        Object c = m.get("column");
        if (!(c instanceof String)) {
            throw new IllegalArgumentException("predicate missing string 'column'");
        }
        return c;
    }

    @SuppressWarnings("unchecked")
    private static List<Object> asList(Object o) {
        if (!(o instanceof List)) {
            throw new IllegalArgumentException("expected 'predicates' array");
        }
        return (List<Object>) o;
    }

    /** Row access by column name; NULL cells come back as {@link Value#NULL}. */
    public interface RowAccessor {
        Value get(String column);
    }

    static final class Comparison extends Predicate {
        final String column;
        final String op;
        final long value;

        Comparison(String column, String op, Long value) {
            this.column = column;
            this.op = op;
            if (value == null) {
                throw new IllegalArgumentException("comparison needs integer 'value'");
            }
            this.value = value;
        }

        @Override
        public boolean test(RowAccessor row) {
            return match(row.get(column));
        }

        boolean match(Value v) {
            if (!v.present) return false; // NULL comparisons are UNKNOWN
            long x = v.longValue;
            switch (op) {
                case "=": case "==": case "EQ": return x == value;
                case "!=": case "<>": case "NE": return x != value;
                case "<": case "LT": return x < value;
                case "<=": case "LE": return x <= value;
                case ">": case "GT": return x > value;
                case ">=": case "GE": return x >= value;
                default: throw new IllegalArgumentException("unknown operator: " + op);
            }
        }

        @Override
        public List<String> referencedColumns() {
            return List.of(column);
        }
    }

    static final class NullTest extends Predicate {
        final String column;
        final boolean wantNull;

        NullTest(String column, boolean wantNull) {
            this.column = column;
            this.wantNull = wantNull;
        }

        @Override
        public boolean test(RowAccessor row) {
            return match(row.get(column));
        }

        boolean match(Value v) {
            return v.present != wantNull;
        }

        @Override
        public List<String> referencedColumns() {
            return List.of(column);
        }
    }

    static final class BoolPredicate extends Predicate {
        final String op;
        final List<Predicate> children;

        BoolPredicate(String op, List<Predicate> children) {
            this.op = op;
            this.children = children;
        }

        @Override
        public boolean test(RowAccessor row) {
            if (op.equals("AND")) {
                for (Predicate c : children) if (!c.test(row)) return false;
                return true;
            }
            for (Predicate c : children) if (c.test(row)) return true;
            return false;
        }

        @Override
        public List<String> referencedColumns() {
            List<String> out = new ArrayList<>();
            for (Predicate c : children) out.addAll(c.referencedColumns());
            return out;
        }
    }

    static final class NotPredicate extends Predicate {
        final Predicate child;

        NotPredicate(Predicate child) {
            this.child = child;
        }

        @Override
        public boolean test(RowAccessor row) {
            return !child.test(row);
        }

        @Override
        public List<String> referencedColumns() {
            return child.referencedColumns();
        }
    }
}
