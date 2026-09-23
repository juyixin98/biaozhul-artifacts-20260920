package windowengine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 朴素逐行窗口参考实现（测试专用）。
 *
 * 刻意与生产引擎走完全不同的代码路径：
 * - 不用 Partitioner / RowComparator / WindowOperator，甚至不依赖 plan 包；
 * - 用冒泡式排序、逐行扫描的 O(n²) 最直白写法；
 * - 每行独立重算自己的分区归属、排名与帧区间和；
 * 生产引擎只要在大量随机场景上与本实现逐格相等，就有较强的正确性证据。
 *
 * 输入数据用 Object[] 行表示：元素只允许 Long / String / null。
 */
public final class NaiveReferenceWindow {

    /** 列名 -> 列下标。 */
    private final Map<String, Integer> columnIndex = new LinkedHashMap<>();
    private final List<Object[]> rows;

    // 窗口配置
    private final List<String> partitionCols = new ArrayList<>();
    private final List<String> orderCols = new ArrayList<>();
    private final List<Boolean> orderAsc = new ArrayList<>();
    private final List<Boolean> nullsFirst = new ArrayList<>();
    // 帧边界：相对当前行的位移语义；用两个标记表示 UNBOUNDED
    private boolean startUnbounded;
    private long startDelta;   // <=0（PRECEDING 为负，CURRENT ROW 为 0）
    private boolean endUnbounded;
    private long endDelta;     // >=0

    private String sumColumn;

    private NaiveReferenceWindow(List<String> columns, List<Object[]> rows) {
        this.rows = rows;
        for (int i = 0; i < columns.size(); i++) {
            columnIndex.put(columns.get(i), i);
        }
    }

    public static NaiveReferenceWindow of(List<String> columns, List<Object[]> rows) {
        return new NaiveReferenceWindow(new ArrayList<>(columns), rows);
    }

    public NaiveReferenceWindow partitionBy(String... cols) {
        partitionCols.clear();
        partitionCols.addAll(List.of(cols));
        return this;
    }

    /**
     * 添加排序键。
     * @param asc        ASC=true / DESC=false
     * @param nullFirst  NULLS FIRST=true / LAST=false
     */
    public NaiveReferenceWindow orderBy(String col, boolean asc, boolean nullFirst) {
        orderCols.add(col);
        orderAsc.add(asc);
        nullsFirst.add(nullFirst);
        return this;
    }

    /** 帧起点：UNBOUNDED PRECEDING。 */
    public NaiveReferenceWindow frameUnboundedTo(long endDelta) {
        this.startUnbounded = true;
        this.startDelta = Long.MIN_VALUE;
        this.endUnbounded = false;
        this.endDelta = endDelta;
        return this;
    }

    /** 帧终点：UNBOUNDED FOLLOWING。 */
    public NaiveReferenceWindow frameFromToUnbounded(long startDelta) {
        this.startUnbounded = false;
        this.startDelta = startDelta;
        this.endUnbounded = true;
        this.endDelta = Long.MAX_VALUE;
        return this;
    }

    /** 普通 [startDelta, endDelta] 帧，例如 [-1,1]。 */
    public NaiveReferenceWindow frame(long startDelta, long endDelta) {
        this.startUnbounded = false;
        this.startDelta = startDelta;
        this.endUnbounded = false;
        this.endDelta = endDelta;
        return this;
    }

    /** 整个分区（无 ORDER BY 默认帧）。 */
    public NaiveReferenceWindow frameWholePartition() {
        this.startUnbounded = true;
        this.startDelta = Long.MIN_VALUE;
        this.endUnbounded = true;
        this.endDelta = Long.MAX_VALUE;
        return this;
    }

    public NaiveReferenceWindow sumColumn(String col) {
        this.sumColumn = col;
        return this;
    }

    /** 计算结果：每个输入行返回一个三列结果 [ROW_NUMBER, RANK, SUM]，SUM 可能为 null。 */
    public List<Object[]> compute() {
        int n = rows.size();
        List<Object[]> result = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            result.add(new Object[3]);
        }

        // 1) 逐行判定分区归属（完全不用生产代码的 Partitioner）
        Map<List<Object>, List<Integer>> groups = new LinkedHashMap<>();
        for (int i = 0; i < n; i++) {
            List<Object> key = new ArrayList<>();
            for (String pc : partitionCols) {
                key.add(rows.get(i)[columnIndex.get(pc)]);
            }
            groups.computeIfAbsent(key, k -> new ArrayList<>()).add(i);
        }

