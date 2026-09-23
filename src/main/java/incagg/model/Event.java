package incagg.model;

import java.math.BigDecimal;

/**
 * 一个业务事件。eventId 是去重键（生产者提供的唯一 ID，如 UUID）。
 * 去重范围：全局、按 eventId 精确匹配，且只在“已应用”集合内去重
 * （见 {@link incagg.store.IncrementalViewStore} 的说明）。
 *
 * payload 字段按 type 取用：
 *  ORDER_UPSERT/ORDER_DELETE: orderLineId, productId?, qty?, amount?
 *    （delete 只需 orderLineId；upsert 字段齐全）
 *  PRODUCT_UPSERT/PRODUCT_DELETE: productId, category?
 */
public final class Event {
    public final String eventId;
    public final EventType type;

    public final Long orderLineId;
    public final Long productId;
    public final Integer qty;
    public final BigDecimal amount;
    public final String category;

    private Event(Builder b) {
        this.eventId = b.eventId;
        this.type = b.type;
        this.orderLineId = b.orderLineId;
        this.productId = b.productId;
        this.qty = b.qty;
        this.amount = b.amount;
        this.category = b.category;
    }

    public static Builder builder(String eventId, EventType type) {
        return new Builder(eventId, type);
    }

    /** 校验并补全语义，非法时抛 IllegalArgumentException。 */
    public void validate() {
        if (eventId == null || eventId.isBlank()) {
            throw new IllegalArgumentException("eventId 不能为空");
        }
        switch (type) {
            case ORDER_UPSERT -> {
                require(orderLineId, "orderLineId");
                require(productId, "productId");
                require(qty, "qty");
                require(amount, "amount");
                if (qty < 0) throw new IllegalArgumentException("qty 不能为负");
                if (amount.signum() < 0) throw new IllegalArgumentException("amount 不能为负");
            }
            case ORDER_DELETE -> require(orderLineId, "orderLineId");
            case PRODUCT_UPSERT -> {
                require(productId, "productId");
                if (category == null || category.isBlank())
                    throw new IllegalArgumentException("category 不能为空");
            }
            case PRODUCT_DELETE -> require(productId, "productId");
        }
    }

    private static void require(Object v, String name) {
        if (v == null) throw new IllegalArgumentException("缺少字段: " + name);
    }

    public static final class Builder {
        private final String eventId;
        private final EventType type;
        private Long orderLineId;
        private Long productId;
        private Integer qty;
        private BigDecimal amount;
        private String category;

        public Builder(String eventId, EventType type) {
            this.eventId = eventId;
            this.type = type;
        }
        public Builder orderLineId(long v) { this.orderLineId = v; return this; }
        public Builder productId(long v) { this.productId = v; return this; }
        public Builder qty(int v) { this.qty = v; return this; }
        public Builder amount(BigDecimal v) { this.amount = v; return this; }
        public Builder category(String v) { this.category = v; return this; }
        public Event build() {
            Event e = new Event(this);
            e.validate();
            return e;
        }
    }
}
