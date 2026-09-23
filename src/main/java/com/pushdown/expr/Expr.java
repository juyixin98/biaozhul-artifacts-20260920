package com.pushdown.expr;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Expression tree. Predicates use SQL three-valued logic: every boolean
 * position yields TRUE / FALSE / NULL(UNKNOWN); a Filter keeps a row only
 * when its predicate evaluates to TRUE.
 */
public sealed interface Expr {

    record Lit(Object value) implements Expr {}
    record Col(String table, String name) implements Expr {}
    record Bin(String op, Expr l, Expr r) implements Expr {}   // = != < <= > >= + - *
    record And(Expr l, Expr r) implements Expr {}
    record Or(Expr l, Expr r) implements Expr {}
    record Not(Expr e) implements Expr {}
    record IsNull(Expr e) implements Expr {}
    record IsNotNull(Expr e) implements Expr {}

    /** Columns referenced by the expression (provenance for pushdown decisions). */
    static Set<Col> columnsOf(Expr e) {
        Set<Col> out = new LinkedHashSet<>();
        collect(e, out);
        return out;
    }

    private static void collect(Expr e, Set<Col> out) {
        switch (e) {
            case Col c -> out.add(c);
            case Bin b -> { collect(b.l(), out); collect(b.r(), out); }
            case And a -> { collect(a.l(), out); collect(a.r(), out); }
            case Or o -> { collect(o.l(), out); collect(o.r(), out); }
            case Not n -> collect(n.e(), out);
            case IsNull i -> collect(i.e(), out);
            case IsNotNull i -> collect(i.e(), out);
            case Lit ignored -> {}
        }
    }

    /** Evaluate against a row aligned with {@code schema}. Booleans may come back as null (UNKNOWN). */
    static Object eval(Expr e, List<Col> schema, Object[] row) {
        return switch (e) {
            case Lit l -> l.value();
            case Col c -> {
                int i = schema.indexOf(c);
                if (i < 0) throw new IllegalArgumentException("unknown column " + c.table() + "." + c.name());
                yield row[i];
            }
            case Bin b -> evalBin(b, schema, row);
            case And a -> and(truth(eval(a.l(), schema, row)), truth(eval(a.r(), schema, row)));
            case Or o -> or(truth(eval(o.l(), schema, row)), truth(eval(o.r(), schema, row)));
            case Not n -> not(truth(eval(n.e(), schema, row)));
            case IsNull i -> eval(i.e(), schema, row) == null;
            case IsNotNull i -> eval(i.e(), schema, row) != null;
        };
    }

    static Boolean truth(Object v) {
        if (v == null) return null;
        if (v instanceof Boolean b) return b;
        throw new IllegalArgumentException("not a boolean value: " + v);
    }

    static Boolean and(Boolean a, Boolean b) {
        if (Boolean.FALSE.equals(a) || Boolean.FALSE.equals(b)) return Boolean.FALSE;
        if (a == null || b == null) return null;
        return a && b;
    }

    static Boolean or(Boolean a, Boolean b) {
        if (Boolean.TRUE.equals(a) || Boolean.TRUE.equals(b)) return Boolean.TRUE;
        if (a == null || b == null) return null;
        return a && b;
    }

    static Boolean not(Boolean a) { return a == null ? null : !a; }

    private static Object evalBin(Bin b, List<Col> schema, Object[] row) {
        Object l = eval(b.l(), schema, row);
        Object r = eval(b.r(), schema, row);
        switch (b.op()) {
            case "+", "-", "*" -> {
                if (l == null || r == null) return null;
                double x = num(l), y = num(r);
                double res = switch (b.op()) { case "+" -> x + y; case "-" -> x - y; default -> x * y; };
                if (l instanceof Long && r instanceof Long) return (long) res;
                return res;
            }
            default -> {
                if (l == null || r == null) return null;
                Integer cmp = compare(l, r);
                if (cmp == null) return null; // incomparable types -> UNKNOWN
                return switch (b.op()) {
                    case "=" -> cmp == 0;
                    case "!=" -> cmp != 0;
                    case "<" -> cmp < 0;
                    case "<=" -> cmp <= 0;
                    case ">" -> cmp > 0;
                    case ">=" -> cmp >= 0;
                    default -> throw new IllegalArgumentException("unknown operator " + b.op());
                };
            }
        }
    }

    private static double num(Object v) {
        if (v instanceof Number n) return n.doubleValue();
        throw new IllegalArgumentException("not a number: " + v);
    }

    private static Integer compare(Object a, Object b) {
        if (a instanceof Number x && b instanceof Number y) return Double.compare(x.doubleValue(), y.doubleValue());
        if (a instanceof String x && b instanceof String y) return x.compareTo(y);
        if (a instanceof Boolean x && b instanceof Boolean y) return x.compareTo(y);
        return null;
    }
}
