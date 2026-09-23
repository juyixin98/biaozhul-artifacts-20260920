package vecq;

import java.util.Arrays;

/**
 * 选择向量（Selection Vector）：存放“下一批算子应当处理的源表行下标”。
 *
 * 关键性质：
 *  - 下标可以稀疏（过滤后只保留少数行）；
 *  - 下标可以重复（OR 分支合并 / 显式 selection 输入），重复即代表该行要被处理多次，
 *    因此投影与聚合基于“多重集（multiset）”语义；
 *  - 下标有序（构造时排序，保持稳定输出顺序；相等的重复下标相邻保留）；
 *  - 惰性全选（{@link #lazyAll}）不占内存，真正物化时才生成 [0, rowCount)。
 *
 * 越界与负下标在“加入向量”的那一刻就被拒绝（fail-fast），
 * 从而覆盖“无效选择下标”这一验收项。
 */
public final class SelectionVector {

    /** 惰性全选标记；indices == null 表示当前逻辑上为 [0,rowCount)。 */
    private int[] indices;
    private int size;
    private final int rowCount;

    private SelectionVector(int rowCount, int[] indices, int size, boolean materialized) {
        if (rowCount < 0) throw new InvalidSelectionException("rowCount 不能为负");
        this.rowCount = rowCount;
        this.indices = materialized ? indices : null;
        this.size = size;
    }

    /** 惰性全选：逻辑上为 0..rowCount-1，物化前不分配数组。 */
    public static SelectionVector lazyAll(int rowCount) {
        return new SelectionVector(rowCount, null, rowCount, false);
    }

    /** 包装一个空结果。 */
    public static SelectionVector empty(int rowCount) {
        return new SelectionVector(rowCount, new int[0], 0, true);
    }

    /**
     * 从外部显式给入的下标数组构造（请求中的 "selection" 字段）。
     * 复制后排序；保留重复下标；拒绝负数 / 越界下标。
     */
    public static SelectionVector wrapSorted(int rowCount, int[] raw) {
        int[] copy = Arrays.copyOf(raw, raw.length);
        Arrays.sort(copy);
        validate(rowCount, copy);
        return new SelectionVector(rowCount, copy, copy.length, true);
    }

    /** 从已校验、已排序的数组构造（内部及测试使用）。 */
    public static SelectionVector ofSorted(int rowCount, int[] sorted, int size) {
        return new SelectionVector(rowCount, sorted, size, true);
    }

    static void validate(int rowCount, int[] idx) {
        for (int j : idx) {
            if (j < 0 || j >= rowCount) {
                throw new InvalidSelectionException(
                        "无效选择下标 " + j + "：必须落在 [0, " + (rowCount - 1)
                                + "]（表共 " + rowCount + " 行）");
            }
        }
    }

    public boolean isLazy() {
        return indices == null;
    }

    public int size() {
        return size;
    }

    public int rowCount() {
        return rowCount;
    }

    public int get(int i) {
        materialize();
        return indices[i];
    }

    /** 物化为 int[]（惰性全选时才真正分配并填充）。 */
    public int[] toArray() {
        materialize();
        return Arrays.copyOf(indices, size);
    }

    private void materialize() {
        if (indices != null) return;
        int[] all = new int[rowCount];
        for (int i = 0; i < rowCount; i++) all[i] = i;
        indices = all; // size 已在构造时设为 rowCount
    }

    /**
     * 合并两个有序选择向量（多重集并集：重复下标按出现次数累加）。
     * 用于 OR 顶层分支各自过滤后的结果合并。合并结果保持有序。
     */
    public static SelectionVector union(SelectionVector a, SelectionVector b) {
        if (a.rowCount != b.rowCount) {
            throw new InvalidSelectionException("合并的两个选择向量基于不同的表行数（"
                    + a.rowCount + " vs " + b.rowCount + "）");
        }
        int[] x = a.toArray();
        int[] y = b.toArray();
        int[] out = new int[a.size + b.size];
        int i = 0, j = 0, k = 0;
        while (i < x.length && j < y.length) {
            if (x[i] <= y[j]) out[k++] = x[i++];
            else out[k++] = y[j++];
        }
        while (i < x.length) out[k++] = x[i++];
        while (j < y.length) out[k++] = y[j++];
        return ofSorted(a.rowCount, out, k);
    }

    @Override
    public String toString() {
        if (indices == null) return "SelectionVector(lazyAll[" + rowCount + "])";
        return "SelectionVector" + Arrays.toString(Arrays.copyOf(indices, size));
    }
}
