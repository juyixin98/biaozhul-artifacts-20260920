package engine.window;

import engine.model.ColumnType;
import engine.model.Relation;
import engine.model.Relation.Row;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 单机内存窗口算子执行引擎（纯 Java 实现，不调用任何 SQL 引擎）。
 *
 * <h3>执行步骤</h3>
 * <ol>
 *   <li>校验窗口规格（列存在、输出列不重名、SUM 参数为整数列等）；</li>
 *   <li>一次稳定排序：先按分区键、再按各排序键（含 NULL 顺序）、
 *       最后按输入原始位置，保证并列键结果确定；</li>
 *   <li>切出连续分区，逐分区计算 ROW_NUMBER / RANK / 滑动 ROWS 帧求和；</li>
 *   <li>输出顺序 = 分区键顺序 + 分区内排序顺序。</li>
 * </ol>
 *
 * <h3>溢出处理</h3>
 * SUM 用 {@link BigInteger} 前缀和求“整窗数学和”，再用
 * {@code longValueExact()} 做一次性范围检查：窗口内非 NULL 整数之和
 * 只要落在 long 范围外（无论中间累加是否回绕）即抛出
 * {@link WindowException}，异常携带源行号、分区内位置与帧定义。
 * 逐操作版本见 {@link CheckedLong}，测试中两条路径交叉验证。
 */
public final class WindowEngine {

    /** 执行窗口规格，返回在输入列之后追加结果列的新关系。 */
    public Relation execute(Relation input, WindowSpec spec) {
        validate(input, spec);

        List<Row> ordered = new ArrayList<>(input.rows());
        Comparator<Row> partitionCmp = partitionComparator(input, spec.partitionBy());
        Comparator<Row> fullCmp = partitionCmp
                .thenComparing(orderComparator(input, spec.orderBy()))
                .thenComparingLong(r -> r.sourceIndex);
        ordered.sort(fullCmp);

        // 输出列 = 原列 + 每个函数的结果列（全部为 LONG）
        List<String> outNames = new ArrayList<>(input.columnNames());
        List<ColumnType> outTypes = new ArrayList<>(input.columnTypes());
        for (FunctionSpec f : spec.functions()) {
            outNames.add(f.outputColumn());
            outTypes.add(ColumnType.LONG);
        }

        List<Row> outRows = new ArrayList<>(ordered.size());
        int from = 0;
        while (from < ordered.size()) {
            int to = from + 1;
            while (to < ordered.size()
                    && partitionEquals(input, spec.partitionBy(), ordered.get(from), ordered.get(to))) {
                to++;
            }
            processPartition(input, spec, ordered.subList(from, to), outRows);
            from = to;
        }
        return new Relation(outNames, outTypes, outRows);
    }

    // ------------------------------------------------------------------
    // 规格校验
    // ------------------------------------------------------------------

