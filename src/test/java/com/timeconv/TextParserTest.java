package com.timeconv;

import static com.timeconv.TestRunner.assertEquals;
import static com.timeconv.TestRunner.assertThrows;
import static com.timeconv.TestRunner.test;

/** Tests for fractional-second and ISO-8601 text parsing (no floating point anywhere). */
public final class TextParserTest {

    public static void register() {
        // --- decimal text with fractional seconds ---
        test("decimal fraction: 1.5 s -> 1500 ms", () ->
                assertEquals("1500", parseDec("1.5", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("negative decimal fraction: -0.5 s -> -500 ms", () ->
                assertEquals("-500", parseDec("-0.5", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("negative decimal fraction: -0.5 s -> -500000000 ns", () ->
                assertEquals("-500000000", parseDec("-0.5", Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY)));
        test("fraction finer than target with rounding: 1.2345 s -> 1235 ms HALF_UP", () ->
                assertEquals("1235", parseDec("1.2345", Unit.SECOND, Unit.MILLISECOND, Rounding.HALF_UP)));
        test("fraction finer than target, negative FLOOR: -1.2345 s -> -1235 ms", () ->
                assertEquals("-1235", parseDec("-1.2345", Unit.SECOND, Unit.MILLISECOND, Rounding.FLOOR)));
        test("fraction finer than target without rounding -> INEXACT", () ->
                assertThrows(ErrorCode.INEXACT,
                        () -> parseDec("1.2345", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("9-digit fraction exact in ns: 0.123456789 s", () ->
                assertEquals("123456789", parseDec("0.123456789", Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY)));
        test("10-digit fraction in ns without rounding -> INEXACT", () ->
                assertThrows(ErrorCode.INEXACT,
                        () -> parseDec("0.1234567891", Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY)));
        test("10-digit fraction in ns with HALF_UP", () ->
                assertEquals("123456789", parseDec("0.1234567891", Unit.SECOND, Unit.NANOSECOND, Rounding.HALF_UP)));
        test("10-digit fraction tie in ns with HALF_EVEN (odd -> up)", () ->
                assertEquals("123456790", parseDec("0.1234567895", Unit.SECOND, Unit.NANOSECOND, Rounding.HALF_EVEN)));
        test("negative zero fraction: -0.0 s -> 0 ms", () ->
                assertEquals("0", parseDec("-0.0", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));

        // --- invalid precision / malformed text ---
        test("invalid: exponent notation rejected", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseDec("1e3", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("invalid: trailing dot rejected", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseDec("1.", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("invalid: bare dot rejected", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseDec(".5", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("invalid: empty text rejected", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseDec("", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("invalid precision: picoseconds are not a supported unit", () ->
                assertThrows(ErrorCode.INVALID_UNIT, () -> Unit.parse("PICOSECOND")));
        test("invalid precision: unknown unit name", () ->
                assertThrows(ErrorCode.INVALID_UNIT, () -> Unit.parse("FORTNIGHT")));

        // --- ISO-8601 instants with fractional seconds ---
        test("ISO epoch zero: 1970-01-01T00:00:00Z -> 0 s", () ->
                assertEquals("0", parseIso("1970-01-01T00:00:00Z", Unit.SECOND, Rounding.UNNECESSARY)));
        test("ISO negative epoch: 1969-12-31T23:59:59.5Z -> -500 ms", () ->
                assertEquals("-500", parseIso("1969-12-31T23:59:59.5Z", Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("ISO negative epoch exact ns: 1969-12-31T23:59:59.999999999Z -> -1 ns", () ->
                assertEquals("-1", parseIso("1969-12-31T23:59:59.999999999Z", Unit.NANOSECOND, Rounding.UNNECESSARY)));
        test("ISO positive: 2026-09-25T00:00:00Z -> 1790294400 s", () ->
                assertEquals("1790294400", parseIso("2026-09-25T00:00:00Z", Unit.SECOND, Rounding.UNNECESSARY)));
        test("ISO with offset: 1970-01-01T01:00:00+01:00 -> 0 s", () ->
                assertEquals("0", parseIso("1970-01-01T01:00:00+01:00", Unit.SECOND, Rounding.UNNECESSARY)));
        test("ISO fraction beyond ns with rounding: ...59.1234567899Z -> ms", () ->
                assertEquals("-877", parseIso("1969-12-31T23:59:59.1234567899Z", Unit.MILLISECOND, Rounding.FLOOR)));
        test("ISO invalid date rejected: 2026-13-01", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseIso("2026-13-01T00:00:00Z", Unit.SECOND, Rounding.UNNECESSARY)));
        test("ISO missing offset rejected", () ->
                assertThrows(ErrorCode.INVALID_TEXT,
                        () -> parseIso("2026-09-25T00:00:00", Unit.SECOND, Rounding.UNNECESSARY)));
    }

    private static String parseDec(String text, Unit unit, Unit to, Rounding mode) {
        TimeConverter.Result r = TextParser.parseDecimal(text, unit, to, mode);
        return Long.toString(TimeConverter.toLongChecked(r.value()));
    }

    private static String parseIso(String text, Unit to, Rounding mode) {
        TimeConverter.Result r = TextParser.parseIsoInstant(text, to, mode);
        return Long.toString(TimeConverter.toLongChecked(r.value()));
    }

    private TextParserTest() {
    }
}
