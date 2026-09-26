package com.example.recur;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.recur.model.ExpandRequest;
import com.example.recur.model.RecurrenceRule;
import java.time.format.DateTimeFormatter;
import java.util.List;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class RecurrenceExpanderTest {

  private final RecurrenceExpander expander = new RecurrenceExpander();

  private List<String> expand(RecurrenceRule rule) {
    return expand(rule, null, null, null);
  }

  private List<String> expand(
      RecurrenceRule rule, String windowStart, String windowEnd, Integer maxExpansions) {
    return expander.expand(new ExpandRequest(null, rule, windowStart, windowEnd, maxExpansions))
        .stream().map(z -> z.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)).toList();
  }

  private RecurrenceRule rule(String start, String until, String freq,
                              Integer interval, Integer count, String policy) {
    return new RecurrenceRule(start, until, freq, interval, count, policy);
  }

  @Test
  @DisplayName("DAILY 默认 interval=1，count 限制次数，首项即 start")
  void dailyWithCount() {
    List<String> result = expand(rule("2024-03-01", null, "DAILY", null, 3, null));
    assertEquals(List.of("2024-03-01T00:00:00Z", "2024-03-02T00:00:00Z", "2024-03-03T00:00:00Z"), result);
  }

  @Test
  @DisplayName("DAILY interval=2 跨月（覆盖 31 日所在月）")
  void dailyIntervalAcrossMonthBoundary() {
    List<String> result = expand(rule("2024-01-30", null, "DAILY", 2, 4, null));
    assertEquals(List.of(
        "2024-01-30T00:00:00Z", "2024-02-01T00:00:00Z",
        "2024-02-03T00:00:00Z", "2024-02-05T00:00:00Z"), result);
  }

  @Test
  @DisplayName("WEEKLY interval 与 until 含边界")
  void weeklyWithUntilInclusive() {
    List<String> result = expand(
        rule("2024-01-01T09:00:00Z", "2024-01-22T09:00:00Z", "WEEKLY", 1, null, null));
    assertEquals(List.of(
        "2024-01-01T09:00:00Z", "2024-01-08T09:00:00Z",
        "2024-01-15T09:00:00Z", "2024-01-22T09:00:00Z"), result);
  }

  @Test
  @DisplayName("WEEKLY interval=2，双周")
  void weeklyBiweekly() {
    List<String> result = expand(rule("2024-01-01", null, "WEEKLY", 2, 3, null));
    assertEquals(List.of(
        "2024-01-01T00:00:00Z", "2024-01-15T00:00:00Z", "2024-01-29T00:00:00Z"), result);
  }

  @Test
  @DisplayName("MONTHLY 31 日 + LAST_VALID_DAY：闰年 2 月落 29 日，平月落月末")
  void monthly31stLastValidDayLeapYear() {
    List<String> result = expand(rule("2024-01-31", null, "MONTHLY", 1, 6, "LAST_VALID_DAY"));
    assertEquals(List.of(
        "2024-01-31T00:00:00Z", "2024-02-29T00:00:00Z", "2024-03-31T00:00:00Z",
        "2024-04-30T00:00:00Z", "2024-05-31T00:00:00Z", "2024-06-30T00:00:00Z"), result);
  }

  @Test
  @DisplayName("MONTHLY 31 日 + LAST_VALID_DAY：平年 2 月落 28 日，随后 3 月仍为 31 日")
  void monthly31stLastValidDayCommonYear() {
    List<String> result = expand(rule("2023-12-31", null, "MONTHLY", 1, 4, "LAST_VALID_DAY"));
    assertEquals(List.of(
        "2023-12-31T00:00:00Z", "2024-01-31T00:00:00Z",
        "2024-02-29T00:00:00Z", "2024-03-31T00:00:00Z"), result);
  }

  @Test
  @DisplayName("MONTHLY 31 日 + SKIP：无 31 日的月份不产生实例，后续月份恢复")
  void monthly31stSkip() {
    List<String> result = expand(rule("2024-01-31", null, "MONTHLY", 1, 4, "SKIP"));
    assertEquals(List.of(
        "2024-01-31T00:00:00Z", "2024-03-31T00:00:00Z",
        "2024-05-31T00:00:00Z", "2024-07-31T00:00:00Z"), result);
  }

  @Test
  @DisplayName("MONTHLY 2 月 29 日 + LAST_VALID_DAY：平年回退 28 日")
  void monthlyFeb29LastValidDay() {
    List<String> result = expand(rule("2024-02-29", null, "MONTHLY", 1, 4, "LAST_VALID_DAY"));
    assertEquals(List.of(
        "2024-02-29T00:00:00Z", "2024-03-29T00:00:00Z",
        "2024-04-29T00:00:00Z", "2024-05-29T00:00:00Z"), result);

    List<String> yearly = expand(rule("2024-02-29", "2027-03-01", "MONTHLY", 12, null, "LAST_VALID_DAY"));
    // interval=12 再次到 2 月：2025-02-28、2026-02-28、2027-02-28
    assertEquals(List.of(
        "2024-02-29T00:00:00Z", "2025-02-28T00:00:00Z",
        "2026-02-28T00:00:00Z", "2027-02-28T00:00:00Z"), yearly);
  }

  @Test
  @DisplayName("MONTHLY 2 月 29 日 + SKIP：仅闰年 2 月产出")
  void monthlyFeb29SkipOnlyLeapYears() {
    List<String> result =
        expand(rule("2024-02-29", "2032-03-01", "MONTHLY", 12, null, "SKIP"));
    assertEquals(List.of(
        "2024-02-29T00:00:00Z", "2028-02-29T00:00:00Z", "2032-02-29T00:00:00Z"), result);
  }

  @Test
  @DisplayName("MONTHLY interval=3（季度）保留起始日，月末策略生效")
  void quarterlyWithPolicy() {
    List<String> lastValid = expand(rule("2024-01-31", null, "MONTHLY", 3, 4, "LAST_VALID_DAY"));
    assertEquals(List.of(
        "2024-01-31T00:00:00Z", "2024-04-30T00:00:00Z",
        "2024-07-31T00:00:00Z", "2024-10-31T00:00:00Z"), lastValid);

    List<String> skip = expand(rule("2024-01-31", "2024-12-31", "MONTHLY", 3, null, "SKIP"));
    assertEquals(List.of("2024-01-31T00:00:00Z", "2024-07-31T00:00:00Z", "2024-10-31T00:00:00Z"), skip);
  }

  @Test
  @DisplayName("count 与 until 同时给出：count 更严格时按 count 截断")
  void countAndUntilCountStricter() {
    List<String> result = expand(
        rule("2024-01-01", "2024-12-31", "DAILY", 1, 3, null));
    assertEquals(3, result.size());
    assertEquals("2024-01-03T00:00:00Z", result.get(2));
  }

  @Test
  @DisplayName("count 与 until 同时给出：until 更严格时按 until 截断（含边界）")
  void countAndUntilUntilStricter() {
    List<String> result = expand(
        rule("2024-01-01", "2024-01-05T12:00:00Z", "DAILY", 1, 100, null));
    assertEquals(List.of(
        "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z",
        "2024-01-04T00:00:00Z", "2024-01-05T00:00:00Z"), result);
  }

  @Test
  @DisplayName("窗口在序列之后：空区间返回空结果（无异常）")
  void emptyResultFromWindowAfterSeries() {
    List<String> bounded = expand(
        rule("2024-01-01", "2024-01-05", "DAILY", 1, null, null),
        "2025-01-01", null, null);
    assertTrue(bounded.isEmpty());
  }

  @Test
  @DisplayName("窗口在序列之前：空区间返回空结果")
  void emptyResultFromWindowBeforeSeries() {
    List<String> result = expand(
        rule("2024-01-10", null, "DAILY", 1, 3, null),
        null, "2024-01-05", null);
    assertTrue(result.isEmpty());
  }

  @Test
  @DisplayName("窗口过滤不改变 count：count 对完整序列计数")
  void windowFilterDoesNotChangeCount() {
    List<String> result = expand(
        rule("2024-01-01", null, "DAILY", 1, 5, null),
        "2024-01-03", null, null);
    assertEquals(List.of(
        "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z", "2024-01-05T00:00:00Z"), result);
  }

  @Test
  @DisplayName("小 maxExpansions + 远离起点的窗口：周期守卫与输出上限解耦，不误报")
  void smallMaxExpansionsWithFarWindowStillWorks() {
    List<String> result = expand(
        rule("2020-01-31", "2100-12-31", "MONTHLY", 1, null, "SKIP"),
        "2090-01-01", "2090-09-30", 5);
    assertEquals(List.of(
        "2090-01-31T00:00:00Z", "2090-03-31T00:00:00Z",
        "2090-05-31T00:00:00Z", "2090-07-31T00:00:00Z",
        "2090-08-31T00:00:00Z"), result);
  }

  @Test
  @DisplayName("无界规则（无 count 且无 until）触发展开上限错误")
  void unboundedRuleHitsExpansionLimit() {
    RecurrenceException ex = assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY", 1, null, null), null, null, 10));
    assertEquals("EXPANSION_LIMIT_EXCEEDED", ex.errorCode());
  }

  @Test
  @DisplayName("maxExpansions 超范围被校验拒绝（防过大展开）")
  void maxExpansionsOutOfRangeRejected() {
    RecurrenceException tooBig = assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY", 1, null, null), null, null, 10001));
    assertEquals("VALIDATION_ERROR", tooBig.errorCode());

    RecurrenceException zero = assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY", 1, 3, null), null, null, 0));
    assertEquals("VALIDATION_ERROR", zero.errorCode());
  }

  @Test
  @DisplayName("until 早于 start：显式校验失败而非静默空结果")
  void untilBeforeStartRejected() {
    RecurrenceException ex = assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-10", "2024-01-01", "DAILY", 1, null, null)));
    assertEquals("VALIDATION_ERROR", ex.errorCode());
  }

  @Test
  @DisplayName("非法 frequency / interval / count / policy 均被拒绝")
  void invalidRuleFieldsRejected() {
    assertEquals("VALIDATION_ERROR", assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "YEARLY", 1, 3, null))).errorCode());
    assertEquals("VALIDATION_ERROR", assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY", 0, 3, null))).errorCode());
    assertEquals("VALIDATION_ERROR", assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY", 1, -1, null))).errorCode());
    assertEquals("VALIDATION_ERROR", assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-31", null, "MONTHLY", 1, 3, "EXPLODE"))).errorCode());
    assertEquals("VALIDATION_ERROR", assertThrows(RecurrenceException.class,
        () -> expand(rule("2024-01-01", null, "DAILY",
            RecurrenceExpander.MAX_INTERVAL + 1, 3, null))).errorCode());
  }

  @Test
  @DisplayName("tzdb 版本字符串非空")
  void tzdbVersionReported() {
    String version = expander.tzdbVersion();
    assertFalse(version.isBlank());
    // IANA tzdb 版本形如 2024a
    assertTrue(version.matches("\\d{4}[a-z]"), "unexpected tzdb version: " + version);
  }
}
