package tvl.eval;

import java.util.List;

import tvl.core.DataType;
import tvl.core.EvalException;
import tvl.core.TriBool;
import tvl.core.Value;
import tvl.parser.BinaryExpr;
import tvl.parser.ColumnExpr;
import tvl.parser.Expr;
import tvl.parser.IsNullExpr;
import tvl.parser.LiteralExpr;
import tvl.parser.NotExpr;
import tvl.parser.UnaryMinusExpr;

/**
 * 表达式求值器：对一行数据求一个已通过类型检查的 AST。
 *
 * 两条求值路径：
 *  - {@link #eval} 求“值”，返回 {@link Value}（可以是任意类型，含 NULL）。
 *  - {@link #evalLogic} 求“逻辑条件”，返回 {@link TriBool}。
 *    布尔值 NULL 在逻辑语境下解释为 UNKNOWN（SQL 3VL）。
 *
 * AND/OR 使用 Kleene 三值逻辑并真正短路：未选择的分支中的
 * 除零、溢出都不会发生（短路测试的核心保证）。
 */
public final class Evaluator {

    private final String[] columns;

    public Evaluator(List<String> columns) {
        this(columns.toArray(new String[0]));
    }

    public Evaluator(String[] columns) {
        this.columns = columns.clone();
    }

    /** WHERE 过滤：只选中结果为 TRUE 的行（FALSE、UNKNOWN 均不选中）。 */
    public boolean selected(Expr e, Value[] row) {
        return evalLogic(e, row) == TriBool.TRUE;
    }

    /** 把逻辑表达式求值为三值。 */
    public TriBool evalLogic(Expr e, Value[] row) {
        if (e instanceof NotExpr) {
            NotExpr n = (NotExpr) e;
            return evalLogic(n.operand, row).not();
        }
        if (e instanceof BinaryExpr) {
            BinaryExpr b = (BinaryExpr) e;
            if (b.op.name().equals("AND")) {
                TriBool l = evalLogic(b.left, row);
                if (l == TriBool.FALSE) {
                    return TriBool.FALSE; // 短路：右分支不求值
                }
                return l.and(evalLogic(b.right, row));
            }
            if (b.op.name().equals("OR")) {
                TriBool l = evalLogic(b.left, row);
                if (l == TriBool.TRUE) {
                    return TriBool.TRUE; // 短路：右分支不求值
                }
                return l.or(evalLogic(b.right, row));
            }
        }
        if (e instanceof IsNullExpr) {
            IsNullExpr i = (IsNullExpr) e;
            boolean isNull = eval(i.operand, row).isNull();
            TriBool tb = isNull ? TriBool.TRUE : TriBool.FALSE;
            return i.negated ? tb.not() : tb;
        }
        // 比较表达式 / 布尔列 / 布尔字面量：NULL -> UNKNOWN
        Value v = eval(e, row);
        if (v.isNull()) {
            return TriBool.UNKNOWN;
        }
        return v.bool() ? TriBool.TRUE : TriBool.FALSE;
    }

    /** 对表达式求值，得到通用值。 */
    public Value eval(Expr e, Value[] row) {
        if (e instanceof LiteralExpr) {
            return evalLiteral((LiteralExpr) e);
        }
        if (e instanceof ColumnExpr) {
            return evalColumn((ColumnExpr) e, row);
        }
        if (e instanceof UnaryMinusExpr) {
            UnaryMinusExpr u = (UnaryMinusExpr) e;
            Value v = eval(u.operand, row);
            if (v.isNull()) {
                return Value.NULL;
            }
            return Value.ofInteger(negateExact(v.integer()));
        }
        if (e instanceof NotExpr) {
            // 值语境下求 NOT：UNKNOWN 仍以 NULL 布尔表示
            return triToValue(evalLogic(e, row));
        }
        if (e instanceof IsNullExpr) {
            IsNullExpr i = (IsNullExpr) e;
            boolean isNull = eval(i.operand, row).isNull();
            return Value.ofBoolean(i.negated ? !isNull : isNull);
        }
        if (e instanceof BinaryExpr) {
            return evalBinary((BinaryExpr) e, row);
        }
        throw new IllegalStateException("未知表达式节点：" + e.getClass());
    }

