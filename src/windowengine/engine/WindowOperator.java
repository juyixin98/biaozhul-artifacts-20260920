package windowengine.engine;

import windowengine.EngineException;
import windowengine.ErrorCode;
import windowengine.Row;
import windowengine.Value;
import windowengine.plan.FrameBound;
import windowengine.plan.FunctionCall;
import windowengine.plan.WindowFunction;
import windowengine.plan.WindowSpec;

import java.math.BigInteger;
import java.util.List;

/**
 * 窗口算子：对一个已按 ORDER BY 排好序的分区计算各窗口函数。
 *
 * 三种函数的语义（全部按 SQL 标准定义）：
 * - ROW_NUMBER：分区内行的物理序号（1 基）。并列键相等时也不并列，
 *   用原始输入行号稳定打破并列。
 * - RANK：排名（1 基）。并列键完全相等的行得到相同排名，之后的排名跳号
 *   （1,1,3）。无 ORDER BY 时整个分区互为并列，全得 1。
 * - SUM(x) over ROWS 帧：帧是 [start,end] 的物理行区间，帧按分区边界裁剪，
 *   不相交或起点超过终点时帧为空，空帧结果为 NULL；帧内 NULL 输入被忽略，
 *   全 NULL 的帧同样得到 NULL；求和用 BigInteger 前缀和计算，最终值必须落在
 *   long 范围内，否则抛 OVERFLOW。
 */
public final class WindowOperator {

    private final WindowSpec spec;

    public WindowOperator(WindowSpec spec) {
        this.spec = spec;
    }

    /**
     * 计算一个排序后分区。
     *
     * @param sorted         分区内行，已按 ORDER BY（含稳定兜底）排序
     * @param orderColumns   排序键在 schema 中的列下标
     * @param functions      函数调用
     * @param argColumns     每个函数的入参列下标（SUM 为参数列，其余 -1）
     * @return 每行一个 Value[]，顺序与 functions 对应，即结果列
     */
    public Value[][] apply(List<Row> sorted,
                           int[] orderColumns,
                           List<FunctionCall> functions,
                           int[] argColumns) {
        int n = sorted.size();
        int fnCount = functions.size();
        Value[][] out = new Value[n][fnCount];

        for (int f = 0; f < fnCount; f++) {
            WindowFunction fn = functions.get(f).function();
            switch (fn) {
                case ROW_NUMBER -> evalRowNumber(sorted, out, f);
                case RANK -> evalRank(sorted, out, f, orderColumns);
                case SUM -> evalSum(sorted, out, f, argColumns[f], functions.get(f).alias());
            }
        }
        return out;
    }

    // ---------- ROW_NUMBER ----------

    private void evalRowNumber(List<Row> sorted, Value[][] out, int f) {
        for (int i = 0; i < sorted.size(); i++) {
            out[i][f] = Value.ofLong(i + 1L);
        }
    }

    // ---------- RANK ----------

    private void evalRank(List<Row> sorted, Value[][] out, int f, int[] orderColumns) {
        int n = sorted.size();
        if (n == 0) {
            return;
        }
        if (orderColumns.length == 0) {
            // 无 ORDER BY：整个分区并列，排名全为 1（SQL 标准）
            for (int i = 0; i < n; i++) {
                out[i][f] = Value.ofLong(1L);
            }
            return;
        }
        // 逐行扫描：与上一行互为并列则沿用排名，否则排名 = 物理位置 + 1（造成跳号）
        out[0][f] = Value.ofLong(1L);
        for (int i = 1; i < n; i++) {
            if (isPeer(sorted.get(i - 1), sorted.get(i), orderColumns)) {
                out[i][f] = out[i - 1][f];
            } else {
                out[i][f] = Value.ofLong(i + 1L);
            }
        }
    }

