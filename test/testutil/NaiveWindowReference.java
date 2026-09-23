package testutil;

import engine.model.ColumnType;
import engine.model.Relation;
import engine.model.Relation.Row;
import engine.window.BoundKind;
import engine.window.Frame;
import engine.window.FunctionSpec;
import engine.window.NullOrder;
import engine.window.OrderKey;
import engine.window.WindowException;
import engine.window.WindowFunction;
import engine.window.WindowSpec;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 朴素逐行窗口参考实现：刻意写得“直白、O(n²)、与生产引擎不同构”，
 * 作为差分测试（differential testing）的对照基准。
 *
 * <p>做法：用 HashMap 按分区键值分组 → 各组用独立编写的比较器排序 →
 * ROW_NUMBER/RANK 逐行扫描 → SUM 对每行用双重 for 循环在帧内逐行累加。
 *
 * <p>溢出契约与生产引擎一致：帧内非 NULL 整数的“数学和”超出 long
 * 范围即报错（这里逐行 {@link BigInteger#add} 后一次性 longValueExact，
 * 不用前缀和），因此两侧在负值抵消场景下结论相同。
 */
public final class NaiveWindowReference {

    private Relation input;
    private WindowSpec spec;

    public Relation run(Relation input, WindowSpec spec) {
        this.input = input;
        this.spec = spec;

        // 1) 按分区键值朴素分组
        Map<List<Object>, List<Row>> groups = new LinkedHashMap<>();
        List<Integer> partCols = new ArrayList<>();
        for (String p : spec.partitionBy()) {
            partCols.add(input.columnIndex(p));
        }
        for (Row row : input.rows()) {
            List<Object> key = new ArrayList<>();
            for (int c : partCols) {
                key.add(row.get(c));
            }
            groups.computeIfAbsent(key, k -> new ArrayList<>()).add(row);
        }

        // 2) 输出列
        List<String> outNames = new ArrayList<>(input.columnNames());
        List<ColumnType> outTypes = new ArrayList<>(input.columnTypes());
        for (FunctionSpec f : spec.functions()) {
            outNames.add(f.outputColumn());
            outTypes.add(ColumnType.LONG);
        }

        // 3) 每组排序后逐行求值，直接产出结果行
        List<Row> out = new ArrayList<>();
        for (List<Row> group : groups.values()) {
            group.sort(groupComparator());
            for (int i = 0; i < group.size(); i++) {
                Object[] values = new Object[outNames.size()];
                Row src = group.get(i);
                System.arraycopy(src.values, 0, values, 0, input.columnCount());
                int col = input.columnCount();
                for (FunctionSpec f : spec.functions()) {
                    values[col++] = eval(f, group, i);
                }
                out.add(new Row(src.sourceIndex, values));
            }
        }
        return new Relation(outNames, outTypes, out);
    }

    private Object eval(FunctionSpec f, List<Row> group, int i) {
        switch (f.function()) {
            case ROW_NUMBER:
                return (long) i + 1;
            case RANK:
                return naiveRank(group, i);
            case SUM:
                return naiveSum(f, group, i);
            default:
                throw new AssertionError();
        }
    }

    private long naiveRank(List<Row> group, int i) {
        // 名次 = 排序键严格小于当前行的行数 + 1。
        // 注意：必须只按 ORDER BY 键比较，不能含 sourceIndex 决胜键，
        // 否则并列键永远不会被判为相等。
        long rank = 1;
        for (int j = 0; j < i; j++) {
            if (orderKeysCompare(group.get(j), group.get(i)) < 0) {
                rank++;
            }
        }
        return rank;
    }

    private Object naiveSum(FunctionSpec f, List<Row> group, int i) {
        int arg = input.columnIndex(f.argumentColumn());
        Frame frame = f.frame();
        long loRaw = resolveIndex(frame.start(), i);
        long hiRaw = resolveIndex(frame.end(), i);
        int n = group.size();
        if (hiRaw < 0 || loRaw > n - 1) {
            return null; // 整帧在分区外
        }
        int lo = (int) Math.max(0, Math.min(n - 1, loRaw));
        int hi = (int) Math.max(0, Math.min(n - 1, hiRaw));

        BigInteger acc = BigInteger.ZERO;
        boolean any = false;
        for (int j = lo; j <= hi; j++) {
            Object v = group.get(j).get(arg);
            if (v != null) {
                acc = acc.add(BigInteger.valueOf((Long) v));
                any = true;
            }
        }
        if (!any) {
            return null;
        }
        try {
            return acc.longValueExact();
        } catch (ArithmeticException e) {
            throw new WindowException(String.format(
                    "[参考实现] 窗口求和溢出 long：SUM(%s) AS %s，帧 %s，第 %d/%d 行，和 = %s",
                    f.argumentColumn(), f.outputColumn(), frame.toSql(), i + 1, n, acc));
        }
    }

    private long resolveIndex(engine.window.FrameBound b, int current) {
        return switch (b.kind()) {
            case UNBOUNDED_PRECEDING -> Long.MIN_VALUE;
            case PRECEDING -> {
                long r = current - b.offset();
                yield r > current ? Long.MIN_VALUE : r;
            }
            case CURRENT_ROW -> current;
            case FOLLOWING -> {
                long r = current + b.offset();
                yield r < current ? Long.MAX_VALUE : r;
            }
            case UNBOUNDED_FOLLOWING -> Long.MAX_VALUE;
        };
    }

    private Comparator<Row> groupComparator() {
        return (a, b) -> {
            for (OrderKey k : spec.orderBy()) {
                int col = input.columnIndex(k.column());
                Object x = a.get(col);
                Object y = b.get(col);
                int cmp;
                if (x == null && y == null) {
                    cmp = 0;
                } else if (x == null) {
                    cmp = k.nullOrder() == NullOrder.NULLS_FIRST ? -1 : 1;
                } else if (y == null) {
                    cmp = k.nullOrder() == NullOrder.NULLS_FIRST ? 1 : -1;
                } else {
                    cmp = cellCompare(x, y);
                    if (!k.ascending()) {
                        cmp = -cmp;
                    }
                }
                if (cmp != 0) {
                    return cmp;
                }
            }
            return Long.compare(a.sourceIndex, b.sourceIndex);
        };
    }

    private int orderCompare(Row a, Row b) {
        return groupComparator().compare(a, b);
    }

    /** 仅按 ORDER BY 键比较（不含 sourceIndex），用于 RANK 的并列判定。 */
    private int orderKeysCompare(Row a, Row b) {
        for (OrderKey k : spec.orderBy()) {
            int col = input.columnIndex(k.column());
            Object x = a.get(col);
            Object y = b.get(col);
            int cmp;
            if (x == null && y == null) {
                cmp = 0;
            } else if (x == null) {
                cmp = k.nullOrder() == NullOrder.NULLS_FIRST ? -1 : 1;
            } else if (y == null) {
                cmp = k.nullOrder() == NullOrder.NULLS_FIRST ? 1 : -1;
            } else {
                cmp = cellCompare(x, y);
                if (!k.ascending()) {
                    cmp = -cmp;
                }
            }
            if (cmp != 0) {
                return cmp;
            }
        }
        return 0;
    }

    @SuppressWarnings("unchecked")
    private static int cellCompare(Object a, Object b) {
        if (a instanceof Long && b instanceof Long) {
            return Long.compare((Long) a, (Long) b);
        }
        return ((Comparable<Object>) a).compareTo(b);
    }

    /** 仅供测试统计：函数类型枚举引用便捷方法。 */
    public static boolean isSum(WindowFunction f) {
        return f == WindowFunction.SUM;
    }
}
