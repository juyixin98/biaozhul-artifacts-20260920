package dev.intervals.json;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonServiceTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final JsonService service = new JsonService();

    private JsonNode call(String json) throws Exception {
        return MAPPER.readTree(service.handle(json));
    }

    @Test
    @DisplayName("版本域并集：整数端点 + 无穷 + 单点，结果稳定排序合并")
    void versionUnion() throws Exception {
        String req = """
                {
                  "domain": "version",
                  "operation": "union",
                  "a": [{"lower": 1, "upper": 4}, {"lower": 4, "upper": 8, "lowerOpen": true}],
                  "b": [{"lower": 10, "upper": null}]
                }
                """;
        JsonNode resp = call(req);
        assertTrue(resp.get("ok").asBoolean());
        JsonNode result = resp.get("result");
        // [1,4] 与 (4,8] 在点 4 相触（点 4 属于前者），合并为 [1,8]；再与 [10,+∞) 共 2 段
        assertEquals(2, result.size());
        assertEquals(1, result.get(0).get("lower").asInt());
        assertEquals(8, result.get(0).get("upper").asInt());
        assertTrue(result.get(1).get("upper").isNull());
    }

    @Test
    @DisplayName("时间域差集：ISO 时间解析，输出带偏移量")
    void timeDifference() throws Exception {
        String req = """
                {
                  "domain": "time",
                  "timeZone": "Asia/Shanghai",
                  "operation": "difference",
                  "a": [{"lower": "2026-01-01T00:00:00", "upper": "2026-01-10T00:00:00"}],
                  "b": [{"lower": "2026-01-03T00:00:00", "upper": "2026-01-04T00:00:00"}]
                }
                """;
        JsonNode resp = call(req);
        assertTrue(resp.get("ok").asBoolean());
        JsonNode result = resp.get("result");
        assertEquals(2, result.size());
        assertTrue(result.get(0).get("upper").asText().startsWith("2026-01-03T00:00:00+08:00"));
        assertTrue(result.get(1).get("lower").asText().startsWith("2026-01-04T00:00:00+08:00"));
    }

    @Test
    @DisplayName("补集：有限集合补为 (-inf,..) 与 (..+inf) 两段")
    void complement() throws Exception {
        String req = """
                {
                  "domain": "version",
                  "operation": "complement",
                  "a": [{"lower": 3, "upper": 5}]
                }
                """;
        JsonNode resp = call(req);
        assertTrue(resp.get("ok").asBoolean());
        JsonNode result = resp.get("result");
        assertEquals(2, result.size());
        assertTrue(result.get(0).get("lower").isNull());
        assertEquals(3, result.get(0).get("upper").asInt());
        assertTrue(result.get(0).get("upperOpen").asBoolean());
        assertEquals(5, result.get(1).get("lower").asInt());
        assertTrue(result.get(1).get("lowerOpen").asBoolean());
        assertTrue(result.get(1).get("upper").isNull());
    }

    @Test
    @DisplayName("all 操作一次返回四种运算")
    void operationAll() throws Exception {
        String req = """
                {
                  "domain": "version",
                  "operation": "all",
                  "a": [{"lower": 1, "upper": 5}],
                  "b": [{"lower": 3, "upper": 7}]
                }
                """;
        JsonNode resp = call(req);
        assertTrue(resp.get("ok").asBoolean());
        assertTrue(resp.get("results").has("union"));
        assertTrue(resp.get("results").has("intersection"));
        assertTrue(resp.get("results").has("difference"));
        assertTrue(resp.get("results").has("complementOfA"));
    }

    @Test
    @DisplayName("反向区间被拒绝：ok=false 且错误信息说明原因")
    void reverseRejected() throws Exception {
        String req = """
                {
                  "domain": "version",
                  "operation": "union",
                  "a": [{"lower": 9, "upper": 2}],
                  "b": []
                }
                """;
        JsonNode resp = call(req);
        assertFalse(resp.get("ok").asBoolean());
        assertTrue(resp.get("error").asText().contains("反向区间"));
    }

    @Test
    @DisplayName("未知 operation 返回错误而非崩溃")
    void unknownOperation() throws Exception {
        JsonNode resp = call("""
                {"domain": "version", "operation": "xor", "a": [], "b": []}
                """);
        assertFalse(resp.get("ok").asBoolean());
        assertTrue(resp.get("error").asText().contains("operation"));
    }

    @Test
    @DisplayName("响应携带时区数据库版本信息")
    void includesTzVersion() throws Exception {
        JsonNode resp = call("""
                {"domain": "time", "operation": "union", "a": [], "b": []}
                """);
        assertTrue(resp.get("ok").asBoolean());
        JsonNode tz = resp.get("tzVersion");
        assertTrue(tz.get("jvmTzdbVersion").asText().matches("\\d{4}[a-z]"));
        assertEquals(java.time.ZoneId.systemDefault().getId(),
                tz.get("jvmDefaultZone").asText());
        assertTrue(tz.get("zoneIdCount").asInt() > 300);
    }

    @Test
    @DisplayName("非法 JSON 返回 ok=false")
    void malformedJson() {
        JsonNode resp;
        try {
            resp = call("{not json");
            assertFalse(resp.get("ok").asBoolean());
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
