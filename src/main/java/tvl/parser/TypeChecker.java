package tvl.parser;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.CompileException;
import tvl.core.DataType;

/**
 * 语义分析 / 类型检查器。
 *
 * 对 AST 做一次遍历：
 *  - 解析列名（列不存在报 UNKNOWN_COLUMN，位置指向该标识符）
 *  - 校验每个操作符两侧的操作数类型
 *  - 回填每个节点的 resolvedType
 *
 * NULL 字面量是“未定型”的（DataType.NULL），与任何类型运算都兼容；
 * 一旦参与具体运算，结果类型由另一侧/操作符决定。
 */
public final class TypeChecker {

    /** 列名 -> 类型（列名大小写敏感）。 */
    private final Map<String, DataType> schema;

    public TypeChecker(Map<String, DataType> schema) {
        this.schema = new LinkedHashMap<>(schema);
    }

    /** 无表场景（纯表达式）下使用：仅允许字面量表达式。 */
    public static void checkStandalone(Expr e) {
        new TypeChecker(new LinkedHashMap<>()).check(e);
    }

    public DataType check(Expr e) {
        if (e instanceof LiteralExpr) {
            return checkLiteral((LiteralExpr) e);
        }
        if (e instanceof ColumnExpr) {
            return checkColumn((ColumnExpr) e);
        }
        if (e instanceof UnaryMinusExpr) {
            return checkUnaryMinus((UnaryMinusExpr) e);
        }
        if (e instanceof NotExpr) {
            return checkNot((NotExpr) e);
        }
        if (e instanceof IsNullExpr) {
            return checkIsNull((IsNullExpr) e);
        }
        if (e instanceof BinaryExpr) {
            return checkBinary((BinaryExpr) e);
        }
        throw new IllegalStateException("未知表达式节点：" + e.getClass());
    }

    private DataType checkLiteral(LiteralExpr e) {
        switch (e.kind) {
            case INTEGER: e.resolvedType = DataType.INTEGER; break;
            case STRING:  e.resolvedType = DataType.STRING;  break;
            case BOOLEAN: e.resolvedType = DataType.BOOLEAN; break;
            case NULL:    e.resolvedType = DataType.NULL;    break;
        }
        return e.resolvedType;
    }

    private DataType checkColumn(ColumnExpr e) {
        DataType t = schema.get(e.name);
        if (t == null) {
            throw new CompileException("UNKNOWN_COLUMN",
                    "未知列名 '" + e.name + "'；可用列："
                            + (schema.isEmpty() ? "(无)" : schema.keySet()),
                    e.pos(), e.length());
        }
        e.resolvedType = t;
        return t;
    }

    private DataType checkUnaryMinus(UnaryMinusExpr e) {
        DataType t = check(e.operand);
        requireIntegerOrNull(t, "一元负号 '-'", e.operand.pos(),
                e.operand.length());
        e.resolvedType = DataType.INTEGER;
        return DataType.INTEGER;
    }

    private DataType checkNot(NotExpr e) {
        DataType t = check(e.operand);
        requireBooleanOrNull(t, "NOT", e.operand.pos(), e.operand.length());
        e.resolvedType = DataType.BOOLEAN;
        return DataType.BOOLEAN;
    }

    private DataType checkIsNull(IsNullExpr e) {
        // IS NULL 的操作数可以是任意类型
        check(e.operand);
        e.resolvedType = DataType.BOOLEAN;
        return DataType.BOOLEAN;
    }

