package incagg.store;

import java.math.BigDecimal;
import java.util.Map;
import java.util.TreeMap;

/**
 * 视图快照（不可变视图）：按类别的数量/金额和，外加连接不到维表的孤儿订单合计。
 */
public final class Snapshot {
    private final TreeMap<String, IncrementalViewStore.AggCell> categories;
    private final long orphanQty;
    private final BigDecimal orphanAmount;
    private final int orderCount;
    private final int productCount;
    private final int appliedEventCount;

    Snapshot(TreeMap<String, IncrementalViewStore.AggCell> categories,
             long orphanQty, BigDecimal orphanAmount,
             int orderCount, int productCount, int appliedEventCount) {
        this.categories = categories;
        this.orphanQty = orphanQty;
        this.orphanAmount = orphanAmount;
        this.orderCount = orderCount;
        this.productCount = productCount;
        this.appliedEventCount = appliedEventCount;
    }

    public Map<String, IncrementalViewStore.AggCell> categories() { return categories; }
    public long orphanQty() { return orphanQty; }
    public BigDecimal orphanAmount() { return orphanAmount; }
    public int orderCount() { return orderCount; }
    public int productCount() { return productCount; }
    public int appliedEventCount() { return appliedEventCount; }
}
