package com.tvl.engine;

import com.tvl.columnar.Batch;
import com.tvl.core.Ternary;
import com.tvl.core.TruthVector;
import com.tvl.sql.Expr;
import com.tvl.types.DataType;

import java.util.List;
import java.util.function.IntPredicate;

/**
 * 逐行解释器 —— 与向量执行器独立的第二套执行路径，作为验收时的对照参考。
 * 每次只处理一行：标量求值为 Object（null 即 SQL NULL），谓词求值为 Ternary。
 * 比较语义与向量执行器共享 {@link CompareOps}，确保两边只差在"执行方式"。
 */
public final class RowInterpreter {

    private final List<DataType> paramTypes;
    private final List<Object> params;

    public RowInterpreter(List<DataType> paramTypes, List<Object> params) {
        this.paramTypes = paramTypes;
        this.params = params;
    }

    /** 对整批逐行求值谓词，产出与向量执行器同形态的 byte[] 三值向量。 */
    public TruthVector evaluateWhere(Expr where, Batch batch) {
        int n = batch.rowCount();
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            Ternary t = where == null ? Ternary.TRUE : evalPredicate(where, batch, i);
            out[i] = t.code;
        }
        return new TruthVector(out);
    }

    /** 投影：取第 row 行的标量值（null = SQL NULL）。 */
    public Object evalScalarValue(Expr expr, Batch batch, int row) {
        return evalScalar(expr, batch, row);
    }

    public Ternary evalPredicate(Expr expr, Batch batch, int row) {
        if (expr instanceof Expr.Comparison cmp) {
            Object a = evalScalar(cmp.lhs(), batch, row);
            Object b = evalScalar(cmp.rhs(), batch, row);
            if (a == null || b == null) {
                return Ternary.UNKNOWN;
            }
            DataType common = CompareOps.resolveType(
                    scalarType(cmp.lhs(), batch), scalarType(cmp.rhs(), batch));
            IntPredicate pred = CompareOps.predicate(cmp.op());
            return Ternary.fromBool(pred.test(CompareOps.compareNonNull(a, b, common)));
        }
        if (expr instanceof Expr.IsNull is) {
            Object v = evalScalar(is.input(), batch, row);
            boolean isNull = v == null;
            return Ternary.fromBool(is.negate() ? !isNull : isNull);
        }
        if (expr instanceof Expr.Not n) {
            return evalPredicate(n.input(), batch, row).not();
        }
        if (expr instanceof Expr.And a) {
            return evalPredicate(a.left(), batch, row)
                    .and(evalPredicate(a.right(), batch, row));
        }
        if (expr instanceof Expr.Or o) {
            return evalPredicate(o.left(), batch, row)
                    .or(evalPredicate(o.right(), batch, row));
        }
        if (expr instanceof Expr.BoolLit b) {
            return Ternary.fromBool(b.value());
        }
        if (expr instanceof Expr.ColumnRef || expr instanceof Expr.Param) {
            Object v = evalScalar(expr, batch, row);
            return v == null ? Ternary.UNKNOWN : Ternary.fromBool((Boolean) v);
        }
        throw new IllegalStateException("谓词逐行求值遇到非谓词表达式: " + expr);
    }

    private Object evalScalar(Expr expr, Batch batch, int row) {
        if (expr instanceof Expr.ColumnRef c) {
            return batch.column(c.name()).valueAt(row);
        }
        if (expr instanceof Expr.Param p) {
            return params.get(p.index()); // 可能为 null（参数本身传 NULL）
        }
        if (expr instanceof Expr.Literal l) {
            return l.value();
        }
        if (expr instanceof Expr.BoolLit b) {
            return b.value();
        }
        throw new IllegalStateException("逐行求值遇到非标量表达式: " + expr);
    }

    /** AST 标量在当前批次下的静态类型；裸 NULL 返回 null。 */
    private DataType scalarType(Expr expr, Batch batch) {
        if (expr instanceof Expr.ColumnRef c) {
            return batch.column(c.name()).type();
        }
        if (expr instanceof Expr.Literal l) {
            return l.type();
        }
        if (expr instanceof Expr.Param p) {
            return paramTypes.get(p.index());
        }
        if (expr instanceof Expr.BoolLit) {
            return DataType.BOOLEAN;
        }
        return null;
    }
}
