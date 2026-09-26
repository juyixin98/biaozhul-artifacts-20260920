package com.example.recur.model;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;
import java.util.List;

/** 成功响应。occurrences 为带偏移量的 ISO-8601 日期时间字符串。 */
@JsonPropertyOrder({"success", "tzdbVersion", "zoneId", "occurrenceCount", "occurrences"})
public record ExpandResponse(
    boolean success,
    String tzdbVersion,
    String zoneId,
    int occurrenceCount,
    List<String> occurrences) {
}
