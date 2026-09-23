package tvl.evaluator;

import tvl.expr.Expr;
import tvl.expr.Tri;

/**
 * 表达式求值器，实现 SQL 风格三值逻辑（3VL）。
 *
 * <p>关键语义：</p>
 * <ul>
 *   <li>NULL 参与任何比较结果为 UNKNOWN；NULL 参与算术结果为 NULL。</li>
 *   <li>{@code NOT UNKNOWN = UNKNOWN}。</li>
 *   <li>AND / OR 在结果已确定时短路：AND 左值为 FALSE 时不求右值；
 *       OR 左值为 TRUE 时不求右值。左值为 UNKNOWN 时结果仍取决于右值，
 *       因此必须继续求值（右值中的除零等错误仍会抛出）。</li>
 *   <li>{@code x IS NULL} 与 {@code x IS NOT NULL} 恒为 TRUE/FALSE，绝不返回 UNKNOWN。</li>
 *   <li>整数算术使用 {@link Math} 的精确运算：加/减/乘/一元负号溢出、除零均抛
 *       {@link EvalException}，错误位置定位到具体运算节点。</li>
 * </ul>
 *
 * 类型兼容性已由 {@code TypeChecker} 静态保证，求值期不再重复类型判断。
 */
public final class Evaluator {

    /** 求值标量表达式，返回 Long / String / Boolean 或 null。 */
    public Object evalScalar(Expr expr, RowLike row) {
        return switch (expr) {
            case Expr.Literal lit -> switch (lit.kind) {
                case INTEGER -> lit.value;       // Long
                case STRING -> lit.value;        // String
                case BOOLEAN -> lit.value;       // Boolean
                case NULL, UNKNOWN -> null;
            };
            case Expr.Column col -> row.get(col.name);
            case Expr.UnaryMinus um -> {
                Object v = evalScalar(um.operand, row);
                if (v == null) yield null;
                try {
                    yield Math.negateExact((Long) v);
                } catch (ArithmeticException ex) {
                    throw new EvalException("integer overflow in unary '-'", um.pos());
                }
            }
            case Expr.BinaryArith arith -> {
                Object l = evalScalar(arith.left, row);
                if (l == null) yield null; // SQL: NULL 参与算术 => NULL（右值不需求值）
                Object r = evalScalar(arith.right, row);
                if (r == null) yield null;
                yield evalArith(arith, (Long) l, (Long) r);
            }
            // 布尔语境节点不应走到标量求值
            case Expr.Compare cmp -> triToNullableBoolean(evalCompare(cmp, row));
            case Expr.IsNull isn -> triToNullableBoolean(evalIsNull(isn, row));
            case Expr.Not not -> triToNullableBoolean(evalNot(not, row));
            case Expr.Logical log -> triToNullableBoolean(evalLogical(log, row));
        };
    }

    private static Boolean triToNullableBoolean(Tri tri) {
        return switch (tri) {
            case TRUE -> Boolean.TRUE;
            case FALSE -> Boolean.FALSE;
            case UNKNOWN -> null;
        };
    }

    /** 求值布尔（谓词）表达式，返回三值逻辑真值。 */
    public Tri evalPredicate(Expr expr, RowLike row) {
        return switch (expr) {
            case Expr.Literal lit -> switch (lit.kind) {
                case BOOLEAN -> Tri.fromBoolean((Boolean) lit.value);
                case NULL, UNKNOWN -> Tri.UNKNOWN;
                default -> throw new EvalException("non-boolean literal used as predicate",
                        lit.pos());
            };
            case Expr.Column col -> {
                Object v = row.get(col.name);
                if (v == null) yield Tri.UNKNOWN;
                yield Tri.fromBoolean((Boolean) v);
            }
            case Expr.Compare cmp -> evalCompare(cmp, row);
            case Expr.IsNull isn -> evalIsNull(isn, row);
            case Expr.Not not -> evalNot(not, row);
            case Expr.Logical log -> evalLogical(log, row);
            default -> throw new EvalException("non-boolean expression used as predicate",
                    expr.pos());
        };
    }

