package com.example.recurrence.json;

import com.example.recurrence.core.Frequency;
import com.example.recurrence.core.MonthEndPolicy;
import com.example.recurrence.core.RecurrenceExpander;
import com.example.recurrence.core.ValidationException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.time.DayOfWeek;
import java.time.LocalDate;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RequestParserTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private JsonNode json(String s) throws Exception {
        return mapper.readTree(s);
    }

    @Test
    void parsesFullValidRequest() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "r1",
                  "rule": {
                    "dtStart": "2025-01-31",
                    "freq": "MONTHLY",
                    "interval": 2,
                    "count": 10,
                    "until": "2026-01-31",
                    "monthEndPolicy": "CLAMP"
                  },
                  "windowStart": "2025-01-01",
                  "windowEnd": "2026-12-31",
                  "maxExpansions": 500
                }
                """;

        // Act
        RequestParser.ExpandRequest req = RequestParser.parse(json(body));

        // Assert
        assertEquals("r1", req.requestId());
        assertEquals(Frequency.MONTHLY, req.rule().freq());
        assertEquals(2, req.rule().interval());
        assertEquals(10, req.rule().count());
        assertEquals(LocalDate.of(2026, 1, 31), req.rule().until());
        assertEquals(MonthEndPolicy.CLAMP, req.rule().monthEndPolicy());
        assertEquals(LocalDate.of(2025, 1, 1), req.windowStart());
        assertEquals(500, req.effectiveMaxExpansions());
    }

    @Test
    void appliesDefaultsWhenOptionalFieldsMissing() throws Exception {
        // Arrange
        String body = """
                {
                  "rule": { "dtStart": "2026-03-02", "freq": "WEEKLY",
                            "byDay": ["MO", "FR"] },
                  "windowStart": "2026-03-01",
                  "windowEnd": "2026-03-31"
                }
                """;

        // Act
        RequestParser.ExpandRequest req = RequestParser.parse(json(body));

        // Assert
        assertNull(req.requestId());
        assertEquals(1, req.rule().interval());
        assertNull(req.rule().count());
        assertNull(req.rule().until());
        assertTrue(req.rule().byDay().contains(DayOfWeek.MONDAY));
        assertTrue(req.rule().byDay().contains(DayOfWeek.FRIDAY));
        assertEquals(RecurrenceExpander.DEFAULT_MAX_EXPANSIONS, req.effectiveMaxExpansions());
    }

    @Test
    void rejectsInvalidFreq() throws Exception {
        String body = """
                { "rule": { "dtStart": "2026-01-01", "freq": "YEARLY" },
                  "windowStart": "2026-01-01", "windowEnd": "2026-01-31" }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_FREQ", e.code());
    }

    @Test
    void rejectsMalformedDate() throws Exception {
        String body = """
                { "rule": { "dtStart": "01/01/2026", "freq": "DAILY" },
                  "windowStart": "2026-01-01", "windowEnd": "2026-01-31" }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_DATE", e.code());
    }

    @Test
    void rejectsInvalidByDayCode() throws Exception {
        String body = """
                { "rule": { "dtStart": "2026-03-02", "freq": "WEEKLY",
                            "byDay": ["MO", "XX"] },
                  "windowStart": "2026-03-01", "windowEnd": "2026-03-31" }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_BYDAY", e.code());
    }

    @Test
    void rejectsMaxExpansionsOverHardLimit() throws Exception {
        String body = """
                {
                  "rule": { "dtStart": "2026-01-01", "freq": "DAILY" },
                  "windowStart": "2026-01-01",
                  "windowEnd": "2026-01-31",
                  "maxExpansions": 999999
                }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_MAX_EXPANSIONS", e.code());
    }

    @Test
    void rejectsMissingRuleObject() throws Exception {
        String body = """
                { "windowStart": "2026-01-01", "windowEnd": "2026-01-31" }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_REQUEST", e.code());
    }

    @Test
    void rejectsNonPositiveCount() throws Exception {
        String body = """
                { "rule": { "dtStart": "2026-01-01", "freq": "DAILY", "count": 0 },
                  "windowStart": "2026-01-01", "windowEnd": "2026-01-31" }
                """;
        ValidationException e = assertThrows(ValidationException.class,
                () -> RequestParser.parse(json(body)));
        assertEquals("INVALID_COUNT", e.code());
    }
}
