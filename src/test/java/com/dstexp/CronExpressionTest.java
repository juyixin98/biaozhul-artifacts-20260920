package com.dstexp;

import com.dstexp.cron.CronExpression;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class CronExpressionTest {

    private static List<LocalDateTime> expand(String cron, String from, String to) {
        return CronExpression.parse(cron)
                .occurrencesBetween(LocalDate.parse(from), LocalDate.parse(to));
    }

    @Test
    @DisplayName("every minute of a single day expands to 1440 ordered occurrences")
    void everyMinute() {
        var out = expand("* * * * *", "2026-03-10", "2026-03-10");
        assertThat(out).hasSize(1440);
        assertThat(out.get(0)).isEqualTo(LocalDateTime.parse("2026-03-10T00:00"));
        assertThat(out.get(1439)).isEqualTo(LocalDateTime.parse("2026-03-10T23:59"));
        assertThat(out).isSorted();
    }

    @Test
    @DisplayName("fixed time daily")
    void fixedTimeDaily() {
        var out = expand("30 2 * * *", "2026-03-08", "2026-03-10");
        assertThat(out).containsExactly(
                LocalDateTime.parse("2026-03-08T02:30"),
                LocalDateTime.parse("2026-03-09T02:30"),
                LocalDateTime.parse("2026-03-10T02:30"));
    }

    @Test
    @DisplayName("step expression in minutes")
    void minuteSteps() {
        var out = expand("*/15 10 * * *", "2026-03-10", "2026-03-10");
        assertThat(out).containsExactly(
                LocalDateTime.parse("2026-03-10T10:00"),
                LocalDateTime.parse("2026-03-10T10:15"),
                LocalDateTime.parse("2026-03-10T10:30"),
                LocalDateTime.parse("2026-03-10T10:45"));
    }

    @Test
    @DisplayName("ranged steps: minutes 5..20/5")
    void rangedStep() {
        var out = expand("5-20/5 * * * *", "2026-03-10", "2026-03-10");
        assertThat(out).extracting(LocalDateTime::getMinute)
                .containsOnly(5, 10, 15, 20)
                .hasSize(24 * 4);
    }

    @Test
    @DisplayName("month and day-of-week names parse case-insensitively with OR day semantics")
    void names() {
        var out = expand("0 9 1 jan-mar mon,wed,fri", "2026-01-01", "2026-03-31");
        assertThat(out).isNotEmpty().allSatisfy(t -> {
            assertThat(t.getHour()).isEqualTo(9);
            assertThat(t.getMinute()).isZero();
        });
        // OR semantics: the month 1sts are kept even when not Mon/Wed/Fri ...
        assertThat(out).contains(
                LocalDateTime.parse("2026-01-01T09:00"),
                LocalDateTime.parse("2026-02-01T09:00"),
                LocalDateTime.parse("2026-03-01T09:00"));
        // ... and Mon/Wed/Fri dates are kept even when they are not the 1st.
        assertThat(out).anyMatch(t -> t.getDayOfMonth() != 1);
    }

    @Test
    @DisplayName("restricted day-of-month alone gates the date")
    void dayOfMonthOnly() {
        var out = expand("0 0 15 * *", "2026-01-01", "2026-12-31");
        assertThat(out).hasSize(12).allSatisfy(t -> assertThat(t.getDayOfMonth()).isEqualTo(15));
    }

    @Test
    @DisplayName("restricted day-of-week alone gates the weekday")
    void dayOfWeekOnly() {
        var out = expand("0 9 * * 1-5", "2026-03-09", "2026-03-15");
        assertThat(out).hasSize(5);
        assertThat(out).noneMatch(t -> t.getDayOfWeek().getValue() >= 6);
    }

    @Test
    @DisplayName("day-of-week 0 and 7 both mean Sunday")
    void sundayAliases() {
        var zeros = expand("0 12 * * 0", "2026-03-08", "2026-03-08");
        var sevens = expand("0 12 * * 7", "2026-03-08", "2026-03-08");
        assertThat(zeros).containsExactly(LocalDateTime.parse("2026-03-08T12:00"));
        assertThat(sevens).containsExactly(LocalDateTime.parse("2026-03-08T12:00"));
    }

    @Test
    @DisplayName("empty range when from is after to")
    void emptyRange() {
        assertThat(expand("* * * * *", "2026-03-11", "2026-03-10")).isEmpty();
    }

    @Test
    @DisplayName("cross-year range includes January of the second year")
    void crossYear() {
        var out = expand("0 0 1 1 *", "2026-12-31", "2027-01-02");
        assertThat(out).containsExactly(LocalDateTime.parse("2027-01-01T00:00"));
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "   ", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
            "* * 0 * *", "* * * 13 *", "* * * * 8", "x * * * *", "*/0 * * * *", "10-5 * * * *"})
    @DisplayName("invalid cron expressions are rejected with a clear error")
    void invalidExpressions(String expr) {
        assertThatThrownBy(() -> CronExpression.parse(expr))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
