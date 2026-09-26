package com.example.recur.model;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 周期规则（输入子集）。所有时间字段均为 ISO-8601 字符串：
 * 日期 {@code yyyy-MM-dd}、本地日期时间 {@code yyyy-MM-ddTHH:mm:ss}、
 * 或带偏移量日期时间（末尾含 {@code Z} 或 {@code +08:00}）。
 *
 * @param start           起始实例（必填），第一次展开结果即该时刻
 * @param until           截止时刻（可选，含边界）；与 count 同时给出时取更严格者
 * @param frequency       DAILY / WEEKLY / MONTHLY（必填）
 * @param interval        间隔，&gt;=1，默认 1
 * @param count           次数上限（可选），从 start 起计数
 * @param monthEndPolicy  月末不存在日期策略，默认 LAST_VALID_DAY，仅 MONTHLY 生效
 */
@JsonPropertyOrder({"start", "until", "frequency", "interval", "count", "monthEndPolicy"})
public record RecurrenceRule(
    String start,
    String until,
    String frequency,
    Integer interval,
    Integer count,
    String monthEndPolicy) {
}
