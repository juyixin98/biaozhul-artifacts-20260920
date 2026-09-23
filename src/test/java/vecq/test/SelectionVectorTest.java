package vecq.test;

import vecq.InvalidSelectionException;
import vecq.SelectionVector;

import java.util.Arrays;

/** 选择向量：惰性全选、显式构造、重复、越界、多集合并。 */
public final class SelectionVectorTest {

    public static void run() {
        SelectionVector lazy = SelectionVector.lazyAll(6);
        Assert.that(lazy.isLazy(), "lazyAll 初始为惰性");
        Assert.eqInt(6, lazy.size(), "lazyAll 大小为行数");
        Assert.eq(Arrays.asList(0, 1, 2, 3, 4, 5), boxed(lazy.toArray()),
                "惰性全选物化后为 [0..n)");
        Assert.that(!lazy.isLazy(), "toArray 后被物化");

        SelectionVector wrap = SelectionVector.wrapSorted(6, new int[]{5, 0, 2, 2});
        Assert.eq(Arrays.asList(0, 2, 2, 5), boxed(wrap.toArray()),
                "显式下标排序且保留重复");

        SelectionVector sorted = SelectionVector.ofSorted(6, new int[]{1, 1, 4}, 3);
        Assert.eq(Arrays.asList(1, 1, 4), boxed(sorted.toArray()), "ofSorted 保留重复");

        Assert.fails(() -> SelectionVector.wrapSorted(6, new int[]{6}),
                InvalidSelectionException.class, "下标 == rowCount 越界被拒绝");
        Assert.fails(() -> SelectionVector.wrapSorted(6, new int[]{-1}),
                InvalidSelectionException.class, "负下标被拒绝");
        Assert.fails(() -> SelectionVector.wrapSorted(6, new int[]{1, 100}),
                InvalidSelectionException.class, "重复列表中夹带越界下标也被拒绝");

        // 多集合并：[0,2,2,5] U [1,2,5,5] = [0,1,2,2,2,5,5,5]
        SelectionVector a = SelectionVector.wrapSorted(6, new int[]{5, 0, 2, 2});
        SelectionVector b = SelectionVector.wrapSorted(6, new int[]{5, 1, 2, 5});
        SelectionVector u = SelectionVector.union(a, b);
        Assert.eq(Arrays.asList(0, 1, 2, 2, 2, 5, 5, 5), boxed(u.toArray()),
                "多集并集保留两侧重复次数并保持有序");

        // 与惰性全选合并
        SelectionVector u2 = SelectionVector.union(SelectionVector.lazyAll(3),
                SelectionVector.wrapSorted(3, new int[]{2}));
        Assert.eq(Arrays.asList(0, 1, 2, 2), boxed(u2.toArray()),
                "惰性全选参与合并时先物化");

        SelectionVector empty = SelectionVector.empty(6);
        Assert.eqInt(0, empty.size(), "空选择向量大小为 0");
        Assert.eq(java.util.List.of(), boxed(empty.toArray()), "空选择向量无下标");
    }

    private static java.util.List<Integer> boxed(int[] arr) {
        return Arrays.stream(arr).boxed().toList();
    }
}
