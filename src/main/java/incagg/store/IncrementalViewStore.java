package incagg.store;

import incagg.model.Event;
import incagg.model.EventType;
import incagg.model.OrderLine;
import incagg.model.Product;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;

/**
 * 增量连接聚合视图的全部状态与维护逻辑，线程安全。
 *
 * 视图定义：
 *   orders ⋈ products（on productId）按 category 分组，
 *   聚合 qty 之和、amount 之和（BigDecimal，2 位小数定点）。
 *   连接不到维表的订单行计入“孤儿”，不属于任何类别。
 *
 * 事件 ID 去重范围（重要）：
 *   - 全局范围，跨双边（订单/商品）共用一个 eventId 命名空间；
 *   - 以 eventId 精确匹配，事件一旦“应用”（含被语义忽略的删除）即记入去重日志；
 *   - 相同 eventId 重放：直接跳过，不看载荷；若载荷与首次不同，标记 conflict=true
 *     （仍跳过 —— 首达事件获胜 first-writer-wins）。
 */
public final class IncrementalViewStore {

    /** 一个类别的聚合单元。 */
    public static final class AggCell {
        public long qty = 0;
        public BigDecimal amount = BigDecimal.ZERO.setScale(2);

        public AggCell copy() {
            AggCell c = new AggCell();
            c.qty = qty;
            c.amount = amount;
            return c;
        }
    }

    /** 事件应用结果。 */
    public static final class ApplyResult {
        /** APPLIED：首次见到并执行；DUPLICATE：eventId 已存在，本次跳过。 */
        public final String status;
        /** 语义层面是否无动作（如删除不存在的记录）。 */
        public final boolean ignored;
        /** 重复 eventId 但载荷与首次不同。 */
        public final boolean conflict;
        public final String detail;

        private ApplyResult(String status, boolean ignored, boolean conflict, String detail) {
            this.status = status;
            this.ignored = ignored;
            this.conflict = conflict;
            this.detail = detail;
        }

        static ApplyResult applied(boolean ignored, String detail) {
            return new ApplyResult("APPLIED", ignored, false, detail);
        }
        static ApplyResult duplicate(boolean conflict, String detail) {
            return new ApplyResult("DUPLICATE", true, conflict, detail);
        }
    }

    private record AppliedEvent(String fingerprint) {}

    private final Map<Long, OrderLine> orders = new LinkedHashMap<>();
    private final Map<Long, Product> products = new LinkedHashMap<>();
    private final Map<String, AppliedEvent> appliedEvents = new LinkedHashMap<>();
    private final TreeMap<String, AggCell> cells = new TreeMap<>();
    private long orphanQty;
    private BigDecimal orphanAmount = BigDecimal.ZERO.setScale(2);

    // ---------------------------------------------------------------- 事件应用

    public synchronized ApplyResult apply(Event e) {
        e.validate();
        AppliedEvent first = appliedEvents.get(e.eventId);
        if (first != null) {
            String fp = fingerprint(e);
            boolean conflict = !first.fingerprint().equals(fp);
            return ApplyResult.duplicate(conflict,
                    conflict ? "eventId 已应用且载荷不同，仍按首达事件跳过" : "eventId 已应用，跳过");
        }

        ApplyResult r = switch (e.type) {
            case ORDER_UPSERT -> applyOrderUpsert(e);
            case ORDER_DELETE -> applyOrderDelete(e);
            case PRODUCT_UPSERT -> applyProductUpsert(e);
            case PRODUCT_DELETE -> applyProductDelete(e);
        };
        appliedEvents.put(e.eventId, new AppliedEvent(fingerprint(e)));
        return r;
    }

    private ApplyResult applyOrderUpsert(Event e) {
        long id = e.orderLineId;
        OrderLine fresh = new OrderLine(id, e.productId, e.qty, e.amount);
        OrderLine old = orders.get(id);
        if (old != null) {
            subtractOrder(old);
            orders.put(id, fresh);
            addOrder(fresh);
            return ApplyResult.applied(false, "订单行 " + id + " 已更新，旧贡献剔除、新贡献计入");
        }
        orders.put(id, fresh);
        addOrder(fresh);
        return ApplyResult.applied(false, "订单行 " + id + " 已插入");
    }

