package com.example.recur.model;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 展开请求。
 *
 * @param zoneId        IANA 时区名（可选，默认 UTC）
 * @param rule          周期规则（必填）
 * @param windowStart   输出窗口起点（可选，含边界）；不影响 count 对序列的计数
 * @param windowEnd     输出窗口终点（可选，含边界）
 * @param maxExpansions 本次请求允许的展开数量上限（1..10000，默认 5000）
 */
@JsonPropertyOrder({"zoneId", "rule", "windowStart", "windowEnd", "maxExpansions"})
public record ExpandRequest(
    String zoneId,
    RecurrenceRule rule,
    String windowStart,
    String windowEnd,
    Integer maxExpansions) {
}
