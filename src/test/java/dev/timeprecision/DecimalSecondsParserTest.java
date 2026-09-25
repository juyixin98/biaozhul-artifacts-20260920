package dev.timeprecision;

import org.junit.jupiter.api.Test;

import java.math.BigInteger;
import java.math.RoundingMode;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class DecimalSecondsParserTest {

    @Test
    void parsesIntegerSeconds() {
        assertEquals(new BigInteger("123000000000"), DecimalSecondsParser.toNanos("123"));
        assertEquals(new BigInteger("-7000000000"), DecimalSecondsParser.toNanos("-7"));
        assertEquals(new BigInteger("5000000000"), DecimalSecondsParser.toNanos("+5"));
    }

    @Test
    void padsFractionToNanoseconds() {
        assertEquals(new BigInteger("1500000000"), DecimalSecondsParser.toNanos("1.5"));
        assertEquals(new BigInteger("123456789012"), DecimalSecondsParser.toNanos("123.456789012"));
        assertEquals(new BigInteger("1"), DecimalSecondsParser.toNanos("0.000000001"));
    }

    @Test
    void negativeFractionBelowOneSecondStaysNegative() {
        assertEquals(new BigInteger("-500000000"), DecimalSecondsParser.toNanos("-0.5"));
        assertEquals(new BigInteger("-1"), DecimalSecondsParser.toNanos("-0.000000001"));
    }

    @Test
    void rejectsPrecisionBeyondNanoseconds() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> DecimalSecondsParser.toNanos("0.1234567890"));
        assertEquals(ErrorCode.INVALID_PRECISION, e.code());
        e = assertThrows(ConversionException.class,
                () -> DecimalSecondsParser.toNanos("1.0000000001"));
        assertEquals(ErrorCode.INVALID_PRECISION, e.code());
    }

    @Test
    void rejectsMalformedLiterals() {
        for (String bad : new String[]{"", "abc", "1.2.3", "1.", ".5", "--1", "1e3", "NaN", "Infinity", "0x1"}) {
            ConversionException e = assertThrows(ConversionException.class,
                    () -> DecimalSecondsParser.toNanos(bad), "expected rejection of: " + bad);
            assertEquals(ErrorCode.INVALID_VALUE, e.code());
        }
    }

    @Test
    void parsesDirectlyIntoCoarserUnitWithRounding() {
        assertEquals(500L, DecimalSecondsParser.parse("0.5", TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        assertEquals(-500L, DecimalSecondsParser.parse("-0.5", TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        // 0.0005 s = 0.5 ms: rounding mode decides the outcome.
        assertEquals(1L, DecimalSecondsParser.parse("0.0005", TimeUnit.MILLISECONDS, RoundingMode.HALF_UP));
        assertEquals(0L, DecimalSecondsParser.parse("0.0005", TimeUnit.MILLISECONDS, RoundingMode.FLOOR));
        assertEquals(-1L, DecimalSecondsParser.parse("-0.0005", TimeUnit.MILLISECONDS, RoundingMode.FLOOR));
        assertEquals(0L, DecimalSecondsParser.parse("-0.0005", TimeUnit.MILLISECONDS, RoundingMode.DOWN));
    }

    @Test
    void exactParseRejectsInexactWithoutRounding() {
        ConversionException e = assertThrows(ConversionException.class,
                () -> DecimalSecondsParser.parse("0.0005", TimeUnit.MILLISECONDS, RoundingMode.UNNECESSARY));
        assertEquals(ErrorCode.ROUNDING_NECESSARY, e.code());
    }
}
