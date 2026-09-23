package incagg.model;

import java.math.BigDecimal;
import java.util.Objects;

/**
 * 商品维表记录。category 是连接键，决定订单行走入哪个分组。
 * 金额无关，仅保存分类。
 */
public final class Product {
    public final long productId;
    public final String category;

    public Product(long productId, String category) {
        this.productId = productId;
        this.category = Objects.requireNonNull(category, "category");
    }

    public Product withCategory(String newCategory) {
        return new Product(productId, newCategory);
    }

    @Override public String toString() {
        return "Product{productId=" + productId + ", category='" + category + "'}";
    }
}
