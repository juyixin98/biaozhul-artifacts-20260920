package com.example.iview.server;

import com.example.iview.model.OrderLine;
import com.example.iview.model.Product;
import com.example.iview.view.ApplyResult;
import com.example.iview.view.MaterializedView;

import java.math.BigDecimal;
import java.util.Map;

/** Converts one parsed JSON event body into a view call. */
final class EventParser {

    private EventParser() {
    }

    static ApplyResult apply(MaterializedView view, Map<String, Object> body) {
        String eventId = requireString(body, "eventId");
        String side = requireString(body, "side");
        String op = requireString(body, "op");

        Product product = null;
        OrderLine line = null;
        switch (side) {
            case "product" -> product = readProduct(body);
            case "order_line" -> line = readOrderLine(body);
            default -> throw new IllegalArgumentException("side must be 'product' or 'order_line'");
        }
        return view.applyEvent(eventId, side, op, product, line);
    }

    private static Product readProduct(Map<String, Object> body) {
        @SuppressWarnings("unchecked")
        Map<String, Object> payload = (Map<String, Object>) body.get("product");
        String productId;
        String category;
        if (payload != null) {
            productId = str(payload.get("productId"));
            category = str(payload.get("category"));
        } else {
            // delete may pass the id at the top level
            productId = str(body.get("productId"));
            category = str(body.get("category"));
        }
        if (productId == null || productId.isBlank()) {
            throw new IllegalArgumentException("product.productId is required");
        }
        if ("upsert".equals(str(body.get("op"))) && (category == null || category.isBlank())) {
            throw new IllegalArgumentException("product.category is required for upsert");
        }
        return new Product(productId, category == null ? "" : category.trim());
    }

    private static OrderLine readOrderLine(Map<String, Object> body) {
        boolean upsert = "upsert".equals(str(body.get("op")));
        @SuppressWarnings("unchecked")
        Map<String, Object> payload = (Map<String, Object>) body.get("orderLine");
        String orderLineId;
        String productId = "";
        long qty = 0;
        BigDecimal amount = BigDecimal.ZERO;
        if (payload != null) {
            orderLineId = str(payload.get("orderLineId"));
            String pid = str(payload.get("productId"));
            if (pid != null) {
                productId = pid;
            }
            if (upsert) {
                productId = pid;
                qty = asLong(payload.get("qty"));
                amount = asMoney(payload.get("amount"));
            }
        } else {
            // Flat form: a delete may pass just the id at the top level.
            orderLineId = str(body.get("orderLineId"));
            String pid = str(body.get("productId"));
            if (pid != null) {
                productId = pid;
            }
            if (upsert) {
                qty = asLong(body.get("qty"));
                amount = asMoney(body.get("amount"));
            }
        }
        if (orderLineId == null || orderLineId.isBlank()) {
            throw new IllegalArgumentException("orderLine.orderLineId is required");
        }
        if (upsert && (productId == null || productId.isBlank())) {
            throw new IllegalArgumentException("orderLine.productId is required for upsert");
        }
        return new OrderLine(orderLineId, productId, qty, amount);
    }

    private static String requireString(Map<String, Object> body, String key) {
        String v = str(body.get(key));
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException(key + " is required");
        }
        return v;
    }

    private static String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static long asLong(Object o) {
        if (o instanceof Number n) {
            return n.longValue();
        }
        if (o instanceof String s && !s.isBlank()) {
            return Long.parseLong(s);
        }
        throw new IllegalArgumentException("qty is required and must be an integer");
    }

    private static BigDecimal asMoney(Object o) {
        if (o == null) {
            throw new IllegalArgumentException("amount is required");
        }
        if (o instanceof BigDecimal bd) {
            return bd;
        }
        if (o instanceof Long l) {
            return BigDecimal.valueOf(l);
        }
        if (o instanceof Integer i) {
            return BigDecimal.valueOf(i);
        }
        // quoted string form, e.g. "19.90"
        return new BigDecimal(String.valueOf(o));
    }
}
