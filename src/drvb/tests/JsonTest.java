package drvb.tests;

import drvb.json.Json;

import java.util.List;
import java.util.Map;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertThrows;
import static drvb.tests.Asserts.assertTrue;

public class JsonTest {

    @Test
    public void parsesAndRoundTripsAllTypes() {
        String raw = "{\"name\":\"a\\\"b\",\"n\":42,\"d\":3.5,\"b\":false,\"z\":null,"
                + "\"arr\":[1,2,{\"k\":\"v\"}],\"u\":\"中\"}";
        Object v = Json.parse(raw);
        Map<String, Object> m = Json.asObject(v);
        assertEquals("a\"b", m.get("name"), "字符串转义");
        assertEquals(42L, ((Number) m.get("n")).longValue(), "整数解析为 Long");
        assertEquals(3.5, Json.asDouble(m.get("d")), 1e-9, "小数解析");
        assertEquals(Boolean.FALSE, m.get("b"), "布尔值");
        assertTrue(Json.isNull(m.get("z")), "null 哨兵");
        assertEquals(3, Json.asArray(m.get("arr")).size(), "数组长度");
        assertEquals("v", Json.asObject(Json.asArray(m.get("arr")).get(2)).get("k"), "嵌套对象");

        // round-trip 后结构等价
        Object reparsed = Json.parse(Json.write(m));
        assertEquals(m, reparsed, "序列化往返一致");
    }

    @Test
    public void rejectsMalformedJson() {
        assertThrows(RuntimeException.class, () -> Json.parse("{\"a\":}"), "缺值");
        assertThrows(RuntimeException.class, () -> Json.parse("[1,2,]"), "尾随逗号非法");
        assertThrows(RuntimeException.class, () -> Json.parse("123abc"), "多余内容");
        assertThrows(RuntimeException.class, () -> Json.parse("\"unterminated"), "未闭合字符串");
    }

    @Test
    public void prettyPrintIsValidJson() {
        Map<String, Object> obj = Json.obj("a", 1L, "list", Json.arr(1L, 2L));
        String pretty = Json.writePretty(obj);
        assertTrue(pretty.contains("\n"), "pretty 含换行");
        assertEquals(obj, Json.parse(pretty), "pretty 输出可重新解析且等价");
    }

    @Test
    public void intsAreLongAndDoublesStayDouble() {
        Map<String, Object> m = Json.asObject(Json.parse("{\"i\":7,\"f\":7.0}"));
        assertTrue(m.get("i") instanceof Long, "整数字面量 -> Long");
        assertTrue(m.get("f") instanceof Double, "小数字面量 -> Double");
        List<Object> a = Json.asArray(Json.parse("[10,10.0]"));
        assertTrue(a.get(0) instanceof Long && a.get(1) instanceof Double, "数组类型保持");
    }
}
