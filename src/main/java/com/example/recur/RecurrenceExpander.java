package com.example.recur;

import com.example.recur.model.ExpandRequest;
import com.example.recur.model.Frequency;
import com.example.recur.model.MonthEndPolicy;
import com.example.recur.model.RecurrenceRule;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * 周期规则有限展开引擎（明确子集，非完整 RFC 5545 实现）。
 *
 * <p>支持：DAILY / WEEKLY / MONTHLY 三种频率；正整数 interval；
 * COUNT（次数）与 UNTIL（截止时刻，含边界）可分别或同时给出，同时给出时取更严格者；
 * MONTHLY 在目标月份缺少起始日（如 31 日遇 2 月）时按 {@link MonthEndPolicy} 显式处理；
 * 可选输出窗口 windowStart/windowEnd（含边界）只过滤输出，不改变 COUNT 对序列的计数。
 */
public final class RecurrenceExpander {

  /** 未显式指定时的默认展开上限。 */
  public static final int DEFAULT_MAX_EXPANSIONS = 5000;
  /** 请求可申请的展开数量硬上限，超过即 VALIDATION_ERROR。 */
  public static final int HARD_MAX_EXPANSIONS = 10000;
  /** interval 上限：防止 interval*周期数 在日期运算中溢出 long。 */
  public static final int MAX_INTERVAL = 100_000;

  public RecurrenceExpander() {
  }

  /** 当前 JDK 内置 IANA 时区数据库版本，例如 2024a。 */
  public String tzdbVersion() {
    return java.time.zone.ZoneRulesProvider.getVersions("UTC").lastKey();
  }

  /** 解析并校验请求，返回有限展开结果（可能为空列表）。 */
  public List<ZonedDateTime> expand(ExpandRequest request) {
    Parsed inputs = parseAndValidate(request);
    return generate(inputs);
  }

  private Parsed parseAndValidate(ExpandRequest request) {
    if (request == null || request.rule() == null) {
      throw new RecurrenceException("VALIDATION_ERROR", "缺少 rule（周期规则）");
    }
    RecurrenceRule rule = request.rule();

    ZoneId zone;
    try {
      zone = (request.zoneId() == null || request.zoneId().isBlank())
          ? ZoneId.of("UTC") : ZoneId.of(request.zoneId().trim());
    } catch (Exception e) {
      throw new RecurrenceException("VALIDATION_ERROR", "未知时区: " + request.zoneId());
    }

    if (rule.start() == null || rule.start().isBlank()) {
      throw new RecurrenceException("VALIDATION_ERROR", "rule.start 为必填项");
    }
    ZonedDateTime start = TemporalParser.parse(rule.start(), zone);

    Frequency frequency;
    try {
      frequency = Frequency.valueOf(rule.frequency() == null ? "" : rule.frequency().trim());
    } catch (IllegalArgumentException e) {
      throw new RecurrenceException(
          "VALIDATION_ERROR", "frequency 仅支持 DAILY / WEEKLY / MONTHLY，收到: " + rule.frequency());
    }

    int interval = rule.interval() == null ? 1 : rule.interval();
    if (interval < 1) {
      throw new RecurrenceException("VALIDATION_ERROR", "interval 必须 >= 1，收到: " + interval);
    }
    if (interval > MAX_INTERVAL) {
      throw new RecurrenceException(
          "VALIDATION_ERROR", "interval 必须 <= " + MAX_INTERVAL + "，收到: " + interval);
    }

    Integer count = rule.count();
    if (count != null && count < 1) {
      throw new RecurrenceException("VALIDATION_ERROR", "count 必须 >= 1，收到: " + count);
    }

    ZonedDateTime until = null;
    if (rule.until() != null && !rule.until().isBlank()) {
      until = TemporalParser.parse(rule.until(), zone);
      if (until.isBefore(start)) {
        throw new RecurrenceException(
            "VALIDATION_ERROR", "until 早于 start，区间为空（请确认输入意图）");
      }
    }

    MonthEndPolicy policy = MonthEndPolicy.LAST_VALID_DAY;
    if (rule.monthEndPolicy() != null && !rule.monthEndPolicy().isBlank()) {
      try {
        policy = MonthEndPolicy.valueOf(rule.monthEndPolicy().trim());
      } catch (IllegalArgumentException e) {
        throw new RecurrenceException(
            "VALIDATION_ERROR",
            "monthEndPolicy 仅支持 LAST_VALID_DAY / SKIP，收到: " + rule.monthEndPolicy());
      }
    }

    ZonedDateTime windowStart = parseOptional(request.windowStart(), zone, "windowStart");
    ZonedDateTime windowEnd = parseOptional(request.windowEnd(), zone, "windowEnd");
    if (windowStart != null && windowEnd != null && windowEnd.isBefore(windowStart)) {
      throw new RecurrenceException("VALIDATION_ERROR", "windowEnd 早于 windowStart");
    }

    int maxExpansions = request.maxExpansions() == null
        ? DEFAULT_MAX_EXPANSIONS : request.maxExpansions();
    if (maxExpansions < 1 || maxExpansions > HARD_MAX_EXPANSIONS) {
      throw new RecurrenceException(
          "VALIDATION_ERROR",
          "maxExpansions 必须在 1.." + HARD_MAX_EXPANSIONS + " 之间，收到: " + maxExpansions);
    }

    return new Parsed(start, until, frequency, interval, count, policy,
        windowStart, windowEnd, maxExpansions, zone);
  }

