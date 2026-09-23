package com.example.drvb.core;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ConditionTest {

    private Event event(Object... kv) {
        Map<String, Object> payload = new java.util.LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            payload.put((String) kv[i], kv[i + 1]);
        }
        return new Event("e1", "payment", 1000L, payload);
    }

    private Condition cond(Map<String, Object> m) {
        return Condition.fromMap(m);
    }

    private Map<String, Object> m(Object... kv) {
        Map<String, Object> map = new java.util.LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            map.put((String) kv[i], kv[i + 1]);
        }
        return map;
    }

    @Test
    void comparisonsAndNumericPromotion() {
        Event e = event("amount", 100, "price", "99.5", "currency", "USD");
        assertTrue(cond(m("op", "gte", "field", "amount", "value", 100)).test(e));
        assertTrue(cond(m("op", "gt", "field", "amount", "value", 99.99)).test(e));
        assertFalse(cond(m("op", "gt", "field", "amount", "value", 100)).test(e));
        // string "99.5" compared numerically
        assertTrue(cond(m("op", "lt", "field", "price", "value", 100)).test(e));
        // equality across int/long/bigdecimal forms
        assertTrue(cond(m("op", "eq", "field", "amount", "value", 100L)).test(e));
        assertTrue(cond(m("op", "eq", "field", "amount", "value", "100")).test(e));
        // missing field does not match
        assertFalse(cond(m("op", "eq", "field", "nope", "value", 1)).test(e));
        // ne on missing is false as well (three-valued: null is not unequal)
        assertFalse(cond(m("op", "ne", "field", "nope", "value", 1)).test(e));
    }

    @Test
    void stringOpsAndRegex() {
        Event e = event("region", "eu-west-1");
        assertTrue(cond(m("op", "startsWith", "field", "region", "value", "eu")).test(e));
        assertTrue(cond(m("op", "endsWith", "field", "region", "value", "west-1")).test(e));
        assertTrue(cond(m("op", "matches", "field", "region",
                "value", "eu-[a-z]+-\\d")).test(e));
        assertFalse(cond(m("op", "contains", "field", "region", "value", "us")).test(e));
    }

    @Test
    void inAndExists() {
        Event e = event("currency", "EUR", "nested", Map.of("tier", "gold"));
        assertTrue(cond(m("op", "in", "field", "currency",
                "values", List.of("USD", "EUR"))).test(e));
        assertFalse(cond(m("op", "in", "field", "currency",
                "values", List.of("USD"))).test(e));
        assertTrue(cond(m("op", "exists", "field", "nested.tier")).test(e));
        assertTrue(cond(m("op", "notExists", "field", "nested.missing")).test(e));
    }

    @Test
    void nestedLogic() {
        Event e = event("amount", 150, "currency", "USD", "fraud", true);
        Condition c = cond(m("op", "all", "conditions", List.of(
                m("op", "gt", "field", "amount", "value", 100),
                m("op", "any", "conditions", List.of(
                        m("op", "eq", "field", "currency", "value", "USD"),
                        m("op", "eq", "field", "fraud", "value", false))),
                m("op", "not", "condition",
                        m("op", "eq", "field", "currency", "value", "JPY")))));
        assertTrue(c.test(e));
        assertTrue(new Condition.True().test(e));
    }

    @Test
    void listIndexPaths() {
        Event e = event("items", List.of(Map.of("id", "a"), Map.of("id", "b")));
        assertTrue(cond(m("op", "eq", "field", "items.1.id", "value", "b")).test(e));
        assertFalse(cond(m("op", "eq", "field", "items.9.id", "value", "b")).test(e));
    }

    @Test
    void invalidTreesRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> cond(m("op", "bogus")));
        assertThrows(IllegalArgumentException.class,
                () -> cond(m("op", "gt")));
        assertThrows(IllegalArgumentException.class,
                () -> cond(m("op", "gt", "field", "amount")));
        assertThrows(IllegalArgumentException.class,
                () -> new Condition.Logic("not", List.of(new Condition.True(),
                        new Condition.True())));
    }
}
