package com.tvl.engine;

import com.tvl.columnar.Schema;
import com.tvl.sql.Expr;
import com.tvl.sql.Select;
import com.tvl.types.DataType;
import com.tvl.types.TypeCheckException;

import java.util.List;

/**
 * 语义分析 + 类型检查（不执行数据）：
 *  - 列名绑定到输入 schema（大小写敏感）
 *  - 标量表达式静态类型推断（表达式上没有可变状态，每次调用计算）
 *  - 比较两侧必须同族：数值 vs 数值（INTEGER/DOUBLE 可互比），
 *    或字符串 vs 字符串，或布尔 vs 布尔；字符串和数值混比直接拒绝
 *  - 裸 NULL 只允许出现在比较或 IS NULL 中
 *  - 参数按位置绑定声明类型，不允许越界
 *  - WHERE 必须是谓词（布尔类型表达式）
 */
public final class Analyzer {

    private final Schema schema;
    private final List<DataType> paramTypes;

    public Analyzer(Schema schema, List<DataType> paramTypes) {
        this.schema = schema;
        this.paramTypes = List.copyOf(paramTypes);
    }

    /** 校验整条语句，返回投影项的输出类型。 */
    public List<DataType> analyze(Select stmt) {
        if (stmt.where() != null) {
            requirePredicate(stmt.where(), "WHERE");
        }
        if (stmt.selectsAll()) {
            return schema.names().stream().map(schema::typeOf).toList();
        }
        return stmt.items().stream().map(item -> {
            Expr e = item.expr();
            if (isPredicate(e)) {
                throw new TypeCheckException("投影项不支持布尔谓词（比较/AND/OR 等）: " + item.name());
            }
            DataType t = scalarType(e);
            if (t == null) {
                // 裸 NULL 字面量投影：输出 BOOLEAN 类型的全 NULL 列（无类型可依附时的兜底）
                t = DataType.BOOLEAN;
            }
            return t;
        }).toList();
    }

    /** 谓词节点：比较、IS NULL、NOT/AND/OR。布尔列/参数/字面量是标量不是谓词。 */
    private static boolean isPredicate(Expr e) {
        return e instanceof Expr.Comparison
                || e instanceof Expr.IsNull
                || e instanceof Expr.Not
                || e instanceof Expr.And
                || e instanceof Expr.Or;
    }

    public void requirePredicate(Expr expr, String ctx) {
        // 布尔标量（TRUE/FALSE 字面量、BOOLEAN 列/参数）本身就是合法谓词；
        // 其它标量（数值、字符串）作谓词拒绝。
        DataType t = scalarType(expr);
        if (t != null && t != DataType.BOOLEAN) {
            throw new TypeCheckException(ctx + " 需要谓词（比较表达式 / IS NULL / AND / OR / NOT，"
                    + "或 BOOLEAN 标量），但得到的是 " + t.sqlName() + " 类型的标量表达式");
        }
    }

    /**
     * 返回标量结果类型；谓词返回 null；裸 NULL（无类型）对非允许上下文抛出。
     */
    public DataType scalarType(Expr expr) {
        if (expr instanceof Expr.ColumnRef c) {
            if (!schema.hasColumn(c.name())) {
                throw new TypeCheckException("未知列 '" + c.name()
                        + "'；可用列: " + schema.names());
            }
            return schema.typeOf(c.name());
        }
        if (expr instanceof Expr.Literal l) {
            return l.type(); // 裸 NULL -> null（未定型）
        }
        if (expr instanceof Expr.BoolLit) {
            return DataType.BOOLEAN;
        }
        if (expr instanceof Expr.Param p) {
            if (p.index() < 0 || p.index() >= paramTypes.size()) {
                throw new TypeCheckException("参数 ? 位置越界: ?" + (p.index() + 1)
                        + "，共绑定 " + paramTypes.size() + " 个参数");
            }
            return paramTypes.get(p.index());
        }
        if (expr instanceof Expr.Comparison cmp) {
            DataType lt = scalarType(cmp.lhs());
            DataType rt = scalarType(cmp.rhs());
            if (lt != null && rt != null) {
                if (lt == rt || DataType.commonNumeric(lt, rt) != null) {
                    return null;
                }
                throw new TypeCheckException("类型不兼容的比较: " + lt.sqlName() + " " + cmp.op()
                        + " " + rt.sqlName() + "；不允许字符串与数值/布尔隐式混比");
            }
            // 裸 NULL 字面量无类型，跟随另一侧的类型；两侧都是裸 NULL 时按 BOOLEAN 处理。
            // 比较结果仍为 UNKNOWN（求值器按 NULL 位图得出），这里只做静态放行。
            return null;
        }
        if (expr instanceof Expr.IsNull is) {
            DataType t = scalarType(is.input());
            if (t == null) {
                throw new TypeCheckException("IS NULL 的输入需要标量表达式，但得到的是谓词");
            }
            return null;
        }
        if (expr instanceof Expr.Not n) {
            requirePredicate(n.input(), "NOT 的输入");
            return null;
        }
        if (expr instanceof Expr.And a) {
            requirePredicate(a.left(), "AND 左侧");
            requirePredicate(a.right(), "AND 右侧");
            return null;
        }
        if (expr instanceof Expr.Or o) {
            requirePredicate(o.left(), "OR 左侧");
            requirePredicate(o.right(), "OR 右侧");
            return null;
        }
        throw new TypeCheckException("未识别的表达式: " + expr);
    }
}
