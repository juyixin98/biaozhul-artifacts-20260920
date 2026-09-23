package com.example.tvl.engine;

import com.example.tvl.engine.QueryPlanner.ColumnSlot;
import com.example.tvl.engine.QueryPlanner.ConstSlot;
import com.example.tvl.engine.QueryPlanner.PAnd;
import com.example.tvl.engine.QueryPlanner.PComparison;
import com.example.tvl.engine.QueryPlanner.PIsNull;
import com.example.tvl.engine.QueryPlanner.PNot;
import com.example.tvl.engine.QueryPlanner.POr;
import com.example.tvl.engine.QueryPlanner.PlanExpr;
import com.example.tvl.engine.QueryPlanner.ParamSlot;
import com.example.tvl.engine.QueryPlanner.Slot;
import com.example.tvl.sql.DataType;

/**
 * 向量化执行器 —— 生产路径。
 *
 * <p>WHERE 结果用两个位图表示三值：
 * <ul>
 *   <li>{@code trueBits}：谓词为 TRUE 的行；</li>
 *   <li>{@code nullBits}：谓词为 UNKNOWN 的行（由 NULL 操作数传播而来）。</li>
 * </ul>
 * FALSE 行两个位图都为 false。最终过滤只保留 TRUE 行，UNKNOWN 被排除
 * （这正是 SQL WHERE 的三值语义）。
 *
 * <p>列值存于原生数组（{@code long[]}/{@code double[]}/{@code String[]}），
 * 每列带独立的 NULL 位图，比较循环不做装箱。
 */
public final class VectorExecutor {

    private VectorExecutor() {}

    /** 计算整个批次上 WHERE 的逐行三值编码。 */
    public static byte[] evalWhere(PlanExpr where, Batch batch, ParameterSet params) {
        int n = batch.rowCount();
        if (where == null) {
            byte[] all = new byte[n];
            java.util.Arrays.fill(all, (byte) SqlBool.TRUE.code());
            return all;
        }
        return eval(where, batch, params, n);
    }

    private static byte[] eval(PlanExpr e, Batch batch, ParameterSet params, int n) {
        if (e instanceof PIsNull isn) {
            return evalIsNull(isn, batch, params, n);
        }
        if (e instanceof PComparison cmp) {
            return evalComparison(cmp, batch, params, n);
        }
        if (e instanceof PNot not) {
            byte[] inner = eval(not.inner(), batch, params, n);
            for (int r = 0; r < n; r++) {
                inner[r] = (byte) SqlBool.fromCode(inner[r]).not().code();
            }
            return inner;
        }
        if (e instanceof PAnd and) {
            byte[] acc = null;
            for (PlanExpr t : and.terms()) {
                byte[] v = eval(t, batch, params, n);
                if (acc == null) {
                    acc = v;
                } else {
                    for (int r = 0; r < n; r++) {
                        acc[r] = (byte) SqlBool.and(SqlBool.fromCode(acc[r]), SqlBool.fromCode(v[r])).code();
                    }
                }
            }
            return acc == null ? fill(n, SqlBool.TRUE) : acc;
        }
        if (e instanceof POr or) {
            byte[] acc = null;
            for (PlanExpr t : or.terms()) {
                byte[] v = eval(t, batch, params, n);
                if (acc == null) {
                    acc = v;
                } else {
                    for (int r = 0; r < n; r++) {
                        acc[r] = (byte) SqlBool.or(SqlBool.fromCode(acc[r]), SqlBool.fromCode(v[r])).code();
                    }
                }
            }
            return acc == null ? fill(n, SqlBool.FALSE) : acc;
        }
        throw new AssertionError("未知计划节点: " + e);
    }

    private static byte[] fill(int n, SqlBool b) {
        byte[] out = new byte[n];
        java.util.Arrays.fill(out, (byte) b.code());
        return out;
    }

    private static byte[] evalIsNull(PIsNull isn, Batch batch, ParameterSet params, int n) {
        byte[] out = new byte[n];
        Slot s = isn.slot();
        boolean[] sourceNulls = nullBitmapOf(s, batch, params);
        byte t = (byte) SqlBool.TRUE.code();
        byte f = (byte) SqlBool.FALSE.code();
        for (int r = 0; r < n; r++) {
            boolean isNull = sourceNulls[r];
            out[r] = isNull != isn.negated() ? t : f; // IS NULL / IS NOT NULL 恒为真或假
        }
        return out;
    }

