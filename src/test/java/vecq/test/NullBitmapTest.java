package vecq.test;

import vecq.InvalidQueryException;
import vecq.NullBitmap;

/** NULL 位图测试。 */
public final class NullBitmapTest {

    public static void run() {
        NullBitmap bm = NullBitmap.fromNullRows(10, new int[]{0, 3, 9});
        Assert.that(bm.isNull(0) && bm.isNull(3) && bm.isNull(9), "位图声明的 NULL 行为 null");
        Assert.that(bm.isPresent(1) && bm.isPresent(8), "未声明的行非 null");
        Assert.eqInt(3, bm.nullCount(), "nullCount 统计");
        Assert.eq(java.util.List.of(0, 3, 9), java.util.Arrays.stream(bm.nullRows())
                .boxed().toList(), "nullRows 有序输出");

        NullBitmap none = NullBitmap.allPresent(5);
        Assert.eqInt(0, none.nullCount(), "allPresent 无 NULL");

        Assert.fails(() -> NullBitmap.fromNullRows(3, new int[]{3}),
                InvalidQueryException.class, "NULL 行下标越界被拒绝");
        Assert.fails(() -> NullBitmap.fromNullRows(3, new int[]{-1}),
                InvalidQueryException.class, "NULL 行负下标被拒绝");
    }
}