    // ---------------------------------------------------------------------

    private Long evalArith(Expr.BinaryArith arith, long l, long r) {
        try {
            return switch (arith.op) {
                case ADD -> Math.addExact(l, r);
                case SUB -> Math.subtractExact(l, r);
                case MUL -> Math.multiplyExact(l, r);
                case DIV -> {
                    if (r == 0L) {
                        throw new ArithmeticException("division by zero");
                    }
                    if (l == Long.MIN_VALUE && r == -1L) {
                        // Java 原生的 / 对该情况静默溢出，这里按需求显式报错
                        throw new ArithmeticException("integer overflow");
                    }
                    yield l / r;
                }
            };
        } catch (ArithmeticException ex) {
            String detail = ex.getMessage() != null && ex.getMessage().contains("division")
                    ? "division by zero" : "integer overflow";
            throw new EvalException(detail + " in '" + arith.op.symbol + "'", arith.pos());
        }
    }

    private Tri evalCompare(Expr.Compare cmp, RowLike row) {
        Object l = evalScalar(cmp.left, row);
        if (l == null) return Tri.UNKNOWN;
        Object r = evalScalar(cmp.right, row);
        if (r == null) return Tri.UNKNOWN;

        int cmpResult;
        if (l instanceof Long ll && r instanceof Long rr) {
            cmpResult = Long.compare(ll, rr);
        } else if (l instanceof String ls && r instanceof String rs) {
            cmpResult = ls.compareTo(rs);
        } else if (l instanceof Boolean lb && r instanceof Boolean rb) {
            // 仅 = / <> 可用于布尔（排序比较已在类型检查期拒绝）
            cmpResult = Boolean.compare(lb, rb);
        } else {
            throw new EvalException(
                    "runtime type mismatch in comparison", cmp.pos());
        }

        boolean result = switch (cmp.op) {
            case EQ -> cmpResult == 0;
            case NE -> cmpResult != 0;
            case LT -> cmpResult < 0;
            case LE -> cmpResult <= 0;
            case GT -> cmpResult > 0;
            case GE -> cmpResult >= 0;
        };
        return Tri.fromBoolean(result);
    }

    private Tri evalIsNull(Expr.IsNull isn, RowLike row) {
        Object v = evalScalar(isn.operand, row);
        boolean isNull = v == null;
        return Tri.fromBoolean(isn.negated != isNull);
    }

    private Tri evalNot(Expr.Not not, RowLike row) {
        Tri v = evalPredicate(not.operand, row);
        return switch (v) {
            case TRUE -> Tri.FALSE;
            case FALSE -> Tri.TRUE;
            case UNKNOWN -> Tri.UNKNOWN;
        };
    }

    private Tri evalLogical(Expr.Logical log, RowLike row) {
        Tri l = evalPredicate(log.left, row);
        if (log.op == Expr.LogicalOp.AND) {
            // 短路：左值 FALSE 已决定结果，右值绝不求值
            if (l == Tri.FALSE) return Tri.FALSE;
            Tri r = evalPredicate(log.right, row);
            return andTruthTable(l, r);
        } else {
            // 短路：左值 TRUE 已决定结果，右值绝不求值
            if (l == Tri.TRUE) return Tri.TRUE;
            Tri r = evalPredicate(log.right, row);
            return orTruthTable(l, r);
        }
    }

    /** AND 三值真值表（此时左值只可能为 TRUE 或 UNKNOWN）。 */
    static Tri andTruthTable(Tri l, Tri r) {
        if (l == Tri.FALSE || r == Tri.FALSE) return Tri.FALSE;
        if (l == Tri.TRUE && r == Tri.TRUE) return Tri.TRUE;
        return Tri.UNKNOWN;
    }

    /** OR 三值真值表（此时左值只可能为 FALSE 或 UNKNOWN）。 */
    static Tri orTruthTable(Tri l, Tri r) {
        if (l == Tri.TRUE || r == Tri.TRUE) return Tri.TRUE;
        if (l == Tri.FALSE && r == Tri.FALSE) return Tri.FALSE;
        return Tri.UNKNOWN;
    }
}