    private static byte[] evalComparison(PComparison cmp, Batch batch, ParameterSet params, int n) {
        byte[] out = new byte[n];
        Slot ls = cmp.left();
        Slot rs = cmp.right();
        boolean[] ln = nullBitmapOf(ls, batch, params);
        boolean[] rn = nullBitmapOf(rs, batch, params);
        String op = cmp.op();

        DataType lt = ls.type();
        DataType rt = rs.type();
        boolean bothText = lt == DataType.TEXT && rt == DataType.TEXT;
        boolean bothLong = lt == DataType.INTEGER && rt == DataType.INTEGER;
        // 裸 NULL（静态类型 null）与任何类型比较：所有非 NULL 侧的行仍为 UNKNOWN
        boolean untyped = lt == null || rt == null;

        byte unk = (byte) SqlBool.UNKNOWN.code();
        byte t = (byte) SqlBool.TRUE.code();
        byte f = (byte) SqlBool.FALSE.code();

        for (int r = 0; r < n; r++) {
            if (ln[r] || rn[r]) {
                out[r] = unk;
                continue;
            }
            if (untyped) {
                out[r] = unk; // 非 NULL 侧与裸 NULL 比较仍为 UNKNOWN（裸 NULL 侧位图恒为 true，不会走到这）
                continue;
            }
            SqlBool b;
            if (bothText) {
                b = Comparisons.compare(textOf(ls, batch, params, r), textOf(rs, batch, params, r), op);
            } else if (bothLong) {
                b = Comparisons.compare(longOf(ls, batch, params, r), longOf(rs, batch, params, r), op);
            } else {
                b = Comparisons.compare(doubleOf(ls, batch, params, r), doubleOf(rs, batch, params, r), op);
            }
            out[r] = b == SqlBool.TRUE ? t : f;
        }
        return out;
    }

    // ---- 槽位 → 位图 / 原生值 ----

    private static boolean[] nullBitmapOf(Slot slot, Batch batch, ParameterSet params) {
        if (slot instanceof ColumnSlot cs) {
            return batch.column(cs.index()).nullBitmap();
        }
        if (slot instanceof ConstSlot ct) {
            boolean isNull = ct.untypedNull() || ct.value().isNull();
            return constBitmap(batch.rowCount(), isNull);
        }
        if (slot instanceof ParamSlot ps) {
            return constBitmap(batch.rowCount(), params.get(ps.index()).isNull());
        }
        throw new AssertionError("未知槽位: " + slot);
    }

    private static boolean[] constBitmap(int n, boolean value) {
        boolean[] bm = new boolean[n];
        if (value) {
            java.util.Arrays.fill(bm, true);
        }
        return bm;
    }

    private static long longOf(Slot slot, Batch batch, ParameterSet params, int row) {
        if (slot instanceof ColumnSlot cs) {
            return batch.column(cs.index()).getLong(row);
        }
        if (slot instanceof ConstSlot ct) {
            return ct.value().asLong();
        }
        return params.get(((ParamSlot) slot).index()).asLong();
    }

    private static double doubleOf(Slot slot, Batch batch, ParameterSet params, int row) {
        if (slot instanceof ColumnSlot cs) {
            Column c = batch.column(cs.index());
            return c.type() == DataType.INTEGER ? c.getLong(row) : c.getDouble(row);
        }
        if (slot instanceof ConstSlot ct) {
            Object v = ct.value().value();
            return v instanceof Long l ? l.doubleValue() : ((Number) v).doubleValue();
        }
        Value pv = params.get(((ParamSlot) slot).index());
        return pv.type() == DataType.INTEGER ? pv.asLong() : pv.asDouble();
    }

    private static String textOf(Slot slot, Batch batch, ParameterSet params, int row) {
        if (slot instanceof ColumnSlot cs) {
            return batch.column(cs.index()).getString(row);
        }
        if (slot instanceof ConstSlot ct) {
            return ct.value().asText();
        }
        return params.get(((ParamSlot) slot).index()).asText();
    }
}