    private ApplyResult applyOrderDelete(Event e) {
        long id = e.orderLineId;
        OrderLine old = orders.remove(id);
        if (old == null) {
            // 删除不存在的记录：幂等无动作，但 eventId 仍进入去重日志。
            return ApplyResult.applied(true, "订单行 " + id + " 不存在，删除为空操作（幂等）");
        }
        subtractOrder(old);
        return ApplyResult.applied(false, "订单行 " + id + " 已删除，其贡献剔除");
    }

    private ApplyResult applyProductUpsert(Event e) {
        long pid = e.productId;
        Product fresh = new Product(pid, e.category);
        Product old = products.get(pid);
        if (old == null) {
            products.put(pid, fresh);
            int matched = rejoinOrphansOf(pid, fresh.category);
            return ApplyResult.applied(false,
                    "商品 " + pid + " 已插入到类别 " + fresh.category
                            + "，" + matched + " 条既有订单行由孤儿加入聚合");
        }
        if (old.category.equals(fresh.category)) {
            return ApplyResult.applied(true,
                    "商品 " + pid + " 已存在且类别未变（" + old.category + "），空操作");
        }
        // 维表分类变更：把引用该商品的全部订单行从旧类别迁到新类别。
        long movedQty = 0;
        BigDecimal movedAmount = BigDecimal.ZERO.setScale(2);
        for (OrderLine ol : orders.values()) {
            if (ol.productId == pid) {
                cell(old.category).qty -= ol.qty;
                cell(old.category).amount = cell(old.category).amount.subtract(ol.amount);
                cell(fresh.category).qty += ol.qty;
                cell(fresh.category).amount = cell(fresh.category).amount.add(ol.amount);
                movedQty += ol.qty;
                movedAmount = movedAmount.add(ol.amount);
            }
        }
        products.put(pid, fresh);
        prune(old.category);
        return ApplyResult.applied(false,
                "商品 " + pid + " 类别 " + old.category + " -> " + fresh.category
                        + "，迁移数量 " + movedQty + "、金额 " + movedAmount.toPlainString());
    }

    private ApplyResult applyProductDelete(Event e) {
        long pid = e.productId;
        Product old = products.remove(pid);
        if (old == null) {
            return ApplyResult.applied(true, "商品 " + pid + " 不存在，删除为空操作（幂等）");
        }
        int detached = 0;
        for (OrderLine ol : orders.values()) {
            if (ol.productId == pid) {
                cell(old.category).qty -= ol.qty;
                cell(old.category).amount = cell(old.category).amount.subtract(ol.amount);
                orphanQty += ol.qty;
                orphanAmount = orphanAmount.add(ol.amount);
                detached++;
            }
        }
        prune(old.category);
        return ApplyResult.applied(false,
                "商品 " + pid + " 已删除，" + detached + " 条订单行脱离类别 "
                        + old.category + " 成为孤儿");
    }

    /** 新商品（或重新插入的商品）使该 productId 的孤儿订单重新入聚合。 */
    private int rejoinOrphansOf(long pid, String category) {
        int matched = 0;
        for (OrderLine ol : orders.values()) {
            if (ol.productId == pid) {
                cell(category).qty += ol.qty;
                cell(category).amount = cell(category).amount.add(ol.amount);
                orphanQty -= ol.qty;
                orphanAmount = orphanAmount.subtract(ol.amount);
                matched++;
            }
        }
        return matched;
    }

    // ---------------------------------------------------------------- 贡献增减

    private void addOrder(OrderLine ol) {
        Product p = products.get(ol.productId);
        if (p == null) {
            orphanQty += ol.qty;
            orphanAmount = orphanAmount.add(ol.amount);
        } else {
            cell(p.category).qty += ol.qty;
            cell(p.category).amount = cell(p.category).amount.add(ol.amount);
        }
    }

    private void subtractOrder(OrderLine ol) {
        Product p = products.get(ol.productId);
        if (p == null) {
            orphanQty -= ol.qty;
            orphanAmount = orphanAmount.subtract(ol.amount);
        } else {
            cell(p.category).qty -= ol.qty;
            cell(p.category).amount = cell(p.category).amount.subtract(ol.amount);
            prune(p.category);
        }
    }

