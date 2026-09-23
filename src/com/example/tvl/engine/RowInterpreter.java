package com.example.tvl.engine;

import com.example.tvl.engine.QueryPlanner.PAnd;
import com.example.tvl.engine.QueryPlanner.PComparison;
import com.example.tvl.engine.QueryPlanner.PIsNull;
import com.example.tvl.engine.QueryPlanner.PNot;
import com.example.tvl.engine.QueryPlanner.POr;
import com.example.tvl.engine.QueryPlanner.PlanExpr;
import com.example.tvl.engine.QueryPlanner.Slot;
import com.example.tvl.sql.DataType;

/**
 * 逐行解释器 —— 参考实现（spec oracle）。
 *
 * 每行独立、不做任何批量优化，语义最直白，供测试对照向量化执行器。
 */
public final class RowInterpreter {

    private RowInterpreter() {}

    /** 计算某一行上 WHERE 的三值结果；无 WHERE 时返回 TRUE。 */
    public static SqlBool evalWhere(PlanExpr where, Batch batch, int row, ParameterSet params) {
        if (where == null) {
            return SqlBool.TRUE;
        }
        return eval(where, batch, row, params);
    }

    private static SqlBool eval(PlanExpr e, Batch batch, int row, ParameterSet params) {
        if (e instanceof PIsNull isn) {
            Object v = Comparisons.read(isn.slot(), batch, row, params);
            boolean isNull = v == null;
            return isNull != isn.negated() ? SqlBool.TRUE : SqlBool.FALSE;
        }
        if (e instanceof PComparison cmp) {
            return evalComparison(cmp, batch, row, params);
        }
        if (e instanceof PNot n) {
            return eval(n.inner(), batch, row, params).not();
        }
        if (e instanceof PAnd and) {
            SqlBool acc = SqlBool.TRUE;
            for (PlanExpr t : and.terms()) {
                acc = SqlBool.and(acc, eval(t, batch, row, params));
                if (acc == SqlBool.FALSE) {
                    return SqlBool.FALSE;
                }
            }
            return acc;
        }
        if (e instanceof POr or) {
            SqlBool acc = SqlBool.FALSE;
            for (PlanExpr t : or.terms()) {
                acc = SqlBool.or(acc, eval(t, batch, row, params));
                if (acc == SqlBool.TRUE) {
                    return SqlBool.TRUE;
                }
            }
            return acc;
        }
        throw new AssertionError("未知计划节点: " + e);
    }

    private static SqlBool evalComparison(PComparison cmp, Batch batch, int row, ParameterSet params) {
        Slot ls = cmp.left();
        Slot rs = cmp.right();
        Object lv = Comparisons.read(ls, batch, row, params);
        Object rv = Comparisons.read(rs, batch, row, params);
        if (lv == null || rv == null) {
            return SqlBool.UNKNOWN;
        }
        DataType lt = ls.type() == null ? dataTypeOf(lv) : ls.type();
        DataType rt = rs.type() == null ? dataTypeOf(rv) : rs.type();
        String op = cmp.op();

        if (lt == DataType.TEXT && rt == DataType.TEXT) {
            return Comparisons.compare((String) lv, (String) rv, op);
        }
        if (lt == DataType.INTEGER && rt == DataType.INTEGER) {
            return Comparisons.compare((Long) lv, (Long) rv, op);
        }
        // 数值混型（INTEGER/FLOAT）提升为 double
        return Comparisons.compare(((Number) lv).doubleValue(), ((Number) rv).doubleValue(), op);
    }

    /** 裸 NULL 的槽位静态类型为 null，但其值若非 null 不会发生（解析器保证），兜底用载体类型。 */
    private static DataType dataTypeOf(Object v) {
        if (v instanceof Long) {
            return DataType.INTEGER;
        }
        if (v instanceof Number) {
            return DataType.FLOAT;
        }
        return DataType.TEXT;
    }
}
