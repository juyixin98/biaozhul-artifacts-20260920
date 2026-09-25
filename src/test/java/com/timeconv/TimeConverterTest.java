package com.timeconv;

import java.math.BigInteger;

import static com.timeconv.TestRunner.assertEquals;
import static com.timeconv.TestRunner.assertThrows;
import static com.timeconv.TestRunner.assertTrue;
import static com.timeconv.TestRunner.test;

/** Unit tests for the core integer conversion, focused on negative rounding and limits. */
public final class TimeConverterTest {

    public static void register() {
        test("scale up is exact: 1 s -> 1e9 ns", () ->
                assertEquals("1000000000", convert("1", Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY)));

        test("scale down exact: 1500 ms -> 1.5 s is inexact without rounding", () ->
                assertThrows(ErrorCode.INEXACT,
                        () -> convert("1500", Unit.MILLISECOND, Unit.SECOND, Rounding.UNNECESSARY)));

        // --- negative rounding: every mode on -1500 ms -> s ---
        test("negative FLOOR rounds toward -inf: -1500 ms -> -2 s", () ->
                assertEquals("-2", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.FLOOR)));
        test("negative CEILING rounds toward +inf: -1500 ms -> -1 s", () ->
                assertEquals("-1", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.CEILING)));
        test("negative DOWN truncates toward zero: -1500 ms -> -1 s", () ->
                assertEquals("-1", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.DOWN)));
        test("negative UP rounds away from zero: -1500 ms -> -2 s", () ->
                assertEquals("-2", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.UP)));
        test("negative HALF_UP ties away from zero: -1500 ms -> -2 s", () ->
                assertEquals("-2", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_UP)));
        test("negative HALF_EVEN tie to even: -1500 ms -> -2 s", () ->
                assertEquals("-2", convert("-1500", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_EVEN)));
        test("HALF_EVEN tie to even: 2500 ms -> 2 s (not 3)", () ->
                assertEquals("2", convert("2500", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_EVEN)));
        test("HALF_EVEN negative tie to even: -2500 ms -> -2 s", () ->
                assertEquals("-2", convert("-2500", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_EVEN)));
        test("HALF_UP tie: 2500 ms -> 3 s", () ->
                assertEquals("3", convert("2500", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_UP)));
        test("HALF_UP below tie stays: 1499 ms -> 1 s", () ->
                assertEquals("1", convert("1499", Unit.MILLISECOND, Unit.SECOND, Rounding.HALF_UP)));
        test("negative FLOOR non-tie: -1 ns -> -1 us", () ->
                assertEquals("-1", convert("-1", Unit.NANOSECOND, Unit.MICROSECOND, Rounding.FLOOR)));
        test("negative CEILING non-tie: -1 ns -> 0 us", () ->
                assertEquals("0", convert("-1", Unit.NANOSECOND, Unit.MICROSECOND, Rounding.CEILING)));

        // --- extreme integers ---
        test("Long.MAX_VALUE ns -> us FLOOR", () ->
                assertEquals("9223372036854775",
                        convert("9223372036854775807", Unit.NANOSECOND, Unit.MICROSECOND, Rounding.FLOOR)));
        test("Long.MIN_VALUE ns -> us FLOOR (negative extreme)", () ->
                assertEquals("-9223372036854776",
                        convert("-9223372036854775808", Unit.NANOSECOND, Unit.MICROSECOND, Rounding.FLOOR)));
        test("Long.MIN_VALUE ns -> us CEILING", () ->
                assertEquals("-9223372036854775",
                        convert("-9223372036854775808", Unit.NANOSECOND, Unit.MICROSECOND, Rounding.CEILING)));
        test("Long.MAX_VALUE s -> ms exact", () ->
                assertEquals("9223372036854775000",
                        convert("9223372036854775", Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));

        // --- overflow must not silently truncate ---
        test("OVERFLOW: Long.MAX_VALUE s -> ms overflows int64", () ->
                assertThrows(ErrorCode.OVERFLOW, () ->
                        TimeConverter.convertToLong(Long.MAX_VALUE, Unit.SECOND, Unit.MILLISECOND, Rounding.UNNECESSARY)));
        test("OVERFLOW: Long.MIN_VALUE s -> ns overflows int64", () ->
                assertThrows(ErrorCode.OVERFLOW, () ->
                        TimeConverter.convertToLong(Long.MIN_VALUE, Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY)));
        test("OVERFLOW: huge BigInteger input reported, not truncated", () ->
                assertThrows(ErrorCode.OVERFLOW, () ->
                        TimeConverter.toLongChecked(TimeConverter.convert(
                                new BigInteger("99999999999999999999999999"), Unit.SECOND, Unit.NANOSECOND,
                                Rounding.UNNECESSARY).value())));

        // --- round trips (lossless only where representable) ---
        test("round trip ns->us->ns with DOWN loses sub-us part (documented)", () -> {
            long down = TimeConverter.convertToLong(123456789L, Unit.NANOSECOND, Unit.MICROSECOND, Rounding.DOWN);
            long back = TimeConverter.convertToLong(down, Unit.MICROSECOND, Unit.NANOSECOND, Rounding.UNNECESSARY);
            assertEquals(123456000L, back);
        });
        test("round trip s->ns->s is lossless for any int64 seconds in range", () -> {
            long v = -1_696_000_000L; // before epoch
            long ns = TimeConverter.convertToLong(v, Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY);
            long back = TimeConverter.convertToLong(ns, Unit.NANOSECOND, Unit.SECOND, Rounding.UNNECESSARY);
            assertEquals(v, back);
        });
        test("exact flag is true only for exact conversions", () -> {
            assertTrue(TimeConverter.convert(BigInteger.ONE, Unit.SECOND, Unit.NANOSECOND, Rounding.UNNECESSARY).exact(),
                    "1 s -> ns is exact");
            assertTrue(!TimeConverter.convert(BigInteger.valueOf(1500), Unit.MILLISECOND, Unit.SECOND,
                    Rounding.HALF_UP).exact(), "1500 ms -> s is inexact");
        });
    }

    private static String convert(String value, Unit from, Unit to, Rounding mode) {
        TimeConverter.Result r = TimeConverter.convert(new BigInteger(value), from, to, mode);
        return TimeConverter.toLongChecked(r.value()) + "";
    }

    private TimeConverterTest() {
    }
}
