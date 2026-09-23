package vecq.test;

import vecq.IntColumn;
import vecq.NullBitmap;
import vecq.StringColumn;
import vecq.Table;

import java.util.List;

/** 测试用公共数据构造。 */
public final class Data {

    private Data() {}

    /**
     * 6 行订单表：
     * <pre>
     *  row  id    status
     *   0   10    NEW
     *   1   20    PAID
     *   2   30    NEW
     *   3   NULL  NULL
     *   4   50    PAID
     *   5   60    NEW
     * </pre>
     */
    public static Table orders() {
        int[] ids = {10, 20, 30, 0, 50, 60};
        NullBitmap idNulls = NullBitmap.fromNullRows(6, new int[]{3});
        IntColumn id = new IntColumn("id", ids, idNulls);

        String[] status = {"NEW", "PAID", "NEW", null, "PAID", "NEW"};
        NullBitmap stNulls = NullBitmap.fromNullRows(6, new int[]{3});
        StringColumn sc = new StringColumn("status", status, stNulls);

        return new Table("orders", List.of(id, sc));
    }

    /** n 行、所有列全部为 NULL 的表。 */
    public static Table allNulls(int n) {
        int[] zeros = new int[n];
        int[] nullRows = new int[n];
        for (int i = 0; i < n; i++) nullRows[i] = i;
        IntColumn id = new IntColumn("id", zeros, NullBitmap.fromNullRows(n, nullRows));
        String[] blanks = new String[n];
        StringColumn note = new StringColumn("note", blanks, NullBitmap.fromNullRows(n, nullRows.clone()));
        return new Table("all_nulls", List.of(id, note));
    }
}
