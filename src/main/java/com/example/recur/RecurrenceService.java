package com.example.recur;

import com.example.recur.model.ErrorResponse;
import com.example.recur.model.ExpandRequest;
import com.example.recur.model.ExpandResponse;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;

/** JSON 输入输出编排：反序列化、展开、结果/错误序列化。 */
public final class RecurrenceService {

  private static final ObjectMapper MAPPER = new ObjectMapper();
  private static final DateTimeFormatter OUTPUT_FORMAT = DateTimeFormatter.ISO_OFFSET_DATE_TIME;

  private final RecurrenceExpander expander;

  public RecurrenceService() {
    this(new RecurrenceExpander());
  }

  public RecurrenceService(RecurrenceExpander expander) {
    this.expander = expander;
  }

  /** 处理一段 JSON 请求文本，返回应输出给调用方的 JSON 文本（不抛业务异常）。 */
  public String handleJson(String json) {
    String tzdb = expander.tzdbVersion();
    try {
      ExpandRequest request = MAPPER.readValue(json, ExpandRequest.class);
      List<ZonedDateTime> occurrences = expander.expand(request);
      List<String> formatted = occurrences.stream()
          .map(OUTPUT_FORMAT::format)
          .toList();
      String zoneId = request.zoneId() == null || request.zoneId().isBlank()
          ? "UTC" : request.zoneId().trim();
      ExpandResponse response =
          new ExpandResponse(true, tzdb, zoneId, formatted.size(), formatted);
      return MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(response);
    } catch (JsonProcessingException e) {
      return toJson(new ErrorResponse(false, tzdb, "INVALID_JSON",
          "请求不是合法 JSON 或字段类型错误: " + e.getOriginalMessage()));
    } catch (RecurrenceException e) {
      return toJson(new ErrorResponse(false, tzdb, e.errorCode(), e.getMessage()));
    }
  }

  private String toJson(ErrorResponse error) {
    try {
      return MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(error);
    } catch (JsonProcessingException impossible) {
      return "{\"success\":false,\"errorCode\":\"INTERNAL_ERROR\"}";
    }
  }
}