    private AggCell cell(String category) {
        return cells.computeIfAbsent(category, k -> new AggCell());
    }

    /** 数量金额均归零的类别及时清掉，快照与全量重算口径一致。 */
    private void prune(String category) {
        AggCell c = cells.get(category);
        if (c != null && c.qty == 0 && c.amount.signum() == 0) {
            cells.remove(category);
        }
    }

    // ---------------------------------------------------------------- 快照 / 全量重算

    /** 增量视图当前快照。 */
    public synchronized Snapshot snapshot() {
        TreeMap<String, AggCell> copy = new TreeMap<>();
        for (Map.Entry<String, AggCell> en : cells.entrySet()) {
            copy.put(en.getKey(), en.getValue().copy());
        }
        return new Snapshot(copy, orphanQty, orphanAmount,
                orders.size(), products.size(), appliedEvents.size());
    }

    /**
     * 全量重算：丢弃增量聚合，从两张基表重新 join + group by。
     * 作为验收基准（ground truth）。
     */
    public synchronized Snapshot fullRecompute() {
        TreeMap<String, AggCell> rebuilt = new TreeMap<>();
        long oQty = 0;
        BigDecimal oAmt = BigDecimal.ZERO.setScale(2);
        for (OrderLine ol : orders.values()) {
            Product p = products.get(ol.productId);
            if (p == null) {
                oQty += ol.qty;
                oAmt = oAmt.add(ol.amount);
            } else {
                AggCell c = rebuilt.computeIfAbsent(p.category, k -> new AggCell());
                c.qty += ol.qty;
                c.amount = c.amount.add(ol.amount);
            }
        }
        return new Snapshot(rebuilt, oQty, oAmt,
                orders.size(), products.size(), appliedEvents.size());
    }

    /** 逐类别比对增量视图与全量重算，返回不一致项（空列表 = 完全一致）。 */
    public synchronized List<String> diffAgainstFull() {
        Snapshot inc = snapshot();
        Snapshot full = fullRecompute();
        List<String> diffs = new ArrayList<>();
        if (!inc.categories().keySet().equals(full.categories().keySet())) {
            diffs.add("类别集合不一致: 增量=" + inc.categories().keySet()
                    + " 全量=" + full.categories().keySet());
        }
        for (String cat : full.categories().keySet()) {
            AggCell a = inc.categories().get(cat);
            AggCell b = full.categories().get(cat);
            if (a == null) {
                diffs.add("类别 " + cat + " 增量视图缺失，全量=" + b.qty + "/" + b.amount.toPlainString());
                continue;
            }
            if (a.qty != b.qty || a.amount.compareTo(b.amount) != 0) {
                diffs.add("类别 " + cat + " 不一致: 增量=" + a.qty + "/" + a.amount.toPlainString()
                        + " 全量=" + b.qty + "/" + b.amount.toPlainString());
            }
        }
        if (inc.orphanQty() != full.orphanQty()
                || inc.orphanAmount().compareTo(full.orphanAmount()) != 0) {
            diffs.add("孤儿不一致: 增量=" + inc.orphanQty() + "/" + inc.orphanAmount().toPlainString()
                    + " 全量=" + full.orphanQty() + "/" + full.orphanAmount().toPlainString());
        }
        return diffs;
    }

    // ---------------------------------------------------------------- 调试/测试辅助

    public synchronized void debugReset() {
        orders.clear();
        products.clear();
        appliedEvents.clear();
        cells.clear();
        orphanQty = 0;
        orphanAmount = BigDecimal.ZERO.setScale(2);
    }

    synchronized OrderLine rawOrder(long id) { return orders.get(id); }
    synchronized Product rawProduct(long id) { return products.get(id); }

    /** 事件指纹：除 eventId 外的全部语义字段。用于发现“同 ID 不同载荷”的重放。 */
    static String fingerprint(Event e) {
        StringBuilder sb = new StringBuilder(e.type.name());
        sb.append('|').append(e.orderLineId);
        sb.append('|').append(e.productId);
        sb.append('|').append(e.qty);
        sb.append('|').append(e.amount == null ? "-" : e.amount.stripTrailingZeros().toPlainString());
        sb.append('|').append(e.category == null ? "-" : e.category);
        return sb.toString();
    }
}
