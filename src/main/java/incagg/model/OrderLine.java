package incagg.model;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * 订单行（事实表记录）。
 *
 * 字段：orderLineId 业务主键；productId 外键指向商品维表；
 * qty 数量（正整数）；amount 行金额，定点数（2 位小数）。
 */
public final class OrderLine {
    public final long orderLineId;
    public final long productId;
    public final int qty;
    public final BigDecimal amount;

    public OrderLine(long orderLineId, long productId, int qty, BigDecimal amount) {
        this.orderLineId = orderLineId;
        this.productId = productId;
        this.qty = qty;
        if (qty < 0) {
            throw new IllegalArgumentException("qty 不能为负: " + qty);
        }
        this.amount = Objects.requireNonNull(amount, "amount")
                .setScale(2, java.math.RoundingMode.HALF_UP);
    }

    public OrderLine with(long productId, int qty, BigDecimal amount) {
        return new OrderLine(orderLineId, productId, qty, amount);
    }

    @Override public String toString() {
        return "OrderLine{id=" + orderLineId + ", productId=" + productId
                + ", qty=" + qty + ", amount=" + amount.toPlainString() + "}";
    }
}
