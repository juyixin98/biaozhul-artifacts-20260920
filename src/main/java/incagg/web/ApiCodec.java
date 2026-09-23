package incagg.web;

import incagg.json.Json;
import incagg.model.Event;
import incagg.model.EventType;
import incagg.store.IncrementalViewStore;
import incagg.store.Snapshot;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 把 HTTP JSON 与领域事件/快照互转。 */
final class ApiCodec {

    private ApiCodec() {}

    static Event toEvent(Map<String, Object> m) {
        String eventId = str(m, "eventId");
        String typeStr = str(m, "type");
        if (typeStr == null) throw new IllegalArgumentException("缺少字段: type");
        EventType type;
        try {
            type = EventType.valueOf(typeStr);
        } catch (IllegalArgumentException ex) {
            throw new IllegalArgumentException("未知 type: " + typeStr
                    + "（合法值: ORDER_UPSERT/ORDER_DELETE/PRODUCT_UPSERT/PRODUCT_DELETE）");
        }
        Event.Builder b = Event.builder(eventId, type);
        Long orderLineId = lng(m, "orderLineId");
        Long productId = lng(m, "productId");
        Integer qty = integer(m, "qty");
        BigDecimal amount = decimal(m, "amount");
        String category = str(m, "category");
        if (orderLineId != null) b.orderLineId(orderLineId);
        if (productId != null) b.productId(productId);
        if (qty != null) b.qty(qty);
        if (amount != null) b.amount(amount);
        if (category != null) b.category(category);
        return b.build();
    }

    @SuppressWarnings("unchecked")
    static List<Event> toEvents(Map<String, Object> body) {
        List<Event> out = new ArrayList<>();
        Object rawEvents = body.get("events");
        if (rawEvents instanceof List<?> list) {
            for (Object o : list) {
                if (!(o instanceof Map<?, ?>)) throw new IllegalArgumentException("events 元素必须是对象");
                out.add(toEvent((Map<String, Object>) o));
            }
        } else {
            // 整个请求体就是单个事件
            out.add(toEvent(body));
        }
        return out;
    }

    static Map<String, Object> snapshotJson(Snapshot s) {
        Map<String, Object> root = new LinkedHashMap<>();
        List<Map<String, Object>> cats = new ArrayList<>();
        for (Map.Entry<String, IncrementalViewStore.AggCell> en : s.categories().entrySet()) {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("category", en.getKey());
            c.put("qty", en.getValue().qty);
            c.put("amount", en.getValue().amount);
            cats.add(c);
        }
        root.put("categories", cats);
        Map<String, Object> orphan = new LinkedHashMap<>();
        orphan.put("qty", s.orphanQty());
        orphan.put("amount", s.orphanAmount());
        root.put("orphan", orphan);
        root.put("orderCount", s.orderCount());
        root.put("productCount", s.productCount());
        root.put("appliedEventCount", s.appliedEventCount());
        return root;
    }

    // ---------------------------------------------------------------- 取值辅助

    static String str(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (!(v instanceof String)) throw new IllegalArgumentException(k + " 必须是字符串");
        return (String) v;
    }

    static Long lng(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof BigDecimal bd) return bd.longValueExact();
        throw new IllegalArgumentException(k + " 必须是整数");
    }

    static Integer integer(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof BigDecimal bd) return bd.intValueExact();
        throw new IllegalArgumentException(k + " 必须是整数");
    }

    static BigDecimal decimal(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof BigDecimal bd) return bd;
        throw new IllegalArgumentException(k + " 必须是数字");
    }

    static String write(Object o) {
        return Json.toJson(o);
    }
}