    private DataType checkBinary(BinaryExpr e) {
        DataType lt = check(e.left);
        DataType rt = check(e.right);

        if (e.isArithmetic()) {
            // 二元算术的类型错误统一指向操作符，便于在表达式中定位
            requireType(lt, DataType.INTEGER, "算术运算 '" + e.opText
                    + "' 要求 INTEGER 操作数，左侧为 " + typeName(lt), e);
            requireType(rt, DataType.INTEGER, "算术运算 '" + e.opText
                    + "' 要求 INTEGER 操作数，右侧为 " + typeName(rt), e);
            e.resolvedType = DataType.INTEGER;
            return DataType.INTEGER;
        }

        if (e.isComparison()) {
            checkComparisonTypes(e, lt, rt);
            e.resolvedType = DataType.BOOLEAN;
            return DataType.BOOLEAN;
        }

        // AND / OR：类型错误同样指向逻辑操作符
        requireType(lt, DataType.BOOLEAN, "'" + e.opText
                + "' 要求 BOOLEAN 操作数，左侧为 " + typeName(lt), e);
        requireType(rt, DataType.BOOLEAN, "'" + e.opText
                + "' 要求 BOOLEAN 操作数，右侧为 " + typeName(rt), e);
        e.resolvedType = DataType.BOOLEAN;
        return DataType.BOOLEAN;
    }

    /** 二元操作数类型校验：NULL 未定型永远兼容，否则必须为 expected；错误指向操作符。 */
    private void requireType(DataType actual, DataType expected, String message,
                             BinaryExpr e) {
        if (actual != expected && actual != DataType.NULL) {
            throw mismatch(message, e);
        }
    }

    private void checkComparisonTypes(BinaryExpr e, DataType lt, DataType rt) {
        boolean ordering = e.op != TokenType.EQ && e.op != TokenType.NE;

        String opName = describeOp(e.op);
        // 排序比较不接受布尔
        if (ordering) {
            if (lt != DataType.NULL && !lt.isOrderable()) {
                throw mismatch(opName + " 要求整数或字符串操作数，左侧实际类型为 "
                        + typeName(lt), e);
            }
            if (rt != DataType.NULL && !rt.isOrderable()) {
                throw mismatch(opName + " 要求整数或字符串操作数，右侧实际类型为 "
                        + typeName(rt), e);
            }
        } else {
            if (lt != DataType.NULL && rt != DataType.NULL && lt != rt) {
                throw mismatch(opName + " 两侧类型必须一致（左侧 "
                        + typeName(lt) + "，右侧 " + typeName(rt) + "）", e);
            }
            // 单边 NULL 永远兼容
            return;
        }
        // 排序比较：两侧都非 NULL 时必须同类型
        if (lt != DataType.NULL && rt != DataType.NULL && lt != rt) {
            throw mismatch(opName + " 两侧类型必须一致（左侧 "
                    + typeName(lt) + "，右侧 " + typeName(rt) + "）", e);
        }
    }

    private void requireIntegerOrNull(DataType t, String what,
                                      tvl.core.Pos pos, int len) {
        if (t != DataType.INTEGER && t != DataType.NULL) {
            throw new CompileException("TYPE_MISMATCH",
                    what + " 要求 INTEGER 类型，实际为 " + typeName(t), pos, len);
        }
    }

    private void requireBooleanOrNull(DataType t, String what,
                                      tvl.core.Pos pos, int len) {
        if (t != DataType.BOOLEAN && t != DataType.NULL) {
            throw new CompileException("TYPE_MISMATCH",
                    what + " 要求 BOOLEAN 类型，实际为 " + typeName(t), pos, len);
        }
    }

    private CompileException mismatch(String message, BinaryExpr e) {
        return new CompileException("TYPE_MISMATCH", message, e.opPos, e.opLength);
    }

    private static String typeName(DataType t) {
        return t.name();
    }

    private static String describeOp(TokenType op) {
        switch (op) {
            case EQ: return "'='";
            case NE: return "'<>'";
            case LT: return "'<'";
            case LE: return "'<='";
            case GT: return "'>'";
            case GE: return "'>='";
            default: return op.name();
        }
    }

    public static List<String> orderedColumnNames(Map<String, DataType> schema) {
        return new java.util.ArrayList<>(schema.keySet());
    }
}
