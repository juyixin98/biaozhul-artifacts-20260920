package tvl.analyzer;

import tvl.expr.Expr;

/**
 * 表达式类型检查器（AST 访问器）。
 *
 * 规则（SQL 风格）：
 * <ul>
 *   <li>算术运算两侧必须都是 INTEGER（NULL 字面量除外，其类型为 NULL 且会传播）；
 *       任何一侧为非数值类型即类型错误。</li>
 *   <li>=、&lt;&gt; 两侧必须同类型族（整数与整数、字符串与字符串、布尔与布尔）；
 *       与 NULL 字面量比较始终合法（结果恒为 UNKNOWN）。</li>
 *   <li>&lt;、&lt;=、&gt;、&gt;= 只支持 INTEGER 与 STRING（可排序类型），
 *       不允许对 BOOLEAN 排序。</li>
 *   <li>AND / OR / NOT 的操作数必须是布尔类型表达式（比较、谓词、布尔列/字面量）。</li>
 *   <li>IS [NOT] NULL 可作用于任意类型，结果恒为 BOOLEAN。</li>
 *   <li>未知列名报错。</li>
 * </ul>
 * 类型检查不依赖具体数据行，仅依赖表结构。
 */
public final class TypeChecker {

    private final Schema schema;

    public TypeChecker(Schema schema) {
        this.schema = schema;
    }

    /** 检查表达式，返回其静态类型；WHERE 表达式合法时结果可能为 BOOLEAN 或 NULL。 */
    public DataType check(Expr expr) {
        return switch (expr) {
            case Expr.Literal lit -> switch (lit.kind) {
                case INTEGER -> DataType.INTEGER;
                case STRING -> DataType.STRING;
                case BOOLEAN, UNKNOWN -> DataType.BOOLEAN;
                case NULL -> DataType.NULL;
            };
            case Expr.Column col -> {
                DataType t = schema.typeOf(col.name);
                if (t == null) {
                    throw new TypeException(
                            "unknown column '" + col.name + "'", col.pos());
                }
                yield t;
            }
            case Expr.UnaryMinus um -> {
                DataType t = check(um.operand);
                requireNumericOrNull(t, "unary '-'", um.pos());
                yield t == DataType.NULL ? DataType.NULL : DataType.INTEGER;
            }
            case Expr.BinaryArith arith -> {
                DataType l = check(arith.left);
                DataType r = check(arith.right);
                requireNumericOrNull(l, "arithmetic '" + arith.op.symbol + "'", arith.left.pos());
                requireNumericOrNull(r, "arithmetic '" + arith.op.symbol + "'", arith.right.pos());
                yield (l == DataType.NULL || r == DataType.NULL) ? DataType.NULL : DataType.INTEGER;
            }
            case Expr.Compare cmp -> {
                DataType l = check(cmp.left);
                DataType r = check(cmp.right);
                checkComparisonTypes(cmp, l, r);
                // 与 NULL 字面量比较恒为 UNKNOWN，但静态类型仍为 BOOLEAN
                yield DataType.BOOLEAN;
            }
            case Expr.IsNull ignored -> DataType.BOOLEAN;
            case Expr.Not not -> {
                DataType t = check(not.operand);
                requireBooleanOrNull(t, "NOT", not.operand.pos());
                yield t == DataType.NULL ? DataType.NULL : DataType.BOOLEAN;
            }
            case Expr.Logical log -> {
                DataType l = check(log.left);
                DataType r = check(log.right);
                requireBooleanOrNull(l, log.op.name(), log.left.pos());
                requireBooleanOrNull(r, log.op.name(), log.right.pos());
                yield (l == DataType.NULL || r == DataType.NULL) ? DataType.NULL : DataType.BOOLEAN;
            }
        };
    }

    private void requireNumericOrNull(DataType t, String context, int pos) {
        if (t != DataType.INTEGER && t != DataType.NULL) {
            throw new TypeException(
                    "type error in " + context + ": INTEGER required but got " + t.displayName(),
                    pos);
        }
    }

    private void requireBooleanOrNull(DataType t, String context, int pos) {
        if (t != DataType.BOOLEAN && t != DataType.NULL) {
            throw new TypeException(
                    "type error in " + context + ": boolean expression required but got "
                            + t.displayName(),
                    pos);
        }
    }

    private void checkComparisonTypes(Expr.Compare cmp, DataType l, DataType r) {
        String op = cmp.op.symbol;
        // 与 NULL 字面量比较永远合法
        if (l == DataType.NULL || r == DataType.NULL) {
            return;
        }
        if (l != r) {
            throw new TypeException(
                    "type error in comparison '" + op + "': incompatible types "
                            + l.displayName() + " and " + r.displayName(),
                    cmp.pos());
        }
        boolean ordered = cmp.op == Expr.CompareOp.LT || cmp.op == Expr.CompareOp.LE
                || cmp.op == Expr.CompareOp.GT || cmp.op == Expr.CompareOp.GE;
        if (ordered && (l == DataType.BOOLEAN)) {
            throw new TypeException(
                    "type error in comparison '" + op + "': BOOLEAN cannot be ordered",
                    cmp.pos());
        }
    }
}
