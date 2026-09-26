package com.example.recurrence.core;

import org.junit.jupiter.api.Test;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RecurrenceRuleTest {

    @Test
    void rejectsNullDtstart() {
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(null, Frequency.DAILY, 1, null, null, null, null));
        assertEquals("MISSING_DTSTART", e.code());
    }

    @Test
    void rejectsIntervalBelowOne() {
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(LocalDate.of(2026, 1, 1),
                        Frequency.DAILY, 0, null, null, null, null));
        assertEquals("INVALID_INTERVAL", e.code());
    }

    @Test
    void rejectsCountBelowOne() {
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(LocalDate.of(2026, 1, 1),
                        Frequency.DAILY, 1, 0, null, null, null));
        assertEquals("INVALID_COUNT", e.code());
    }

    @Test
    void rejectsUntilBeforeDtstart() {
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(LocalDate.of(2026, 1, 10),
                        Frequency.DAILY, 1, null, LocalDate.of(2026, 1, 1), null, null));
        assertEquals("UNTIL_BEFORE_DTSTART", e.code());
    }

    @Test
    void rejectsByDayOnNonWeekly() {
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(LocalDate.of(2026, 1, 1),
                        Frequency.DAILY, 1, null, null,
                        List.of(DayOfWeek.MONDAY), null));
        assertEquals("BYDAY_WEEKLY_ONLY", e.code());
    }

    @Test
    void rejectsDtstartWeekdayNotInByDay() {
        // dtStart 2026-01-01 is a Thursday
        ValidationException e = assertThrows(ValidationException.class,
                () -> new RecurrenceRule(LocalDate.of(2026, 1, 1),
                        Frequency.WEEKLY, 1, null, null,
                        List.of(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY), null));
        assertEquals("DTSTART_NOT_IN_BYDAY", e.code());
    }

    @Test
    void defaultsPolicyToSkipAndCopiesByDayDefensively() {
        List<DayOfWeek> mutable = new java.util.ArrayList<>(List.of(DayOfWeek.FRIDAY));
        RecurrenceRule rule = new RecurrenceRule(LocalDate.of(2026, 1, 2),
                Frequency.WEEKLY, 1, null, null, mutable, null);

        assertEquals(MonthEndPolicy.SKIP, rule.monthEndPolicy());
        mutable.add(DayOfWeek.MONDAY);
        assertEquals(1, rule.byDay().size(), "stored byDay must be an immutable copy");
        assertTrue(rule.byDay().contains(DayOfWeek.FRIDAY));
    }
}
