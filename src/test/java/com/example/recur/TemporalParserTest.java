package com.example.recur;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.time.ZoneId;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class TemporalParserTest {

  @Test
  @DisplayName("裸日期视为目标时区当日 00:00")
  void parsesDateAtStartOfDay() {
    assertEquals("2024-03-01T00:00:00+08:00",
        TemporalParser.parse("2024-03-01", ZoneId.of("Asia/Shanghai")).format(java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME));
  }

  @Test
  @DisplayName("带偏移量时间按同一时刻转换到目标时区")
  void parsesOffsetAcrossZones() {
    assertEquals("2024-03-01T09:00:00+08:00",
        TemporalParser.parse("2024-03-01T01:00:00Z", ZoneId.of("Asia/Shanghai")).format(java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME));
  }

  @Test
  @DisplayName("无时区本地时间按目标时区墙上时间解释")
  void parsesLocalWallTime() {
    assertEquals("2024-03-01T09:00:00+08:00",
        TemporalParser.parse("2024-03-01T09:00:00", ZoneId.of("Asia/Shanghai")).format(java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME));
  }

  @Test
  @DisplayName("非法时间字符串抛 VALIDATION_ERROR")
  void rejectsGarbage() {
    RecurrenceException ex = assertThrows(RecurrenceException.class,
        () -> TemporalParser.parse("not-a-date", ZoneId.of("UTC")));
    assertEquals("VALIDATION_ERROR", ex.errorCode());
  }
}
