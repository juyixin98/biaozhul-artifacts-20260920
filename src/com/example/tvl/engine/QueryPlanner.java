package com.example.tvl.engine;

import com.example.tvl.sql.Ast;
import com.example.tvl.sql.Ast.Expr;
import com.example.tvl.sql.Ast.Operand;
import com.example.tvl.sql.Ast.Query;
import com.example.tvl.sql.DataType;

import java.util.ArrayList;
import java.util.List;

/**
 * 把 AST 编译为执行计划，同时完成全部静态类型检查：
 *
 * <ul>
 *   <li>列必须存在；</li>
 *   <li>比较两侧必须同为数值类或同为文本类，禁止 TEXT↔数值 隐式转换；</li>
 *   <li>裸 NULL 关键字无类型，与任何类型比较都合法但结果恒 UNKNOWN；</li>
 *   <li>参数按其声明类型参与检查（即使绑定值是 NULL）。</li>
 * </ul>
 */
public final class QueryPlanner {

    private final Schema schema;
    private final ParameterSet params;

    private QueryPlanner(Schema schema, ParameterSet params) {
        this.schema = schema;
        this.params = params;
    }

    /** 编译后的 WHERE 计划；where 为空时为 null。 */
    public record Plan(List<int[]> projection, PlanExpr where) {}

    public sealed interface PlanExpr permits PComparison, PIsNull, PNot, PAnd, POr {}

    public record PComparison(Slot left, String op, Slot right) implements PlanExpr {}

    public record PIsNull(Slot slot, boolean negated) implements PlanExpr {}

    public record PNot(PlanExpr inner) implements PlanExpr {}

    public record PAnd(List<PlanExpr> terms) implements PlanExpr {}

    public record POr(List<PlanExpr> terms) implements PlanExpr {}

    /** 已解析的标量来源。 */
    public sealed interface Slot permits ColumnSlot, ConstSlot, ParamSlot {
        /** 静态类型；裸 NULL 返回 null。 */
        DataType type();
    }

    public record ColumnSlot(int index, DataType type) implements Slot {}

    /** untypedNull 为 true 时表示 SQL 裸 NULL 关键字。 */
    public record ConstSlot(Value value, boolean untypedNull) implements Slot {
        @Override
        public DataType type() {
            return untypedNull ? null : value.type();
        }
    }

    public record ParamSlot(int index, DataType type) implements Slot {}

    public static Plan compile(Query query, Schema schema, ParameterSet params) {
        QueryPlanner qp = new QueryPlanner(schema, params);

        List<int[]> projection = new ArrayList<>();
        if (query.selectAll()) {
            for (int i = 0; i < schema.size(); i++) {
                projection.add(new int[]{i});
            }
        } else {
            for (String name : query.columns()) {
                projection.add(new int[]{schema.requireIndex(name)});
            }
        }

        PlanExpr where = query.where() == null ? null : qp.compileExpr(query.where());
        return new Plan(List.copyOf(projection), where);
    }

    private PlanExpr compileExpr(Expr e) {
        if (e instanceof Ast.Comparison c) {
            Slot left = compileSlot(c.left());
            Slot right = compileSlot(c.right());
            checkComparable(left, right, c.operator());
            return new PComparison(left, c.operator(), right);
        }
        if (e instanceof Ast.IsNull isn) {
            return new PIsNull(compileSlot(isn.operand()), isn.negated());
        }
        if (e instanceof Ast.NotExpr n) {
            return new PNot(compileExpr(n.operand()));
        }
        if (e instanceof Ast.BoolExpr b) {
            List<PlanExpr> terms = b.terms().stream().map(this::compileExpr).toList();
            return b.conjunction() ? new PAnd(terms) : new POr(terms);
        }
        throw new AssertionError("未知表达式: " + e);
    }

    private Slot compileSlot(Operand o) {
        if (o instanceof Ast.ColumnRef col) {
            int idx = schema.requireIndex(col.name());
            return new ColumnSlot(idx, schema.columns().get(idx).type());
        }
        if (o instanceof Ast.Literal lit) {
            if (lit.type() == null) {
                // 裸 NULL
                return new ConstSlot(new Value(null, null), true);
            }
            return new ConstSlot(new Value(lit.type(), lit.value()), false);
        }
        if (o instanceof Ast.Param p) {
            if (p.index() >= params.size()) {
                throw new SemanticException("内部错误：参数下标越界 " + p.index());
            }
            return new ParamSlot(p.index(), params.get(p.index()).type());
        }
        throw new AssertionError("未知操作数: " + o);
    }

    private void checkComparable(Slot left, Slot right, String op) {
        DataType lt = left.type();
        DataType rt = right.type();
        // 裸 NULL 不参与类型冲突，比较结果恒 UNKNOWN
        if (lt == null || rt == null) {
            return;
        }
        boolean lNum = lt == DataType.INTEGER || lt == DataType.FLOAT;
        boolean rNum = rt == DataType.INTEGER || rt == DataType.FLOAT;
        if (lNum && rNum) {
            return;
        }
        if (lt == DataType.TEXT && rt == DataType.TEXT) {
            return;
        }
        throw new SemanticException(
                "类型不匹配：运算符 " + op + " 两侧为 " + lt + " 与 " + rt
                        + "（拒绝字符串与数值之间的隐式转换）");
    }
}
