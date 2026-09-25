package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.json.Json;

import java.util.List;
import java.util.Map;

/** 迷你 JSON 解析/序列化测试。 */
public final class JsonTests {

    static void register(List<Case> cases) {
        cases.add(new Case("json: 嵌套结构往返", JsonTests::roundTrip));
        cases.add(new Case("json: 字符串转义", JsonTests::escapes));
        cases.add(new Case("json: 解析错误带位置", JsonTests::errorPosition));
    }

    @SuppressWarnings("unchecked")
    static void roundTrip() throws Exception {
        Map<String, Object> doc = new java.util.LinkedHashMap<>();
        doc.put("name", "测试\"引号\"");
        doc.put("tags", List.of("a", "b", 1L, true, 2.5));
        Map<String, Object> nested = new java.util.LinkedHashMap<>();
        nested.put("x", List.of(1L, 2L, 3L));
        nested.put("nil", null);
        doc.put("nested", nested);
        String s = Json.write(doc);
        Object back = Json.parse(s);
        Check.eq(back, doc, "序列化→解析应还原结构");
    }

    static void escapes() throws Exception {
        String original = "a\"b\\c\nd\té";
        Object back = Json.parse(Json.write(original));
        Check.eq(back, original, "转义字符应正确往返");
    }

    static void errorPosition() {
        try {
            Json.parse("{]");
            throw new AssertionError("应抛出 JsonException");
        } catch (Json.JsonException e) {
            Check.eq(e.position(), 1, "错误位置应为 1");
        }
        try {
            Json.parse("[1, 2");
            throw new AssertionError("应抛出 JsonException");
        } catch (Json.JsonException e) {
            Check.isTrue(e.position() >= 0, "未闭合数组应带位置");
        }
    }
}
