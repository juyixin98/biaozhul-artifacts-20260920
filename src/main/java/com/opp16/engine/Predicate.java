package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.ArrayList;
import java.util.List;

/**
 * WHERE predicate evaluated two ways from the same definition:
 * <ul>
 *   <li>{@link #evalBatch} - columnar batch mode returning bit masks with
 *       SQL three-valued logic (TRUE / FALSE / UNKNOWN). Only TRUE rows are
 *       pushed into the {@link SelectionVector}.</li>
 *   <li>{@link #evalRow} - scalar mode used by the row-at-a-time reference
 *       interpreter; returns {@code null} for UNKNOWN.</li>
 * </ul>
 *
 * <p>Batch result packs the tri-state into two masks over {@code len} positions:
 * positions in {@code trueMask} are TRUE, those in {@code unknownMask} are
 * UNKNOWN (typically NULL input), the rest are FALSE.</p>
 */
public abstract class Predicate {

    /** Validate column references against the table schema. */
    public abstract void bind(Table table);

    /** Scalar evaluation for the reference interpreter. null result == UNKNOWN. */
    public abstract Boolean evalRow(int row);

    /** Columnar batch evaluation over rows [base, base+len), len &lt;= 64. */
    public abstract TriMask evalBatch(int base, int len);

    /** Two 64-bit masks encoding a tri-state result over batch positions. */
    public static final class TriMask {
        public long trueMask;
        public long unknownMask;

        public TriMask(long trueMask, long unknownMask) {
            this.trueMask = trueMask;
            this.unknownMask = unknownMask;
        }

        static TriMask allFalse(int len) { return new TriMask(0L, 0L); }
    }

    protected static long validBits(int len) {
        return len == 64 ? ~0L : (1L << len) - 1L;
    }

    // ------------------------------------------------------------------
    // Comparison predicates (column <op> literal)
    // ------------------------------------------------------------------

    enum Cmp {
        EQ("="), NE("!="), LT("<"), LE("<="), GT(">"), GE(">=");
        final String text;
        Cmp(String text) { this.text = text; }

        static Cmp parse(String s) {
            for (Cmp c : values()) if (c.text.equals(s)) return c;
            if ("==".equals(s)) return EQ;
            if ("<>".equals(s)) return NE;
            throw new EngineException("unknown comparison operator: " + s);
        }

        boolean compareInt(long a, long b) {
            return switch (this) {
                case EQ -> a == b; case NE -> a != b;
                case LT -> a < b;  case LE -> a <= b;
                case GT -> a > b;  case GE -> a >= b;
            };
        }

        boolean compareStr(String a, String b) {
            int cmp = a.compareTo(b);
            return switch (this) {
                case EQ -> cmp == 0; case NE -> cmp != 0;
                case LT -> cmp < 0;  case LE -> cmp <= 0;
                case GT -> cmp > 0;  case GE -> cmp >= 0;
            };
        }
    }

    static final class Compare extends Predicate {
        final String columnName;
        final Cmp op;
        final Object literal; // Long or String
        Column column;

        Compare(String columnName, Cmp op, Object literal) {
            this.columnName = columnName;
            this.op = op;
            this.literal = literal;
        }

        @Override public void bind(Table table) {
            column = table.column(columnName);
            if (literal instanceof Long) {
                column.asInt();
            } else if (literal instanceof String) {
                column.asString();
            } else {
                throw new EngineException("unsupported literal type on column " + columnName);
            }
        }

        @Override public Boolean evalRow(int row) {
            if (column.isNullAt(row)) return null;
            if (column.type == ColumnType.INT) {
                return op.compareInt(column.asInt().getLong(row), (Long) literal);
            }
            return op.compareStr(column.asString().getString(row), (String) literal);
        }

        @Override public TriMask evalBatch(int base, int len) {
            if (column.type == ColumnType.INT) {
                Column.IntColumn ic = column.asInt();
                long present = ic.presentMask(base, len);
                long[] vs = ic.values();
                long b = (Long) literal;
                long t = 0L;
                long m = present;
                while (m != 0L) {
                    int p = Long.numberOfTrailingZeros(m);
                    if (op.compareInt(vs[base + p], b)) t |= 1L << p;
                    m &= m - 1L;
                }
                return new TriMask(t, validBits(len) & ~present);
            }
            Column.StringColumn sc = column.asString();
            long present = sc.nulls().presentMask(base, len);
            String[] vs = sc.values();
            String b = (String) literal;
            long t = 0L;
            long m = present;
            while (m != 0L) {
                int p = Long.numberOfTrailingZeros(m);
                if (op.compareStr(vs[base + p], b)) t |= 1L << p;
                m &= m - 1L;
            }
            long valid = validBits(len);
            return new TriMask(t, valid & ~present);
        }
    }

    // ------------------------------------------------------------------
    // BETWEEN (inclusive)
    // ------------------------------------------------------------------

    static final class Between extends Predicate {
        final String columnName;
        final long low, high;
        Column.IntColumn column;

        Between(String columnName, long low, long high) {
            this.columnName = columnName;
            this.low = low;
            this.high = high;
        }

        @Override public void bind(Table table) {
            column = table.column(columnName).asInt();
        }

        private boolean test(long v) { return v >= low && v <= high; }

        @Override public Boolean evalRow(int row) {
            if (column.isNullAt(row)) return null;
            return test(column.getLong(row));
        }

        @Override public TriMask evalBatch(int base, int len) {
            long present = column.presentMask(base, len);
            long[] vs = column.values();
            long t = 0L;
            long m = present;
            while (m != 0L) {
                int p = Long.numberOfTrailingZeros(m);
                if (test(vs[base + p])) t |= 1L << p;
                m &= m - 1L;
            }
            return new TriMask(t, validBits(len) & ~present);
        }
    }

