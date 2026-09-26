package com.example.recur;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.regex.Pattern;

/**
 * 把三种 ISO-8601 文本解析为指定时区下的 ZonedDateTime：
 * <ul>
 *   <li>{@code yyyy-MM-dd}：视为该时区当日 00:00；</li>
 *   <li>带偏移量（末尾 Z 或 ±HH:MM）：按同一时刻转换到目标时区；</li>
 *   <li>无时区的本地日期时间：视为目标时区墙上时间（DST 间隙由 java.time 调整）。</li>
 * </ul>
 */
public final class TemporalParser {

  private static final Pattern OFFSET_SUFFIX = Pattern.compile(".*[+-]\\d{2}:\\d{2}$");
  private static final DateTimeFormatter LOCAL_DATE_TIME = DateTimeFormatter.ISO_LOCAL_DATE_TIME;

  private TemporalParser() {
  }

  public static ZonedDateTime parse(String text, ZoneId zone) {
    if (text == null || text.isBlank()) {
      throw new RecurrenceException("VALIDATION_ERROR", "时间字段为空");
    }
    String s = text.trim();
    try {
      if (s.length() == 10) {
        return LocalDate.parse(s).atStartOfDay(zone);
      }
      if (s.endsWith("Z") || OFFSET_SUFFIX.matcher(s).matches()) {
        return OffsetDateTime.parse(s).atZoneSameInstant(zone);
      }
      return LocalDateTime.parse(s, LOCAL_DATE_TIME).atZone(zone);
    } catch (DateTimeParseException e) {
      throw new RecurrenceException(
          "VALIDATION_ERROR", "无法解析的时间值 '" + text + "'，应为 ISO-8601 日期或日期时间");
    }
  }
}