    /** 并列判定：所有 ORDER BY 列值等值（与 ASC/DESC、NULL 位置无关；NULL 互为并列）。 */
    private boolean isPeer(Row a, Row b, int[] orderColumns) {
        for (int col : orderColumns) {
            if (!a.get(col).valueEquals(b.get(col))) {
                return false;
            }
        }
        return true;
    }

    // ---------- SUM over ROWS ----------

    private void evalSum(List<Row> sorted, Value[][] out, int f,
                         int argColumn, String alias) {
        int n = sorted.size();
        if (n == 0) {
            return;
        }

        // 前缀和：prefix[i+1] = 前 i 个非 NULL 值之和；nullCount[i+1] = 前 i 行中 NULL 数。
        // 用 BigInteger 累积，数学结果超出 long 时在取值阶段抛 OVERFLOW。
        BigInteger[] prefix = new BigInteger[n + 1];
        int[] nullCount = new int[n + 1];
        prefix[0] = BigInteger.ZERO;
        for (int i = 0; i < n; i++) {
            Value v = sorted.get(i).get(argColumn);
            if (v.isNull()) {
                prefix[i + 1] = prefix[i];
                nullCount[i + 1] = nullCount[i] + 1;
            } else {
                long lv;
                try {
                    lv = v.asLong();
                } catch (EngineException e) {
                    throw new EngineException(ErrorCode.TYPE_MISMATCH,
                            "SUM(" + alias + ") 帧内出现非 LONG 值（行 sourceIndex="
                                    + sorted.get(i).sourceIndex() + "）", e);
                }
                prefix[i + 1] = prefix[i].add(BigInteger.valueOf(lv));
                nullCount[i + 1] = nullCount[i];
            }
        }

        for (int i = 0; i < n; i++) {
            int lo = frameIndex(i, spec.frame().start(), n);
            int hi = frameIndex(i, spec.frame().end(), n);
            // 与分区 [0,n-1] 求交；不相交 / start>end 即空帧
            int clampedLo = Math.max(lo, 0);
            int clampedHi = Math.min(hi, n - 1);
            if (lo > hi || clampedLo > clampedHi) {
                out[i][f] = Value.NULL;
                continue;
            }
            int nonNulls = (clampedHi - clampedLo + 1)
                    - (nullCount[clampedHi + 1] - nullCount[clampedLo]);
            if (nonNulls == 0) {
                out[i][f] = Value.NULL; // 帧内全是 NULL：忽略它们后无值可聚合
                continue;
            }
            BigInteger sum = prefix[clampedHi + 1].subtract(prefix[clampedLo]);
            out[i][f] = Value.ofLong(toLongExact(sum, alias, sorted.get(i)));
        }
    }

    /**
     * 计算帧边界相对分区行 i 的下标（可能越界）。
     * 位移使用 long 运算避免 offset 极大时 int 加法溢出；
     * UNBOUNDED 直接钉在分区端点，越界值最后收缩到 int 极值，交由求交逻辑裁剪。
     */
    private static int frameIndex(int currentRow, FrameBound bound, int partitionSize) {
        long idx = switch (bound.type()) {
            case UNBOUNDED_PRECEDING -> 0L;
            case UNBOUNDED_FOLLOWING -> (long) partitionSize - 1;
            case CURRENT_ROW -> currentRow;
            case PRECEDING -> (long) currentRow - bound.offset();
            case FOLLOWING -> (long) currentRow + bound.offset();
        };
        if (idx < Integer.MIN_VALUE) {
            return Integer.MIN_VALUE;
        }
        if (idx > Integer.MAX_VALUE) {
            return Integer.MAX_VALUE;
        }
        return (int) idx;
    }

    private static long toLongExact(BigInteger value, String alias, Row contextRow) {
        try {
            return value.longValueExact();
        } catch (ArithmeticException e) {
            throw new EngineException(ErrorCode.OVERFLOW,
                    "SUM(" + alias + ") 窗口聚合结果 " + value
                            + " 超出 64 位有符号整数范围（行 sourceIndex="
                            + contextRow.sourceIndex() + "）", e);
        }
    }
}
