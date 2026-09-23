package tumbling;

import java.util.List;
import java.util.Map;

import static tumbling.TestRunner.assertTrue;
import static tumbling.TestRunner.eq;

/** Json 解析/取值的边界回归。 */
public final class JsonTest {

    private static boolean rejects(String s) {
        try { Json.parse(s); return false; }
        catch (IllegalArgumentException e) { return true; }
    }

    @TestRunner.Test
    public void rejectsLeadingZeros() {
        assertTrue(rejects("01"), "前导零 01 非法");
        assertTrue(rejects("-007"), "-007 非法");
        assertTrue(rejects("{\"a\":01}"), "对象中的 01 非法");
        // 合法形式
        eq(Json.parse("0"), 0L, "0 合法");
        eq(Json.parse("-0"), 0L, "-0 合法");
        eq(((Number) Json.parse("0.5")).doubleValue(), 0.5, "0.5 合法");
    }

    @TestRunner.Test
    public void rejectsOutOfRangeInteger() {
        assertTrue(rejects("99999999999999999999999"), "超 long 整数非法（不静默降级 double）");
        eq(Json.parse("9223372036854775807"), Long.MAX_VALUE, "Long.MAX_VALUE 合法");
        assertTrue(rejects("-99999999999999999999999"), "超 long 负整数非法");
    }

    @TestRunner.Test
    public void lngRejectsFractions() {
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) Json.parse("{\"t\":15.9}");
        boolean threw = false;
        try { Json.lng(m, "t"); } catch (IllegalArgumentException e) { threw = true; }
        assertTrue(threw, "15.9 不能被 lng 静默截断为 15");

        @SuppressWarnings("unchecked")
        Map<String, Object> m2 = (Map<String, Object>) Json.parse("{\"t\":15}");
        eq(Json.lng(m2, "t"), 15L, "整数 15 正常");
    }

    @TestRunner.Test
    public void parsesNestedAndUnicode() {
        @SuppressWarnings("unchecked")
        Map<String, Object> root = (Map<String, Object>) Json.parse(
                "{\"a\":[1,2,{\"b\":\"中\\n\"}],\"c\":true,\"d\":null}");
        @SuppressWarnings("unchecked")
        List<Object> arr = (List<Object>) root.get("a");
        eq(arr.size(), 3L, "数组长度 3");
        @SuppressWarnings("unchecked")
        Map<String, Object> inner = (Map<String, Object>) arr.get(2);
        eq(inner.get("b"), "中\n", "unicode 与转义正确");
        eq(root.get("c"), Boolean.TRUE, "布尔");
        assertTrue(root.containsKey("d") && root.get("d") == null, "null");
    }
}
