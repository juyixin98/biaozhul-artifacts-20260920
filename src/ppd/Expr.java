package ppd;

import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * 标量表达式（谓词）树。
 *
 * 支持：字面量、列引用、六种比较、AND/OR/NOT、IS [NOT] NULL、加减乘。
 * 求值遵循 SQL 三值逻辑（UNKNOWN 用 Java null 表示 Boolean 结果）。
 *
 * 每个谓词还能用抽象解释回答下推判定的关键问题：
 * {@link #rejectsNulls(Set)} —— 当给定列集合整体为 NULL（模拟外连接保留行），
 * 谓词是否绝不可能求值为 TRUE。
 */
public sealed interface Expr permits Expr.Lit, Expr.Ref, Expr.Cmp, Expr.Logic,
        Expr.Not, Expr.IsNull, Expr.Arith {

    /** 求值环境：按 限定名.列名 / 裸列名 取当前行的值。 */
    @FunctionalInterface
    interface Env {
        Object lookup(String qualifier, String name);
    }

    Object eval(Env env);

    /** 收集出现的全部列引用。 */
    default Set<RefKey> refs() {
        Set<RefKey> out = new LinkedHashSet<>();
        collectRefs(out);
        return out;
    }

    void collectRefs(Set<RefKey> out);

    /** 规范文本（可被 {@link ExprParser} 再次解析，用于计划 JSON 与测试）。 */
    String toSql();

    // ------------------------------------------------------------------
    // NULL 拒绝性分析（抽象解释）
    //
    // 外连接“保留行”的特征：NULL 补充侧的所有列同时为 NULL。
    // NULL 拒绝性测试：把目标列集合整体置为 NULL，其余列视为“某个非空的未知值”，
    // 求谓词在三值逻辑下的可能结果集合（T/F/U 三比特）。
    // ------------------------------------------------------------------

    record Outcome(boolean t, boolean f, boolean u) {
        static final Outcome T = new Outcome(true, false, false);
        static final Outcome F = new Outcome(false, true, false);
        static final Outcome U = new Outcome(false, false, true);
        static final Outcome TF = new Outcome(true, true, false);
        static final Outcome FU = new Outcome(false, true, true);

        Outcome or(Outcome o) {
            return new Outcome(t || o.t, f && o.f,
                    (u || o.u) && !(t || o.t));
        }

        Outcome and(Outcome o) {
            return new Outcome(t && o.t, f || o.f,
                    (u || o.u) && !(f || o.f));
        }

        Outcome not() { return new Outcome(f, t, u); }
    }

    /**
     * 谓词是否拒绝 NULL：把 {@code nulled} 中的列整体置为 NULL，其余列视为某个非空值
     * （使其参与的比较可真可假），若谓词在该情形下不可能求值为 TRUE，则返回 true。
     *
     * @param nulled 在“保留行”场景下被补 NULL 的列集合
     */
    default boolean rejectsNulls(Set<RefKey> nulled) {
        return !nullTestOutcome(nulled).t();
    }

    /**
     * NULL 测试下布尔表达式的可能结果集合。
     * 目标列 => 该值为 NULL；其他列 => 非空未知值（比较可 T 可 F，IS NULL => F）。
     */
    Outcome nullTestOutcome(Set<RefKey> nulled);

    /** 标量子表达式在 NULL 测试中是否必为 NULL。 */
    boolean isNullUnderTest(Set<RefKey> nulled);

    /** 引用是否落在被置 NULL 的集合中（裸列名按列名匹配集合内任一列）。 */
    static boolean refIsNulled(RefKey k, Set<RefKey> nulled) {
        if (nulled.contains(k)) return true;
        if (k.qualifier() == null) {
            for (RefKey x : nulled) {
                if (x.name().equals(k.name())) return true;
            }
        }
        return false;
    }

    // ------------------------------------------------------------------
    // 具体节点
    // ------------------------------------------------------------------

    /** 字面量：null / Long / Double / String / Boolean。 */
    record Lit(Object value) implements Expr {
        @Override
        public Object eval(Env env) { return value; }

        @Override
        public void collectRefs(Set<RefKey> out) { }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) { return value == null; }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            if (value == null) return Outcome.U;
            if (value instanceof Boolean b) return b ? Outcome.T : Outcome.F;
            return Outcome.TF; // 非布尔出现在布尔位置：保守视为可真可假
        }

        @Override
        public String toSql() { return litToSql(value); }

        static String litToSql(Object v) {
            if (v == null) return "NULL";
            if (v instanceof Boolean b) return b ? "TRUE" : "FALSE";
            if (v instanceof String s) {
                return "'" + s.replace("'", "''") + "'";
            }
            return v.toString();
        }
    }

    /** 列引用：t.col 或裸 col。 */
    record Ref(String qualifier, String name) implements Expr {
        @Override
        public Object eval(Env env) { return env.lookup(qualifier, name); }

        public RefKey key() { return new RefKey(qualifier, name); }

        @Override
        public void collectRefs(Set<RefKey> out) { out.add(key()); }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) {
            return refIsNulled(key(), n);
        }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            return isNullUnderTest(n) ? Outcome.U : Outcome.TF;
        }

        @Override
        public String toSql() {
            return qualifier == null ? name : qualifier + "." + name;
        }
    }

    enum CmpOp {
        EQ("="), NE("<>"), LT("<"), LE("<="), GT(">"), GE(">=");
        final String text;
        CmpOp(String t) { text = t; }
    }

    record Cmp(CmpOp op, Expr left, Expr right) implements Expr {
        @Override
        public Object eval(Env env) {
            Object a = left.eval(env);
            Object b = right.eval(env);
            return switch (op) {
                case EQ -> Values.eq(a, b);
                case NE -> {
                    Boolean r = Values.eq(a, b);
                    yield r == null ? null : !r;
                }
                default -> {
                    Integer c = Values.compare(a, b);
                    if (c == null) yield null;
                    yield switch (op) {
                        case LT -> c < 0;
                        case LE -> c <= 0;
                        case GT -> c > 0;
                        case GE -> c >= 0;
                        default -> throw new AssertionError();
                    };
                }
            };
        }

        @Override
        public void collectRefs(Set<RefKey> out) {
            left.collectRefs(out);
            right.collectRefs(out);
        }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) {
            return left.isNullUnderTest(n) || right.isNullUnderTest(n);
        }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            if (isNullUnderTest(n)) return Outcome.U;   // 任一操作数为 NULL => UNKNOWN
            return Outcome.TF;                          // 两侧都是非空未知值：可真可假
        }

        @Override
        public String toSql() {
            return "(" + left.toSql() + " " + op.text + " " + right.toSql() + ")";
        }
    }

    enum LogicOp { AND, OR }

    record Logic(LogicOp op, Expr left, Expr right) implements Expr {
        @Override
        public Object eval(Env env) {
            Boolean a = (Boolean) left.eval(env);
            Boolean b = (Boolean) right.eval(env);
            return op == LogicOp.AND ? Values.and(a, b) : Values.or(a, b);
        }

        @Override
        public void collectRefs(Set<RefKey> out) {
            left.collectRefs(out);
            right.collectRefs(out);
        }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) {
            boolean a = left.isNullUnderTest(n);
            boolean b = right.isNullUnderTest(n);
            return op == LogicOp.AND ? a && b : a || b;
        }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            Outcome a = left.nullTestOutcome(n);
            Outcome b = right.nullTestOutcome(n);
            return op == LogicOp.AND ? a.and(b) : a.or(b);
        }

        @Override
        public String toSql() {
            String opText = op == LogicOp.AND ? " AND " : " OR ";
            return "(" + left.toSql() + opText + right.toSql() + ")";
        }
    }

    record Not(Expr inner) implements Expr {
        @Override
        public Object eval(Env env) {
            return Values.not((Boolean) inner.eval(env));
        }

        @Override
        public void collectRefs(Set<RefKey> out) { inner.collectRefs(out); }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) { return inner.isNullUnderTest(n); }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            return inner.nullTestOutcome(n).not();
        }

        @Override
        public String toSql() { return "(NOT " + inner.toSql() + ")"; }
    }

    record IsNull(Expr inner, boolean negated) implements Expr {
        @Override
        public Object eval(Env env) {
            boolean isNull = inner.eval(env) == null;
            return negated != isNull;
        }

        @Override
        public void collectRefs(Set<RefKey> out) { inner.collectRefs(out); }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) { return false; } // IS NULL 永不为 NULL

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            if (inner.isNullUnderTest(n)) {
                // 内层必为 NULL：IS NULL => 恒 TRUE；IS NOT NULL => 恒 FALSE
                return negated ? Outcome.F : Outcome.T;
            }
            // 内层是非空未知值（该测试模型下它不会为 NULL）
            return negated ? Outcome.T : Outcome.F;
        }

        @Override
        public String toSql() {
            return "(" + inner.toSql() + (negated ? " IS NOT NULL)" : " IS NULL)");
        }
    }

    enum ArithOp { ADD, SUB, MUL }

    record Arith(ArithOp op, Expr left, Expr right) implements Expr {
        @Override
        public Object eval(Env env) {
            Object a = left.eval(env);
            Object b = right.eval(env);
            return switch (op) {
                case ADD -> Values.add(a, b);
                case SUB -> Values.subtract(a, b);
                case MUL -> Values.multiply(a, b);
            };
        }

        @Override
        public void collectRefs(Set<RefKey> out) {
            left.collectRefs(out);
            right.collectRefs(out);
        }

        @Override
        public boolean isNullUnderTest(Set<RefKey> n) {
            return left.isNullUnderTest(n) || right.isNullUnderTest(n);
        }

        @Override
        public Outcome nullTestOutcome(Set<RefKey> n) {
            return isNullUnderTest(n) ? Outcome.U : Outcome.TF;
        }

        @Override
        public String toSql() {
            String opText = switch (op) {
                case ADD -> "+";
                case SUB -> "-";
                case MUL -> "*";
            };
            return "(" + left.toSql() + " " + opText + " " + right.toSql() + ")";
        }
    }

    // ------------------------------------------------------------------
    // 通用变换辅助
    // ------------------------------------------------------------------

    /** 用映射替换列引用（投影下推时把输出列名换成底层来源列）。 */
    default Expr mapRefs(java.util.function.Function<Ref, Expr> f) {
        return ExprMapper.apply(this, f);
    }

    /** 合取拆分：AND 树拆为列表。 */
    static List<Expr> splitConjunction(Expr e) {
        List<Expr> out = new java.util.ArrayList<>();
        collectConjuncts(e, out);
        return out;
    }

    private static void collectConjuncts(Expr e, List<Expr> out) {
        if (e instanceof Logic(LogicOp op, Expr l, Expr r) && op == LogicOp.AND) {
            collectConjuncts(l, out);
            collectConjuncts(r, out);
        } else {
            out.add(e);
        }
    }

    /** 合取合并；空列表返回常量 TRUE。 */
    static Expr combineConjunction(List<Expr> es) {
        if (es.isEmpty()) return new Lit(Boolean.TRUE);
        Expr result = es.get(0);
        for (int i = 1; i < es.size(); i++) {
            result = new Logic(LogicOp.AND, result, es.get(i));
        }
        return result;
    }
}