    private Value evalLiteral(LiteralExpr e) {
        switch (e.kind) {
            case INTEGER: return Value.ofInteger((Long) e.value);
            case STRING:  return Value.ofString((String) e.value);
            case BOOLEAN: return Value.ofBoolean((Boolean) e.value);
            case NULL:    return Value.NULL;
            default: throw new IllegalStateException();
        }
    }

    private Value evalColumn(ColumnExpr e, Value[] row) {
        for (int i = 0; i < columns.length; i++) {
            if (columns[i].equals(e.name)) {
                return row[i];
            }
        }
        // 类型检查阶段已保证列存在
        throw new EvalException("UNKNOWN_COLUMN", "未知列：" + e.name);
    }

    private Value evalBinary(BinaryExpr e, Value[] row) {
        // AND / OR：走三值逻辑，UNKNOWN 表示为布尔 NULL
        if (e.isLogic()) {
            return triToValue(evalLogic(e, row));
        }

        Value l = eval(e.left, row);
        Value r = eval(e.right, row);

        if (e.isArithmetic()) {
            if (l.isNull() || r.isNull()) {
                return Value.NULL;
            }
            return Value.ofInteger(arith(e, l.integer(), r.integer()));
        }

        // 比较：任一为 NULL -> UNKNOWN（布尔 NULL）
        if (l.isNull() || r.isNull()) {
            return Value.NULL;
        }
        return Value.ofBoolean(compare(e, l, r));
    }

    private static Value triToValue(TriBool t) {
        return t == TriBool.UNKNOWN ? Value.NULL : Value.ofBoolean(t == TriBool.TRUE);
    }

    private long arith(BinaryExpr e, long a, long b) {
        try {
            switch (e.op) {
                case PLUS:  return Math.addExact(a, b);
                case MINUS: return Math.subtractExact(a, b);
                case STAR:  return Math.multiplyExact(a, b);
                case SLASH:
                    if (b == 0L) {
                        throw new EvalException("DIVISION_BY_ZERO",
                                "整数除以零（被除数 " + a + "）");
                    }
                    if (a == Long.MIN_VALUE && b == -1L) {
                        throw new EvalException("INTEGER_OVERFLOW",
                                "整数除法溢出：" + a + " / " + b
                                        + " 超出 64 位有符号整数范围");
                    }
                    // Java long / long 向零截断，符合 SQL 整除语义
                    return a / b;
                default:
                    throw new IllegalStateException();
            }
        } catch (ArithmeticException ex) {
            // Math.*Exact 以 ArithmeticException 报告溢出，转换为明确错误码
            throw new EvalException("INTEGER_OVERFLOW",
                    "整数运算溢出：" + a + " " + e.opText + " " + b
                            + " 的结果超出 64 位有符号整数范围（"
                            + Long.MIN_VALUE + " ~ " + Long.MAX_VALUE + "）");
        }
    }

    /** 通用比较入口，按两侧类型分派（此时均非 NULL 且类型已校验一致）。 */
    private boolean compare(BinaryExpr e, Value l, Value r) {
        int cmp;
        if (l.type() == DataType.STRING) {
            cmp = l.string().compareTo(r.string());
        } else {
            cmp = Long.compare(l.integer(), r.integer());
        }
        switch (e.op) {
            case EQ: return cmp == 0;
            case NE: return cmp != 0;
            case LT: return cmp < 0;
            case LE: return cmp <= 0;
            case GT: return cmp > 0;
            case GE: return cmp >= 0;
            default: throw new IllegalStateException();
        }
    }

    // ---------- 一元负号溢出检测 ----------

    private static long negateExact(long a) {
        try {
            return Math.negateExact(a);
        } catch (ArithmeticException ex) {
            throw new EvalException("INTEGER_OVERFLOW",
                    "一元负号导致整数溢出：-" + a + " 超出 64 位有符号整数范围");
        }
    }
}