    // ------------------------------------------------------------------
    // IS NULL / IS NOT NULL
    // ------------------------------------------------------------------

    static final class NullTest extends Predicate {
        final String columnName;
        final boolean wantNull;
        Column column;

        NullTest(String columnName, boolean wantNull) {
            this.columnName = columnName;
            this.wantNull = wantNull;
        }

        @Override public void bind(Table table) { column = table.column(columnName); }

        @Override public Boolean evalRow(int row) {
            return wantNull == column.isNullAt(row);
        }

        @Override public TriMask evalBatch(int base, int len) {
            long present = column.nulls().presentMask(base, len);
            long valid = validBits(len);
            return wantNull ? new TriMask(valid & ~present, 0L) : new TriMask(present, 0L);
        }
    }

    // ------------------------------------------------------------------
    // AND / OR / NOT with three-valued logic
    // ------------------------------------------------------------------

    static final class And extends Predicate {
        final Predicate left, right;
        And(Predicate l, Predicate r) { left = l; right = r; }

        @Override public void bind(Table t) { left.bind(t); right.bind(t); }
        @Override public Boolean evalRow(int row) {
            Boolean a = left.evalRow(row), b = right.evalRow(row);
            if (Boolean.FALSE.equals(a) || Boolean.FALSE.equals(b)) return false;
            if (a == null || b == null) return null;
            return true;
        }
        @Override public TriMask evalBatch(int base, int len) {
            TriMask a = left.evalBatch(base, len), b = right.evalBatch(base, len);
            long valid = validBits(len);
            long t = a.trueMask & b.trueMask;
            // unknown if one side unknown and the other is not false
            long u = ((a.unknownMask & (b.trueMask | b.unknownMask))
                    | (b.unknownMask & (a.trueMask | a.unknownMask))) & valid;
            return new TriMask(t, u);
        }
    }

    static final class Or extends Predicate {
        final Predicate left, right;
        Or(Predicate l, Predicate r) { left = l; right = r; }

        @Override public void bind(Table t) { left.bind(t); right.bind(t); }
        @Override public Boolean evalRow(int row) {
            Boolean a = left.evalRow(row), b = right.evalRow(row);
            if (Boolean.TRUE.equals(a) || Boolean.TRUE.equals(b)) return true;
            if (a == null || b == null) return null;
            return false;
        }
        @Override public TriMask evalBatch(int base, int len) {
            TriMask a = left.evalBatch(base, len), b = right.evalBatch(base, len);
            long valid = validBits(len);
            long t = a.trueMask | b.trueMask;
            // unknown if one side unknown and the other is not true
            long u = ((a.unknownMask & ~b.trueMask) | (b.unknownMask & ~a.trueMask)) & valid;
            return new TriMask(t, u);
        }
    }

    static final class Not extends Predicate {
        final Predicate child;
        Not(Predicate c) { child = c; }

        @Override public void bind(Table t) { child.bind(t); }
        @Override public Boolean evalRow(int row) {
            Boolean a = child.evalRow(row);
            return a == null ? null : !a;
        }
        @Override public TriMask evalBatch(int base, int len) {
            TriMask a = child.evalBatch(base, len);
            long valid = validBits(len);
            long t = valid & ~(a.trueMask | a.unknownMask); // FALSE of child
            return new TriMask(t, a.unknownMask);           // NOT(UNKNOWN)=UNKNOWN
        }
    }

    // ------------------------------------------------------------------
    // JSON construction
    // ------------------------------------------------------------------

    /** Parse a predicate node: {"column":..,"op":..,"value":..} or {"op":"and","args":[..]}. */
    public static Predicate fromJson(Json.Value node) {
        Json.Obj o = node.asObject();
        String op = o.getString("op");
        if (op == null) throw new EngineException("predicate node missing 'op'");
        switch (op.toLowerCase()) {
            case "and": return new And(children(o).get(0), children(o).get(1));
            case "or":  return new Or(children(o).get(0), children(o).get(1));
            case "not": {
                Json.Value arg = o.getOr("arg", o.getOr("args", Json.NULL));
                if (arg.isArray()) arg = arg.asArray().get(0);
                return new Not(fromJson(arg));
            }
            case "is_null":     return new NullTest(o.getString("column"), true);
            case "is_not_null": return new NullTest(o.getString("column"), false);
            case "between": {
                long lo = o.get("low").asLong();
                long hi = o.get("high").asLong();
                return new Between(o.getString("column"), lo, hi);
            }
            default: {
                Cmp cmp = Cmp.parse(op);
                String col = o.getString("column");
                if (col == null) throw new EngineException("comparison predicate missing 'column'");
                Json.Value v = o.get("value");
                Object literal;
                if (v.isString()) literal = v.asString();
                else if (v.isNumber()) literal = v.asLong();
                else if (v.isNull()) throw new EngineException(
                        "comparison with literal null is not supported; use is_null / is_not_null");
                else throw new EngineException("unsupported literal in predicate on " + col);
                return new Compare(col, cmp, literal);
            }
        }
    }

    private static List<Predicate> children(Json.Obj o) {
        Json.Arr args = o.get("args").asArray();
        if (args.size() != 2) {
            throw new EngineException("logic operator expects exactly 2 args, got " + args.size());
        }
        List<Predicate> ps = new ArrayList<>();
        for (Json.Value v : args.list) ps.add(fromJson(v));
        return ps;
    }
}
