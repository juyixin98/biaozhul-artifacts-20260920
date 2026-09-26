package com.example.recurrence.core;

import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RecurrenceExpanderTest {

    private final RecurrenceExpander expander = new RecurrenceExpander();

    private RecurrenceRule rule(LocalDate dtStart, Frequency freq, int interval,
                                Integer count, LocalDate until,
                                List<java.time.DayOfWeek> byDay, MonthEndPolicy policy) {
        return new RecurrenceRule(dtStart, freq, interval, count, until, byDay, policy);
    }

    @Test
    void expandsDailyEveryDayUntilCount() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1, 5, null, null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 2),
                LocalDate.of(2026, 1, 3), LocalDate.of(2026, 1, 4),
                LocalDate.of(2026, 1, 5)), dates);
    }

    @Test
    void expandsDailyWithInterval() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 3, null, null, null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 15), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 4),
                LocalDate.of(2026, 1, 7), LocalDate.of(2026, 1, 10),
                LocalDate.of(2026, 1, 13)), dates);
    }

    @Test
    void monthlyOn31stSkipsMonthsThatHaveNo31st() {
        // Arrange: dtStart Jan 31 -> only Jan, Mar, May, Jul, Aug, Oct, Dec qualify in 2025
        RecurrenceRule r = rule(LocalDate.of(2025, 1, 31), Frequency.MONTHLY, 1,
                null, null, null, MonthEndPolicy.SKIP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2025, 1, 1), LocalDate.of(2025, 12, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2025, 1, 31), LocalDate.of(2025, 3, 31),
                LocalDate.of(2025, 5, 31), LocalDate.of(2025, 7, 31),
                LocalDate.of(2025, 8, 31), LocalDate.of(2025, 10, 31),
                LocalDate.of(2025, 12, 31)), dates);
    }

    @Test
    void monthlyOn31stClampsToLastDayOfShortMonths() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2025, 1, 31), Frequency.MONTHLY, 1,
                null, null, null, MonthEndPolicy.CLAMP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2025, 1, 1), LocalDate.of(2025, 4, 30), 100);

        // Assert: Feb -> 28 (2025 is not a leap year), Apr -> 30
        assertEquals(List.of(
                LocalDate.of(2025, 1, 31), LocalDate.of(2025, 2, 28),
                LocalDate.of(2025, 3, 31), LocalDate.of(2025, 4, 30)), dates);
    }

    @Test
    void monthlyOn31stDoesNotDriftAfterClampedFebruary() {
        // Regression guard: day-1 anchoring must keep March at the 31st, not drift to the 28th.
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2025, 1, 31), Frequency.MONTHLY, 1,
                null, null, null, MonthEndPolicy.CLAMP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2025, 2, 1), LocalDate.of(2025, 3, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2025, 2, 28), LocalDate.of(2025, 3, 31)), dates);
    }

    @Test
    void feb29InLeapYearExpandsAndSkipsNonLeapYears() {
        // Arrange: dtStart 2024-02-29 (leap year), yearly cadence via 12-month interval, SKIP
        RecurrenceRule r = rule(LocalDate.of(2024, 2, 29), Frequency.MONTHLY, 12,
                null, null, null, MonthEndPolicy.SKIP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2024, 1, 1), LocalDate.of(2030, 12, 31), 100);

        // Assert: 2024 and 2028 are leap years; 2025..2027, 2029, 2030 skipped
        assertEquals(List.of(
                LocalDate.of(2024, 2, 29), LocalDate.of(2028, 2, 29)), dates);
    }

    @Test
    void feb29ClampsToFeb28InNonLeapYear() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2024, 2, 29), Frequency.MONTHLY, 12,
                null, null, null, MonthEndPolicy.CLAMP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2024, 1, 1), LocalDate.of(2026, 12, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2024, 2, 29), LocalDate.of(2025, 2, 28),
                LocalDate.of(2026, 2, 28)), dates);
    }

    @Test
    void monthlyClampLeapToNonLeapWithCount() {
        // Arrange: monthly from Feb 29 2024, count 6, CLAMP
        RecurrenceRule r = rule(LocalDate.of(2024, 2, 29), Frequency.MONTHLY, 1,
                6, null, null, MonthEndPolicy.CLAMP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2024, 2, 1), LocalDate.of(2025, 12, 31), 100);

        // Assert: Feb29, Mar29, Apr29, May29, Jun29, Jul29 (COUNT stops the series)
        assertEquals(List.of(
                LocalDate.of(2024, 2, 29), LocalDate.of(2024, 3, 29),
                LocalDate.of(2024, 4, 29), LocalDate.of(2024, 5, 29),
                LocalDate.of(2024, 6, 29), LocalDate.of(2024, 7, 29)), dates);
    }

    @Test
    void countAndUntilBothRestrictUntilStopsEarlier() {
        // Arrange: COUNT 10 but UNTIL Jan 5 -> UNTIL binds first
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1,
                10, LocalDate.of(2026, 1, 5), null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 31), 100);

        // Assert
        assertEquals(5, dates.size());
        assertEquals(LocalDate.of(2026, 1, 5), dates.get(4));
    }

    @Test
    void countAndUntilBothRestrictCountStopsEarlier() {
        // Arrange: COUNT 3 but UNTIL Jan 31 -> COUNT binds first
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1,
                3, LocalDate.of(2026, 1, 31), null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 12, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 2),
                LocalDate.of(2026, 1, 3)), dates);
    }

    @Test
    void returnsEmptyListWhenWindowIsBeforeDtstart() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2030, 1, 1), Frequency.MONTHLY, 1,
                null, null, null, MonthEndPolicy.SKIP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 12, 31), 100);

        // Assert
        assertTrue(dates.isEmpty());
    }

    @Test
    void returnsEmptyListWhenWindowFallsBetweenSkippedMonths() {
        // Arrange: 31st SKIP; a window containing only February yields nothing
        RecurrenceRule r = rule(LocalDate.of(2025, 1, 31), Frequency.MONTHLY, 1,
                null, null, null, MonthEndPolicy.SKIP);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2025, 2, 1), LocalDate.of(2025, 2, 27), 100);

        // Assert
        assertTrue(dates.isEmpty());
    }

    @Test
    void countStillConsumesOccurrencesOutsideWindow() {
        // Arrange: COUNT 3 all fall before the window
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1,
                3, null, null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 6, 1), LocalDate.of(2026, 6, 30), 100);

        // Assert
        assertTrue(dates.isEmpty(), "COUNT is counted from dtStart, not from windowStart");
    }

    @Test
    void throwsWhenInWindowOccurrencesExceedMaxExpansions() {
        // Arrange: 365 daily occurrences inside 2026, cap of 10
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1,
                null, null, null, null);

        // Act / Assert
        ExpansionLimitExceededException e = assertThrows(ExpansionLimitExceededException.class,
                () -> expander.expand(r,
                        LocalDate.of(2026, 1, 1), LocalDate.of(2026, 12, 31), 10));
        assertTrue(e.getMessage().contains("10"));
    }

    @Test
    void rejectsMaxExpansionsBelowOne() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1, null, null, null, null);

        // Act / Assert
        ValidationException e = assertThrows(ValidationException.class,
                () -> expander.expand(r,
                        LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 2), 0));
        assertEquals("INVALID_MAX_EXPANSIONS", e.code());
    }

    @Test
    void rejectsWindowEndBeforeWindowStart() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1, null, null, null, null);

        // Act / Assert
        ValidationException e = assertThrows(ValidationException.class,
                () -> expander.expand(r,
                        LocalDate.of(2026, 2, 1), LocalDate.of(2026, 1, 1), 100));
        assertEquals("INVALID_WINDOW", e.code());
    }

    @Test
    void windowPartiallyOverlapsCountedSeries() {
        // Arrange: a daily series of 10 from Jan 1; window covers Jan 4..6
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1, 10, null, null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 4), LocalDate.of(2026, 1, 6), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 1, 4), LocalDate.of(2026, 1, 5),
                LocalDate.of(2026, 1, 6)), dates);
    }

    @Test
    void rejectsPathologicallyLongSeriesBeforeWindow() {
        // Arrange: dtStart near LocalDate.MIN, window in 2026 -> billions of daily periods
        RecurrenceRule r = rule(LocalDate.of(-999_999_999, 1, 1), Frequency.DAILY, 1,
                null, null, null, null);

        // Act / Assert
        ValidationException e = assertThrows(ValidationException.class,
                () -> expander.expand(r,
                        LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 31), 100));
        assertEquals("EXPANSION_TOO_LARGE", e.code());
    }

    @Test
    void untilIsInclusive() {
        // Arrange
        RecurrenceRule r = rule(LocalDate.of(2026, 1, 1), Frequency.DAILY, 1,
                null, LocalDate.of(2026, 1, 2), null, null);

        // Act
        List<LocalDate> dates = expander.expand(r,
                LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 31), 100);

        // Assert
        assertEquals(List.of(LocalDate.of(2026, 1, 1), LocalDate.of(2026, 1, 2)), dates);
    }
}
