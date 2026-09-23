package com.example.tvl.sql;

import java.util.List;

/**
 * 抽象语法树。
 *
 * 支持的 SQL 子集：
 *   query  := SELECT ('*' | 列名(','列名)*) FROM 表名 [WHERE 表达式]
 *   表达式 := 或表达式
 *   或     := 与 (OR 与)*               —— AND 优先级高于 OR
 *   与     := 非 (AND 非)*
 *   非     := NOT 非 | 谓词
 *   谓词   := '(' 表达式 ')' | 比较 | IS NULL
 *   比较   := 操作数 比较运算符 操作数
 *   操作数 := 列名 | 字面量 | '?' 参数占位符
 */
public final class Ast {

    private Ast() {}

    /** 可出现在比较 / IS NULL 两侧的值。 */
    public sealed interface Operand permits ColumnRef, Literal, Param {}

    /** 列引用。 */
    public record ColumnRef(String name) implements Operand {}

    /**
     * 字面量。{@code type() == null} 且 {@code value() == null} 表示无类型 NULL
     * （SQL 中裸 NULL 关键字）；带类型的 NULL 来自参数绑定。
     */
    public record Literal(Object value, DataType type) implements Operand {}

    /** 参数占位符，index 为在整个查询中出现的序号（从 0 开始）。 */
    public record Param(int index) implements Operand {}

    /** 布尔表达式节点。 */
    public sealed interface Expr permits Comparison, IsNull, NotExpr, BoolExpr {}

    /** 比较谓词，operator 已规范化为 =, &lt;&gt;, &lt;, &lt;=, &gt;, &gt;=。 */
    public record Comparison(Operand left, String operator, Operand right) implements Expr {}

    /** IS [NOT] NULL。结果恒为 TRUE / FALSE，绝不为 UNKNOWN。 */
    public record IsNull(Operand operand, boolean negated) implements Expr {}

    /** NOT：TRUE↔FALSE，UNKNOWN 保持 UNKNOWN。 */
    public record NotExpr(Expr operand) implements Expr {}

    /** 平铺开的 AND（conjunction=true）或 OR 链。 */
    public record BoolExpr(boolean conjunction, List<Expr> terms) implements Expr {}

    /** 解析后的查询。table 仅做语法消费，本服务不维护目录。 */
    public record Query(boolean selectAll, List<String> columns, String table, Expr where) {}
}
