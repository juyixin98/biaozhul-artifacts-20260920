package com.winquant.json;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonTest {

    @Test
    void roundTripOfRequestShapes() {
        String single = "{\"timestamp\":123,\"value\":-7}";
        Map<String, Object> m = Json.parseObject(single);
        assertEquals(123L, ((Number) m.get("timestamp")).longValue());
        assertEquals(-7L, ((Number) m.get("value")).longValue());

        String batch = "{\"events\":[{\"timestamp\":1,\"value\":2},{\"timestamp\":1,\"value\":2}]}";
        Map<String, Object> b = Json.parseObject(batch);
        assertEquals(2, ((List<?>) b.get("events")).size());
    }

    @Test
    void parsesAllTypes() {
        Object v = Json.parse("{\"a\":1,\"b\":2.5,\"c\":-3,\"d\":\"x\\n\\\"y\",\"e\":[true,false,null],\"f\":null}");
        Map<?, ?> m = (Map<?, ?>) v;
        assertEquals(1L, ((Number) m.get("a")).longValue());
        assertEquals(2.5, ((Number) m.get("b")).doubleValue());
        assertEquals(-3L, ((Number) m.get("c")).longValue());
        assertEquals("x\n\"y", m.get("d"));
        List<?> list = (List<?>) m.get("e");
        assertEquals(Boolean.TRUE, list.get(0));
        assertEquals(Boolean.FALSE, list.get(1));
        assertNull(list.get(2));
        assertNull(m.get("f"));
    }

    @Test
    void writeAndParse() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("count", 0L);
        m.put("value", null);
        m.put("name", "中文 test");
        String json = Json.write(m);
        Map<String, Object> parsed = Json.parseObject(json);
        assertEquals(0L, ((Number) parsed.get("count")).longValue());
        assertNull(parsed.get("value"));
        assertEquals("中文 test", parsed.get("name"));
    }

    @Test
    void rejectsInvalidInput() {
        assertThrows(IllegalArgumentException.class, () -> Json.parse(""));
        assertThrows(IllegalArgumentException.class, () -> Json.parse("{\"a\":}"));
        assertThrows(IllegalArgumentException.class, () -> Json.parse("[1,2,]"));
        assertThrows(IllegalArgumentException.class, () -> Json.parse("123trailing"));
        assertTrue(true);
    }
}