  private ZonedDateTime parseOptional(String text, ZoneId zone, String field) {
    if (text == null || text.isBlank()) {
      return null;
    }
    try {
      return TemporalParser.parse(text, zone);
    } catch (RecurrenceException e) {
      throw new RecurrenceException("VALIDATION_ERROR", field + " 无法解析: " + text);
    }
  }

  private List<ZonedDateTime> generate(Parsed p) {
    List<ZonedDateTime> occurrences = new ArrayList<>();
    long periodIndex = 0;
    long produced = 0;
    // SKIP 策略下周期可能不产出实例；对遍历的周期数设防御性上限（与本次输出上限无关，
    // 取硬顶的 12 倍：即使最坏跳过比 5/12 也足以产出硬顶数量，并覆盖远离起点的窗口）。
    long periodGuard = (long) HARD_MAX_EXPANSIONS * 12L + 12L;

    while (true) {
      if (periodIndex > periodGuard) {
        throw new RecurrenceException(
            "EXPANSION_LIMIT_EXCEEDED",
            "遍历周期数超过防御性上限，请用 count/until 收紧规则或将窗口移近 start");
      }
      ZonedDateTime candidate;
      try {
        candidate = materialize(p, periodIndex);
      } catch (java.time.DateTimeException | ArithmeticException e) {
        throw new RecurrenceException(
            "EXPANSION_LIMIT_EXCEEDED", "周期推进超出支持的日期范围，请减小 interval 或收紧 until");
      }
      periodIndex++;
      if (candidate == null) {
        // MONTHLY + SKIP：目标月份无该日期，跳过且不计入 COUNT。
        continue;
      }
      if (p.count() != null && produced >= p.count()) {
        break;
      }
      if (p.until() != null && candidate.isAfter(p.until())) {
        break;
      }
      if (p.windowEnd() != null && candidate.isAfter(p.windowEnd())) {
        break;
      }
      boolean inWindow = (p.windowStart() == null || !candidate.isBefore(p.windowStart()));
      if (inWindow) {
        if (occurrences.size() >= p.maxExpansions()) {
          throw new RecurrenceException(
              "EXPANSION_LIMIT_EXCEEDED",
              "展开结果达到上限 " + p.maxExpansions()
                  + "，请缩小范围（count/until/windowEnd）或提高 maxExpansions（最大 "
                  + HARD_MAX_EXPANSIONS + "）");
        }
        occurrences.add(candidate);
      }
      produced++;
    }
    return occurrences;
  }

  /** 计算第 periodIndex 个周期的实例；MONTHLY+SKIP 且日期不存在时返回 null。 */
  private ZonedDateTime materialize(Parsed p, long periodIndex) {
    long n = (long) p.interval() * periodIndex;
    return switch (p.frequency()) {
      case DAILY -> p.start().plusDays(n);
      case WEEKLY -> p.start().plusWeeks(n);
      case MONTHLY -> materializeMonthly(p, n);
    };
  }

  private ZonedDateTime materializeMonthly(Parsed p, long monthsToAdd) {
    LocalDateTime base = p.start().toLocalDateTime();
    LocalDateTime shifted = base.plusMonths(monthsToAdd);
    int targetDay = base.getDayOfMonth();
    int lengthOfMonth = shifted.toLocalDate().lengthOfMonth();
    if (targetDay > lengthOfMonth) {
      if (p.monthEndPolicy() == MonthEndPolicy.SKIP) {
        return null;
      }
      shifted = shifted.withDayOfMonth(lengthOfMonth);
    }
    return ZonedDateTime.of(shifted, p.zone());
  }

  /** 校验后的不可变展开参数集合。 */
  private record Parsed(
      ZonedDateTime start,
      ZonedDateTime until,
      Frequency frequency,
      int interval,
      Integer count,
      MonthEndPolicy monthEndPolicy,
      ZonedDateTime windowStart,
      ZonedDateTime windowEnd,
      int maxExpansions,
      ZoneId zone) {
  }
}
