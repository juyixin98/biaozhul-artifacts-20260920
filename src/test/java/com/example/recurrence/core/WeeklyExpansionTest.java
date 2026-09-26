package com.example.recurrence.core;

import org.junit.jupiter.api.Test;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class WeeklyExpansionTest {

    private final RecurrenceExpander expander = new RecurrenceExpander();

    @Test
    void weeklyWithoutByDayRepeatsOnDtstartWeekday() {
        // Arrange: dtStart Monday 2026-03-02, every week, COUNT 4
        RecurrenceRule rule = new RecurrenceRule(
                LocalDate.of(2026, 3, 2), Frequency.WEEKLY, 1, 4, null, List.of(), null);

        // Act
        List<LocalDate> dates = expander.expand(rule,
                LocalDate.of(2026, 3, 1), LocalDate.of(2026, 4, 30), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 3, 2), LocalDate.of(2026, 3, 9),
                LocalDate.of(2026, 3, 16), LocalDate.of(2026, 3, 23)), dates);
    }

    @Test
    void weeklyWithByDayEmitsAllMatchingWeekdaysPerActiveWeek() {
        // Arrange: dtStart Mon 2026-03-02; MO,WE,FR; every 2 weeks; COUNT 6
        RecurrenceRule rule = new RecurrenceRule(
                LocalDate.of(2026, 3, 2), Frequency.WEEKLY, 2, 6, null,
                List.of(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY, DayOfWeek.FRIDAY), null);

        // Act
        List<LocalDate> dates = expander.expand(rule,
                LocalDate.of(2026, 3, 1), LocalDate.of(2026, 6, 30), 100);

        // Assert: first active week gives 3/2,3/4,3/6; skipped week; next gives 3/16,3/18,3/20
        assertEquals(List.of(
                LocalDate.of(2026, 3, 2), LocalDate.of(2026, 3, 4),
                LocalDate.of(2026, 3, 6), LocalDate.of(2026, 3, 16),
                LocalDate.of(2026, 3, 18), LocalDate.of(2026, 3, 20)), dates);
    }

    @Test
    void weeklyByDayWithUntilCutsMidWeek() {
        // Arrange: MO,WE,FR; UNTIL Thursday 2026-03-05 -> Friday 3/6 must be excluded
        RecurrenceRule rule = new RecurrenceRule(
                LocalDate.of(2026, 3, 2), Frequency.WEEKLY, 1, null,
                LocalDate.of(2026, 3, 5),
                List.of(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY, DayOfWeek.FRIDAY), null);

        // Act
        List<LocalDate> dates = expander.expand(rule,
                LocalDate.of(2026, 3, 1), LocalDate.of(2026, 12, 31), 100);

        // Assert
        assertEquals(List.of(
                LocalDate.of(2026, 3, 2), LocalDate.of(2026, 3, 4)), dates);
    }

    @Test
    void weeklyFirstWeekRespectsDtstartBoundary() {
        // Arrange: dtStart Friday 2026-03-06; byDay MO,WE,FR -> Mon/Wed of week 1 precede dtStart
        RecurrenceRule rule = new RecurrenceRule(
                LocalDate.of(2026, 3, 6), Frequency.WEEKLY, 1, 5, null,
                List.of(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY, DayOfWeek.FRIDAY), null);

        // Act
        List<LocalDate> dates = expander.expand(rule,
                LocalDate.of(2026, 3, 1), LocalDate.of(2026, 3, 31), 100);

        // Assert: 3/6 (Fri), 3/9 (Mon), 3/11 (Wed), 3/13 (Fri), 3/16 (Mon)
        assertEquals(List.of(
                LocalDate.of(2026, 3, 6), LocalDate.of(2026, 3, 9),
                LocalDate.of(2026, 3, 11), LocalDate.of(2026, 3, 13),
                LocalDate.of(2026, 3, 16)), dates);
    }
}
