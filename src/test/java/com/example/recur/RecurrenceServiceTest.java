package com.example.recur;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class RecurrenceServiceTest {

  private final RecurrenceService service = new RecurrenceService();
  private final ObjectMapper mapper = new ObjectMapper();

  @Test
  @DisplayName("合法请求输出 success JSON，含 tzdbVersion 与 occurrences")
  void successfulJsonRoundTrip() throws Exception {
    String request = """
        {
          "zoneId": "Asia/Shanghai",
          "rule": {
            "start": "2024-01-31T10:00:00",
            "frequency": "MONTHLY",
            "interval": 1,
            "count": 3,
            "monthEndPolicy": "LAST_VALID_DAY"
          }
        }
        """;
    JsonNode root = mapper.readTree(service.handleJson(request));
    assertTrue(root.get("success").asBoolean());
    assertTrue(root.get("tzdbVersion").asText().matches("\\d{4}[a-z]"));
    assertEquals("Asia/Shanghai", root.get("zoneId").asText());
    assertEquals(3, root.get("occurrenceCount").asInt());
    assertEquals("2024-01-31T10:00:00+08:00", root.get("occurrences").get(0).asText());
    assertEquals("2024-02-29T10:00:00+08:00", root.get("occurrences").get(1).asText());
    assertEquals("2024-03-31T10:00:00+08:00", root.get("occurrences").get(2).asText());
  }

  @Test
  @DisplayName("空结果合法：success=true 且 occurrenceCount=0")
  void emptyWindowYieldsSuccessEmptyList() throws Exception {
    String request = """
        {
          "rule": {"start": "2024-01-01", "until": "2024-01-05", "frequency": "DAILY"},
          "windowStart": "2025-01-01"
        }
        """;
    JsonNode root = mapper.readTree(service.handleJson(request));
    assertTrue(root.get("success").asBoolean());
    assertEquals(0, root.get("occurrenceCount").asInt());
  }

  @Test
  @DisplayName("非法 JSON 返回 INVALID_JSON 且不抛异常")
  void invalidJsonReported() throws Exception {
    JsonNode root = mapper.readTree(service.handleJson("{ broken json"));
    assertEquals(false, root.get("success").asBoolean());
    assertEquals("INVALID_JSON", root.get("errorCode").asText());
  }

  @Test
  @DisplayName("无界规则返回 EXPANSION_LIMIT_EXCEEDED")
  void expansionLimitErrorCode() throws Exception {
    String request = """
        {
          "rule": {"start": "2024-01-01", "frequency": "DAILY"},
          "maxExpansions": 5
        }
        """;
    JsonNode root = mapper.readTree(service.handleJson(request));
    assertEquals(false, root.get("success").asBoolean());
    assertEquals("EXPANSION_LIMIT_EXCEEDED", root.get("errorCode").asText());
  }

  @Test
  @DisplayName("错误响应同样携带 tzdbVersion")
  void errorsCarryTzdbVersion() throws Exception {
    JsonNode root = mapper.readTree(service.handleJson("{}"));
    assertEquals("VALIDATION_ERROR", root.get("errorCode").asText());
    assertTrue(root.has("tzdbVersion"));
  }
}
