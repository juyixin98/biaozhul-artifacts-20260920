package windowengine.engine;

import windowengine.Row;
import windowengine.Schema;
import windowengine.Value;
import windowengine.plan.Direction;
import windowengine.plan.NullOrder;
import windowengine.plan.OrderKey;

import java.util.Comparator;
import java.util.List;

/**
 * 按 ORDER BY 键列表比较两行。
 *
 * 排序规则：
 * 1. NULL 不参与大小比较，按 nullOrder 独立成组（FIRST 恒小于任何非 NULL，LAST 恒大）；
 * 2. 非 NULL 的同类型值按 ASC/DESC 比较；LONG 与 STRING 之间比较抛 TYPE_MISMATCH；
 * 3. 全部排序键相等时，按原始行号升序兜底，保证结果确定性（ROW_NUMBER 的并列打破规则）。
 */
public final class RowComparator implements Comparator<Row> {

    private final int[] columns;
    private final Direction[] directions;
    private final NullOrder[] nullOrders;

    public RowComparator(Schema schema, List<OrderKey> keys) {
        int n = keys.size();
        this.columns = new int[n];
        this.directions = new Direction[n];
        this.nullOrders = new NullOrder[n];
        for (int i = 0; i < n; i++) {
            OrderKey key = keys.get(i);
            columns[i] = schema.requireIndex(key.column());
            directions[i] = key.direction();
            nullOrders[i] = key.nullOrder();
        }
    }

    @Override
    public int compare(Row a, Row b) {
        for (int i = 0; i < columns.length; i++) {
            Value va = a.get(columns[i]);
            Value vb = b.get(columns[i]);
            int cmp;
            if (va.isNull() || vb.isNull()) {
                // NULL 的位置只由 NULLS FIRST/LAST 决定，绝不随 ASC/DESC 翻转
                if (va.isNull() && vb.isNull()) {
                    cmp = 0;
                } else {
                    boolean aIsNull = va.isNull();
                    boolean nullShouldBeFirst = nullOrders[i] == NullOrder.FIRST;
                    cmp = (aIsNull == nullShouldBeFirst) ? -1 : 1;
                }
            } else {
                cmp = va.compareNonNull(vb);
                if (directions[i] == Direction.DESC) {
                    cmp = -cmp;
                }
            }
            if (cmp != 0) {
                return cmp;
            }
        }
        // 确定性兜底：稳定排序 + 原始行号
        return Integer.compare(a.sourceIndex(), b.sourceIndex());
    }
}