    private void validate(Relation input, WindowSpec spec) {
        for (String c : spec.partitionBy()) {
            input.columnIndex(c);
        }
        for (OrderKey k : spec.orderBy()) {
            input.columnIndex(k.column());
        }
        List<String> allNames = new ArrayList<>(input.columnNames());
        for (FunctionSpec f : spec.functions()) {
            if (allNames.contains(f.outputColumn())) {
                throw new IllegalArgumentException("输出列名与已有列重复: " + f.outputColumn());
            }
            allNames.add(f.outputColumn());
            switch (f.function()) {
                case ROW_NUMBER, RANK -> {
                    if (f.argumentColumn() != null || f.frame() != null) {
                        throw new IllegalArgumentException(
                                f.function() + " 不接受参数或帧定义");
                    }
                }
                case SUM -> {
                    if (f.argumentColumn() == null) {
                        throw new IllegalArgumentException("SUM 必须指定聚合参数列");
                    }
                    int arg = input.columnIndex(f.argumentColumn());
                    if (input.columnTypes().get(arg) != ColumnType.LONG) {
                        throw new IllegalArgumentException(
                                "SUM 的参数列必须是整数列: " + f.argumentColumn());
                    }
                    if (f.frame() == null) {
                        throw new IllegalArgumentException("SUM 必须指定 ROWS 帧");
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // 分区处理
    // ------------------------------------------------------------------

    private void processPartition(Relation input, WindowSpec spec,
                                  List<Row> part, List<Row> outRows) {
        int fnCount = spec.functions().size();
        // results[i] = 第 i 个函数在本分区每行的结果（允许 null）
        Object[][] results = new Object[fnCount][part.size()];

        for (int fi = 0; fi < fnCount; fi++) {
            FunctionSpec f = spec.functions().get(fi);
            switch (f.function()) {
                case ROW_NUMBER -> {
                    for (int i = 0; i < part.size(); i++) {
                        results[fi][i] = (long) i + 1;
                    }
                }
                case RANK -> {
                    Comparator<Row> orderCmp = orderComparator(input, spec.orderBy());
                    long rank = 1;
                    for (int i = 0; i < part.size(); i++) {
                        if (i > 0 && orderCmp.compare(part.get(i - 1), part.get(i)) != 0) {
                            rank = i + 1L; // RANK 有跳跃：新组的名次 = 物理位置（1 起算）
                        }
                        results[fi][i] = rank;
                    }
                }
                case SUM -> computeSum(input, f, part, results[fi]);
            }
        }

        for (int i = 0; i < part.size(); i++) {
            Row src = part.get(i);
            Object[] values = new Object[input.columnCount() + fnCount];
            System.arraycopy(src.values, 0, values, 0, input.columnCount());
            for (int fi = 0; fi < fnCount; fi++) {
                values[input.columnCount() + fi] = results[fi][i];
            }
            outRows.add(new Row(src.sourceIndex, values));
        }
    }

    /**
     * 滑动 ROWS 帧求和。前缀和下标 ps[0]=0、ps[k]= 前 k 行非 NULL 值之和，
     * 帧内只要存在非 NULL 值则结果非 NULL（SQL 中 SUM 忽略 NULL）。
     */
    private void computeSum(Relation input, FunctionSpec f, List<Row> part, Object[] out) {
        int argCol = input.columnIndex(f.argumentColumn());
        int n = part.size();

        BigInteger[] ps = new BigInteger[n + 1];
        int[] nonNullPrefix = new int[n + 1];
        ps[0] = BigInteger.ZERO;
        for (int i = 0; i < n; i++) {
            Object v = part.get(i).get(argCol);
            ps[i + 1] = ps[i].add(v == null ? BigInteger.ZERO
                    : BigInteger.valueOf(((Long) v).longValue()));
            nonNullPrefix[i + 1] = nonNullPrefix[i] + (v == null ? 0 : 1);
        }

        Frame frame = f.frame();
        for (int i = 0; i < n; i++) {
            long rawLo = frame.start().absoluteIndex(i);
            long rawHi = frame.end().absoluteIndex(i);
            // 越界截断到分区边界
            int lo = (int) Math.max(0, Math.min(n - 1, rawLo));
            int hi = (int) Math.max(0, Math.min(n - 1, rawHi));

            // 整帧都落在分区之外（例如大偏移的 N PRECEDING 在分区头几行）：
            // 截断后的交集为空，聚合结果为 NULL
            if (rawHi < 0 || rawLo > n - 1) {
                out[i] = null;
                continue;
            }

            int nonNullCount = nonNullPrefix[hi + 1] - nonNullPrefix[lo];
            if (nonNullCount == 0) {
                out[i] = null; // 帧内没有任何非 NULL 值
                continue;
            }
            BigInteger total = ps[hi + 1].subtract(ps[lo]);
            try {
                out[i] = total.longValueExact();
            } catch (ArithmeticException e) {
                throw new WindowException(String.format(
                        "窗口求和溢出 long 范围：函数 SUM(%s) AS %s，帧 %s，"
                                + "源行号 %d，分区内第 %d 行（共 %d 行），帧内非空整数和 = %s",
                        f.argumentColumn(), f.outputColumn(), frame.toSql(),
                        part.get(i).sourceIndex + 1, i + 1, n, total));
            }
        }
    }

    // ------------------------------------------------------------------
    // 比较器
    // ------------------------------------------------------------------

    /** 分区比较器：仅用于把同分区行聚到一起、并给出确定的分区先后；NULL 视为相等的一组。 */
    private Comparator<Row> partitionComparator(Relation input, List<String> keys) {
        List<Integer> cols = resolveColumns(input, keys);
        return (a, b) -> {
            for (int col : cols) {
                int c = compareValues(a.get(col), b.get(col));
                if (c != 0) {
                    return c;
                }
            }
            return 0;
        };
    }

    /** 排序键比较器：逐键比较，应用 ASC/DESC 与显式 NULL 顺序。 */
    private Comparator<Row> orderComparator(Relation input, List<OrderKey> keys) {
        return (a, b) -> {
            for (OrderKey key : keys) {
                int col = input.columnIndex(key.column());
                Object va = a.get(col);
                Object vb = b.get(col);
                int c;
                if (va == null && vb == null) {
                    c = 0;
                } else if (va == null) {
                    c = key.nullOrder() == NullOrder.NULLS_FIRST ? -1 : 1;
                } else if (vb == null) {
                    c = key.nullOrder() == NullOrder.NULLS_FIRST ? 1 : -1;
                } else {
                    c = compareValues(va, vb);
                    if (!key.ascending()) {
                        c = -c;
                    }
                }
                if (c != 0) {
                    return c;
                }
            }
            return 0;
        };
    }

    private boolean partitionEquals(Relation input, List<String> keys, Row a, Row b) {
        for (String key : keys) {
            int col = input.columnIndex(key);
            if (compareValues(a.get(col), b.get(col)) != 0) {
                return false;
            }
        }
        return true;
    }

    private static List<Integer> resolveColumns(Relation input, List<String> names) {
        List<Integer> cols = new ArrayList<>(names.size());
        for (String n : names) {
            cols.add(input.columnIndex(n));
        }
        return cols;
    }

    /** null 安全、类型感知的值比较；仅在同类型值间调用（列类型固定）。 */
    @SuppressWarnings("unchecked")
    private static int compareValues(Object a, Object b) {
        if (a == null && b == null) {
            return 0;
        }
        if (a == null) {
            return -1;
        }
        if (b == null) {
            return 1;
        }
        if (a instanceof Long && b instanceof Long) {
            return Long.compare((Long) a, (Long) b);
        }
        if (a instanceof String && b instanceof String) {
            return ((String) a).compareTo((String) b);
        }
        // 理论上不会发生：schema 保证每列类型一致
        throw new IllegalArgumentException("无法比较不同类型的值: " + a + " 与 " + b);
    }
}
