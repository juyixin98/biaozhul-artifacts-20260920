package com.tvl.engine;

import com.tvl.columnar.Batch;
import com.tvl.columnar.ColumnVector;
import com.tvl.columnar.Schema;
import com.tvl.core.Ternary;
import com.tvl.core.TruthVector;
import com.tvl.sql.Expr;
import com.tvl.types.DataType;

import java.util.List;
import java.util.function.IntPredicate;

/**
 * 向量化执行器：
 *  1) 标量表达式按需物化成列向量（列引用零拷贝，常量/参数按批广播）；
 *  2) 谓词表达式整体产出 byte[] 三值向量，逻辑运算只走位图循环；
 *  3) 任一侧为 NULL 的比较结果字节是 UNKNOWN —— NULL 既不等于也不不等于任何值。
 *
 * 物化缓存按"表达式身份"在一个批次内复用，避免同一列被 AND/OR 引用时重复广播。
 */
public final class VectorEngine {

    private final List<Object> params;
    private final List<DataType> paramTypes;

    public VectorEngine(List<DataType> paramTypes, List<Object> params) {
        this.paramTypes = paramTypes;
        this.params = params;
    }

    /** 计算 WHERE 谓词的三值向量；where 为 null 时整批为 TRUE。 */
    public TruthVector evaluateWhere(Expr where, Batch batch) {
        if (where == null) {
            byte[] all = new byte[batch.rowCount()];
            java.util.Arrays.fill(all, Ternary.TRUE.code);
            return new TruthVector(all);
        }
        return evalPredicate(where, batch, new java.util.IdentityHashMap<>());
    }

    /** 把标量表达式物化成列向量（供投影使用）。 */
    public ColumnVector materialize(Expr scalar, Batch batch) {
        return evalScalar(scalar, batch, new java.util.IdentityHashMap<>());
    }

    private static boolean isBareNull(Expr expr) {
        return expr instanceof Expr.Literal l && l.type() == null;
    }

    /** 把 INTEGER 或 DOUBLE 向量的值读入 double 缓冲（NULL 位为占位 0，不会被读取）。 */
    private static void fillDoubles(ColumnVector v, double[] dst) {
        if (v.type() == DataType.DOUBLE) {
            double[] src = v.doubles();
            System.arraycopy(src, 0, dst, 0, src.length);
        } else {
            long[] src = v.longs();
            for (int i = 0; i < src.length; i++) {
                dst[i] = src[i];
            }
        }
    }

    /* ---------------- 谓词 ---------------- */

    private TruthVector evalPredicate(Expr expr, Batch batch,
                                      java.util.IdentityHashMap<Expr, Object> cache) {
        if (expr instanceof Expr.Comparison cmp) {
            ColumnVector l = evalScalar(cmp.lhs(), batch, cache);
            ColumnVector r = evalScalar(cmp.rhs(), batch, cache);
            DataType target = CompareOps.resolveType(l.type(), r.type());
            // 裸 NULL 字面量物化成了 BOOLEAN 占位；按公共类型重建全 NULL 向量。
            if (isBareNull(cmp.lhs())) {
                l = ColumnVector.constant(target, null, batch.rowCount());
            }
            if (isBareNull(cmp.rhs())) {
                r = ColumnVector.constant(target, null, batch.rowCount());
            }
            return compareColumns(cmp.op(), l, r);
        }
        if (expr instanceof Expr.IsNull is) {
            ColumnVector v = evalScalar(is.input(), batch, cache);
            return isNullVector(v, is.negate());
        }
        if (expr instanceof Expr.Not n) {
            return evalPredicate(n.input(), batch, cache).not();
        }
        if (expr instanceof Expr.And a) {
            return evalPredicate(a.left(), batch, cache)
                    .and(evalPredicate(a.right(), batch, cache));
        }
        if (expr instanceof Expr.Or o) {
            return evalPredicate(o.left(), batch, cache)
                    .or(evalPredicate(o.right(), batch, cache));
        }
        if (expr instanceof Expr.BoolLit b) {
            byte code = b.value() ? Ternary.TRUE.code : Ternary.FALSE.code;
            byte[] out = new byte[batch.rowCount()];
            java.util.Arrays.fill(out, code);
            return new TruthVector(out);
        }
        if (expr instanceof Expr.ColumnRef || expr instanceof Expr.Param) {
            // BOOLEAN 标量直接当谓词用（分析器已保证类型）：NULL -> UNKNOWN
            ColumnVector v = evalScalar(expr, batch, cache);
            byte[] out = new byte[batch.rowCount()];
            Boolean[] bs = v.booleans();
            for (int i = 0; i < out.length; i++) {
                out[i] = v.isNullAt(i) ? Ternary.UNKNOWN.code
                        : (bs[i] ? Ternary.TRUE.code : Ternary.FALSE.code);
            }
            return new TruthVector(out);
        }
        throw new IllegalStateException("谓词求值遇到非谓词表达式: " + expr);
    }