        for (List<Integer> memberIndices : groups.values()) {
            List<Integer> sorted = naiveSort(memberIndices);
            int size = sorted.size();

            for (int pos = 0; pos < size; pos++) {
                int rowIdx = sorted.get(pos);
                // ROW_NUMBER：物理位置
                result.get(rowIdx)[0] = (long) (pos + 1);

                // RANK：1 + 严格排在我前面且与我不并列的行数
                long rank = 1;
                for (int j = 0; j < pos; j++) {
                    if (!peer(sorted.get(j), rowIdx)) {
                        rank++;
                    }
                }
                result.get(rowIdx)[1] = rank;

                // SUM：逐行重算帧区间
                result.get(rowIdx)[2] = naiveSum(pos, sorted);
            }
        }
        return result;
    }

    // ---------- 朴素排序（插入排序，不用引擎的比较器） ----------

    private List<Integer> naiveSort(List<Integer> memberIndices) {
        List<Integer> sorted = new ArrayList<>(memberIndices);
        for (int i = 1; i < sorted.size(); i++) {
            int x = sorted.get(i);
            int j = i - 1;
            while (j >= 0 && compareRows(sorted.get(j), x) > 0) {
                sorted.set(j + 1, sorted.get(j));
                j--;
            }
            sorted.set(j + 1, x);
        }
        return sorted;
    }

    /** 返回负数/0/正数。a 在 b 之前为负。 */
    private int compareRows(int aIdx, int bIdx) {
        for (int k = 0; k < orderCols.size(); k++) {
            int c = columnIndex.get(orderCols.get(k));
            Object a = rows.get(aIdx)[c];
            Object b = rows.get(bIdx)[c];

            int cmp;
            if (a == null || b == null) {
                if (a == null && b == null) {
                    cmp = 0;
                } else if (a == null) {
                    cmp = nullsFirst.get(k) ? -1 : 1;
                } else {
                    cmp = nullsFirst.get(k) ? 1 : -1;
                }
                // NULL 的位置只由 NULLS FIRST/LAST 决定，不被 ASC/DESC 翻转
            } else {
                if ((a instanceof Long) != (b instanceof Long)) {
                    throw new EngineException(ErrorCode.TYPE_MISMATCH,
                            "参考实现：跨类型比较 LONG 与 STRING");
                }
                if (a instanceof Long la) {
                    cmp = Long.compare(la, (Long) b);
                } else {
                    cmp = ((String) a).compareTo((String) b);
                }
                if (!orderAsc.get(k)) {
                    cmp = -cmp;
                }
            }
            if (cmp != 0) {
                return cmp;
            }
        }
        return Integer.compare(aIdx, bIdx); // 稳定兜底：原始行号
    }

    /** 并列判定：所有排序键值相等（null==null），与方向、NULL 位置无关。 */
    private boolean peer(int aIdx, int bIdx) {
        if (orderCols.isEmpty()) {
            return true;
        }
        for (String oc : orderCols) {
            int c = columnIndex.get(oc);
            Object a = rows.get(aIdx)[c];
            Object b = rows.get(bIdx)[c];
            if (a == null || b == null) {
                if (a != b) { // 一个 null 一个非 null
                    return false;
                }
                continue;
            }
            if (!a.equals(b)) {
                return false;
            }
        }
        return true;
    }

    // ---------- 朴素帧求和 ----------

    private Object naiveSum(int pos, List<Integer> sorted) {
        int n = sorted.size();
        long loLong = startUnbounded ? Long.MIN_VALUE : (long) pos + startDelta;
        long hiLong = endUnbounded ? Long.MAX_VALUE : (long) pos + endDelta;

        long lo = Math.max(loLong, 0L);
        long hi = Math.min(hiLong, (long) n - 1);
        if (loLong > hiLong || lo > hi) {
            return null; // 空帧
        }

        int sumCol = columnIndex.get(sumColumn);
        java.math.BigInteger sum = java.math.BigInteger.ZERO;
        boolean any = false;
        for (long p = lo; p <= hi; p++) {
            Object v = rows.get(sorted.get((int) p))[sumCol];
            if (v != null) {
                sum = sum.add(java.math.BigInteger.valueOf((Long) v));
                any = true;
            }
        }
        if (!any) {
            return null; // 帧内全 NULL
        }
        // 与生产端一致：数学结果超 long 即溢出
        return sum.longValueExact();
    }
}
