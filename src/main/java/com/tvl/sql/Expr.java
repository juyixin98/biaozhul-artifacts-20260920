package com.tvl.sql;

import com.tvl.types.DataType;

/**
 * 表达式 AST。
 * 标量表达式 resultType 为 INTEGER/DOUBLE/STRING/BOOLEAN（值）；
 * 谓词表达式 isPredicate 为 true（用于 WHERE、NOT、AND/OR）。
 */
public sealed interface Expr
        permits Expr.ColumnRef, Expr.Literal, Expr.Param, Expr.Comparison,
        Expr.IsNull, Expr.Not, Expr.And, Expr.Or, Expr.BoolLit {

    /** 列引用（列名大小写敏感）。 */
    record ColumnRef(String name) implements Expr {
    }

    /** 字面量；value 为 null 表示裸 NULL（无类型，只允许出现在 IS NULL 或比较中）。 */
    record Literal(DataType type, Object value) implements Expr {
    }

    /** 布尔字面量 TRUE / FALSE（谓词上下文）。 */
    record BoolLit(boolean value) implements Expr {
    }

    /** 位置参数 ?，下标从 0 开始。 */
    record Param(int index) implements Expr {
    }

    /** 比较谓词：lhs op rhs，op 为 = <> != < <= > >= 之一。 */
    record Comparison(String op, Expr lhs, Expr rhs) implements Expr {
    }

    /** IS NULL / IS NOT NULL。 */
    record IsNull(Expr input, boolean negate) implements Expr {
    }

    record Not(Expr input) implements Expr {
    }

    record And(Expr left, Expr right) implements Expr {
    }

    record Or(Expr left, Expr right) implements Expr {
    }
}