    private TruthVector compareColumns(String op, ColumnVector l, ColumnVector r) {
        int n = l.size();
        DataType common = CompareOps.resolveType(l.type(), r.type());
        IntPredicate pred = CompareOps.predicate(op);
        byte[] out = new byte[n];
        boolean[] ln = l.nullBitmap();
        boolean[] rn = r.nullBitmap();

        switch (common) {
            case INTEGER: {
                long[] la = l.longs();
                long[] ra = r.longs();
                for (int i = 0; i < n; i++) {
                    out[i] = ln[i] || rn[i] ? Ternary.UNKNOWN.code
                            : Ternary.fromBool(pred.test(Long.compare(la[i], ra[i]))).code;
                }
                break;
            }
            case DOUBLE: {
                // 两侧可能是 INTEGER(long[]) 或 DOUBLE(double[])，按各自类型取值
                double[] da = new double[n];
                double[] db = new double[n];
                fillDoubles(l, da);
                fillDoubles(r, db);
                for (int i = 0; i < n; i++) {
                    out[i] = ln[i] || rn[i] ? Ternary.UNKNOWN.code
                            : Ternary.fromBool(pred.test(Double.compare(da[i], db[i]))).code;
                }
                break;
            }
            case STRING: {
                String[] sa = l.strings();
                String[] sb = r.strings();
                for (int i = 0; i < n; i++) {
                    out[i] = ln[i] || rn[i] ? Ternary.UNKNOWN.code
                            : Ternary.fromBool(pred.test(sa[i].compareTo(sb[i]))).code;
                }
                break;
            }
            case BOOLEAN: {
                Boolean[] ba = l.booleans();
                Boolean[] bb = r.booleans();
                for (int i = 0; i < n; i++) {
                    out[i] = ln[i] || rn[i] ? Ternary.UNKNOWN.code
                            : Ternary.fromBool(pred.test(Boolean.compare(ba[i], bb[i]))).code;
                }
                break;
            }
            default:
                throw new IllegalStateException(String.valueOf(common));
        }
        return new TruthVector(out);
    }

    private TruthVector isNullVector(ColumnVector v, boolean negate) {
        int n = v.size();
        byte[] out = new byte[n];
        boolean[] nulls = v.nullBitmap();
        for (int i = 0; i < n; i++) {
            boolean isNull = nulls[i];
            if (negate) {
                isNull = !isNull;
            }
            out[i] = isNull ? Ternary.TRUE.code : Ternary.FALSE.code;
        }
        return new TruthVector(out);
    }

    /* ---------------- 标量 ---------------- */

    private ColumnVector evalScalar(Expr expr, Batch batch,
                                    java.util.IdentityHashMap<Expr, Object> cache) {
        Object cached = cache.get(expr);
        if (cached != null) {
            return (ColumnVector) cached;
        }
        ColumnVector v = evalScalarUncached(expr, batch, cache);
        cache.put(expr, v);
        return v;
    }

    private ColumnVector evalScalarUncached(Expr expr, Batch batch,
                                            java.util.IdentityHashMap<Expr, Object> cache) {
        int n = batch.rowCount();
        if (expr instanceof Expr.ColumnRef c) {
            return batch.column(c.name()); // 零拷贝
        }
        if (expr instanceof Expr.Param p) {
            DataType type = paramTypes.get(p.index());
            return ColumnVector.constant(type, params.get(p.index()), n);
        }
        if (expr instanceof Expr.Literal l) {
            DataType type = l.type() != null ? l.type() : DataType.BOOLEAN;
            return ColumnVector.constant(type, l.value(), n);
        }
        if (expr instanceof Expr.BoolLit b) {
            return ColumnVector.constant(DataType.BOOLEAN, b.value(), n);
        }
        if (expr instanceof Expr.Comparison
                || expr instanceof Expr.IsNull
                || expr instanceof Expr.Not
                || expr instanceof Expr.And
                || expr instanceof Expr.Or) {
            // 谓词当标量用的唯一合法情形：与裸 TRUE/FALSE 比较，分析器已限制投影为标量；
            // 走到这里说明内部状态不一致。
            throw new IllegalStateException("谓词不能作为标量物化: " + expr);
        }
        throw new IllegalStateException("未识别的标量表达式: " + expr);
    }
}
