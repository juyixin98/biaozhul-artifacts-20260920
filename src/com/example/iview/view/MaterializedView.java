package com.example.iview.view;

import com.example.iview.model.OrderLine;
import com.example.iview.model.Product;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Incrementally maintained join-aggregation view:
 *
 * <pre>
 *   order_lines  ⋈  products (on product_id)
 *   ── GROUP BY category ── SUM(qty), SUM(amount)
 * </pre>
 *
 * Supported changes on <em>both</em> sides: insert (upsert), update and delete.
 * Every change is wrapped in a business event carrying a globally unique
 * {@code eventId}; an eventId is consumed at most once <strong>for the lifetime
 * of this view instance</strong> (see {@link #seenEventIds()}).
 *
 * <p>Order lines whose product is missing (yet), and order lines orphaned by a
 * product delete, are counted under the {@link #UNMATCHED} pseudo-category so
 * that full recomputation over the raw tables always agrees with the
 * incremental view.
 *
 * <p>All money values are {@link BigDecimal} fixed point with scale 2.
 * This class is thread-safe (a single monitor guards every table and aggregate).
 */
public final class MaterializedView {

    /** Pseudo-category for order lines without a matching product row. */
    public static final String UNMATCHED = "(unmatched)";

    private static final int MONEY_SCALE = 2;

    private final Map<String, Product> products = new LinkedHashMap<>();
    private final Map<String, OrderLine> lines = new LinkedHashMap<>();

    /** category -> aggregate; categories with zero totals are pruned. */
    private final Map<String, MutableAggregate> aggregates = new LinkedHashMap<>();

    /** Consumed event ids; deduplication scope = lifetime of this instance. */
    private final java.util.Set<String> seenEventIds = new java.util.HashSet<>();

    private long appliedEvents;

    // ---------------------------------------------------------------- events

    /**
     * Applies one event.
     *
     * @param eventId globally unique event id; a repeat is ignored
     * @param side    {@code "product"} or {@code "order_line"}
     * @param op      {@code "upsert"} or {@code "delete"}
     */
    public synchronized ApplyResult applyEvent(String eventId, String side, String op,
                                               Product product, OrderLine line) {
        if (eventId == null || eventId.isBlank()) {
            throw new IllegalArgumentException("eventId is required");
        }
        if (!seenEventIds.add(eventId)) {
            return new ApplyResult(eventId, true, side, op, false, false,
                    null, null, 0, 0);
        }
        appliedEvents++;
        return switch (side) {
            case "product" -> switch (op) {
                case "upsert" -> upsertProduct(eventId, product);
                case "delete" -> deleteProduct(eventId, product != null ? product.productId() : null);
                default -> throw new IllegalArgumentException("Unknown op: " + op);
            };
            case "order_line" -> switch (op) {
                case "upsert" -> upsertLine(eventId, line);
                case "delete" -> deleteLine(eventId, line != null ? line.orderLineId() : null);
                default -> throw new IllegalArgumentException("Unknown op: " + op);
            };
            default -> throw new IllegalArgumentException("side must be 'product' or 'order_line'");
        };
    }

    // -------------------------------------------------------- product side

    private ApplyResult upsertProduct(String eventId, Product p) {
        require(p != null, "product payload required");
        require(p.productId() != null && !p.productId().isBlank(), "productId required");
        String category = normalizeCategory(p.category());

        Product old = products.put(p.productId(), new Product(p.productId(), category));
        if (old == null) {
            // New product: contributes nothing by itself; existing unmatched
            // lines for it now join and move into the category.
            int migrated = matchExistingLines(p.productId(), UNMATCHED, category);
            return new ApplyResult(eventId, false, "product", "upsert",
                    migrated > 0, false, null, category, migrated, 0);
        }
        if (old.category().equals(category)) {
            return new ApplyResult(eventId, false, "product", "upsert",
                    false, false, old.category(), category, 0, 0);
        }
        // Category change: migrate every joined order line between buckets.
        int migrated = matchExistingLines(p.productId(), old.category(), category);
        return new ApplyResult(eventId, false, "product", "upsert",
                true, false, old.category(), category, migrated, 0);
    }

    private ApplyResult deleteProduct(String eventId, String productId) {
        require(productId != null && !productId.isBlank(), "productId required");
        Product removed = products.remove(productId);
        if (removed == null) {
            return new ApplyResult(eventId, false, "product", "delete",
                    false, true, null, null, 0, 0);
        }
        // Joined lines become unmatched.
        int orphaned = matchExistingLines(productId, removed.category(), UNMATCHED);
        return new ApplyResult(eventId, false, "product", "delete",
                true, false, removed.category(), UNMATCHED, orphaned, orphaned);
    }

    // ------------------------------------------------------- order line side

    private ApplyResult upsertLine(String eventId, OrderLine line) {
        require(line != null, "order_line payload required");
        require(line.orderLineId() != null && !line.orderLineId().isBlank(), "orderLineId required");
        require(line.productId() != null && !line.productId().isBlank(), "productId required");
        require(line.qty() >= 0, "qty must be >= 0");
        BigDecimal amount = normalizeMoney(line.amount());

        OrderLine normalized = new OrderLine(line.orderLineId(), line.productId(), line.qty(), amount);
        OrderLine old = lines.put(line.orderLineId(), normalized);
        if (old != null) {
            addToBucket(categoryOf(old.productId()), -old.qty(), old.amount().negate());
        }
        String target = categoryOf(normalized.productId());
        addToBucket(target, normalized.qty(), normalized.amount());
        pruneIfZero(target);
        if (old != null) {
            pruneIfZero(categoryOf(old.productId()));
        }
        boolean changed = old == null
                || old.qty() != normalized.qty()
                || old.amount().compareTo(normalized.amount()) != 0
                || !old.productId().equals(normalized.productId());
        return new ApplyResult(eventId, false, "order_line", "upsert",
                changed, false, null, target, 0,
                target.equals(UNMATCHED) ? 1 : 0);
    }

    private ApplyResult deleteLine(String eventId, String orderLineId) {
        require(orderLineId != null && !orderLineId.isBlank(), "orderLineId required");
        OrderLine removed = lines.remove(orderLineId);
        if (removed == null) {
            return new ApplyResult(eventId, false, "order_line", "delete",
                    false, true, null, null, 0, 0);
        }
        String bucket = categoryOf(removed.productId());
        addToBucket(bucket, -removed.qty(), removed.amount().negate());
        pruneIfZero(bucket);
        return new ApplyResult(eventId, false, "order_line", "delete",
                true, false, bucket, null, 0,
                bucket.equals(UNMATCHED) ? 1 : 0);
    }

    // ------------------------------------------------------------- helpers

    /**
     * Moves the aggregate contribution of all current lines of one product
     * from bucket {@code from} to bucket {@code to}. Returns how many lines
     * moved.
     */
    private int matchExistingLines(String productId, String from, String to) {
        int moved = 0;
        for (OrderLine line : lines.values()) {
            if (line.productId().equals(productId)) {
                addToBucket(from, -line.qty(), line.amount().negate());
                addToBucket(to, line.qty(), line.amount());
                moved++;
            }
        }
        if (moved > 0) {
            pruneIfZero(from);
            pruneIfZero(to);
        }
        return moved;
    }

    private String categoryOf(String productId) {
        Product p = products.get(productId);
        return p == null ? UNMATCHED : p.category();
    }

    private void addToBucket(String category, long signedQty, BigDecimal signedAmount) {
        MutableAggregate agg = aggregates.computeIfAbsent(category, k -> new MutableAggregate());
        agg.qty += signedQty;
        agg.amount = agg.amount.add(signedAmount).setScale(MONEY_SCALE, java.math.RoundingMode.HALF_UP);
    }

    private void pruneIfZero(String category) {
        MutableAggregate agg = aggregates.get(category);
        if (agg != null && agg.qty == 0 && agg.amount.signum() == 0) {
            aggregates.remove(category);
        }
    }

    private static String normalizeCategory(String c) {
        require(c != null && !c.isBlank(), "category required");
        return c.trim();
    }

    private static BigDecimal normalizeMoney(BigDecimal v) {
        require(v != null, "amount required");
        require(v.signum() >= 0, "amount must be >= 0");
        return v.setScale(MONEY_SCALE, java.math.RoundingMode.HALF_UP);
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new IllegalArgumentException(message);
        }
    }

    // --------------------------------------------------------------- reads

    /** Incremental aggregates keyed by category, sorted by category name. */
    public synchronized Map<String, CategoryAggregate> aggregates() {
        Map<String, CategoryAggregate> out = new TreeMap<>();
        for (Map.Entry<String, MutableAggregate> e : aggregates.entrySet()) {
            out.put(e.getKey(), e.getValue().snapshot());
        }
        return out;
    }

    public synchronized List<Product> products() {
        return List.copyOf(products.values());
    }

    public synchronized List<OrderLine> orderLines() {
        return List.copyOf(lines.values());
    }

    public synchronized java.util.Set<String> seenEventIds() {
        return java.util.Set.copyOf(seenEventIds);
    }

    public synchronized long appliedEvents() {
        return appliedEvents;
    }

    /**
     * Independent full recomputation from the raw tables. Used by the
     * acceptance comparison: it shares no code paths with the incremental
     * updates, so equality is a meaningful end-to-end check.
     */
    public synchronized Map<String, CategoryAggregate> fullRecompute() {
        Map<String, MutableAggregate> rebuilt = new LinkedHashMap<>();
        for (OrderLine line : lines.values()) {
            String bucket = categoryOf(line.productId());
            MutableAggregate agg = rebuilt.computeIfAbsent(bucket, k -> new MutableAggregate());
            agg.qty += line.qty();
            agg.amount = agg.amount.add(line.amount())
                    .setScale(MONEY_SCALE, java.math.RoundingMode.HALF_UP);
        }
        Map<String, CategoryAggregate> out = new TreeMap<>();
        rebuilt.entrySet().stream()
                .sorted(Comparator.comparing(Map.Entry::getKey))
                .forEach(e -> {
                    // Mirror incremental pruning: drop fully-zeroed buckets
                    // (including the unmatched pseudo-category).
                    boolean zero = e.getValue().qty == 0 && e.getValue().amount.signum() == 0;
                    if (!zero) {
                        out.put(e.getKey(), e.getValue().snapshot());
                    }
                });
        return out;
    }

    /** Convenience check used by tests and the {@code /verify} endpoint. */
    public synchronized boolean matchesFullRecompute() {
        return aggregates().equals(fullRecompute());
    }

    /** Snapshot rows for diagnostics: the aggregate list plus table counts. */
    public synchronized Map<String, Object> describe() {
        Map<String, Object> out = new LinkedHashMap<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Map.Entry<String, CategoryAggregate> e : aggregates().entrySet()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("category", e.getKey());
            row.put("totalQty", e.getValue().qty());
            row.put("totalAmount", e.getValue().amount().toPlainString());
            rows.add(row);
        }
        out.put("aggregates", rows);
        out.put("productCount", products.size());
        out.put("orderLineCount", lines.size());
        out.put("distinctSeenEventIds", seenEventIds.size());
        out.put("appliedEvents", appliedEvents);
        out.put("matchesFullRecompute", matchesFullRecompute());
        return out;
    }
}
