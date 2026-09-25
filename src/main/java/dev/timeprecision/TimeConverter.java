package dev.timeprecision;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.util.Objects;

/**
 * Exact integer timestamp conversion between units. All arithmetic uses
 * {@link BigInteger}/{@link BigDecimal} — no floating point is ever involved,
 * and overflow is reported as an error instead of silently truncating.
 */
public final class TimeConverter {

    private TimeConverter() {
    }

    /**
     * Converts an integer timestamp from one unit to another.
     *
     * @param value    the timestamp in {@code from} units (arbitrary precision)
     * @param from     source unit
     * @param to       target unit
     * @param rounding rounding mode applied when the conversion is inexact;
     *                 {@link RoundingMode#UNNECESSARY} makes any lossy
     *                 conversion fail explicitly
     * @return the timestamp in {@code to} units, as a signed 64-bit value
     * @throws ConversionException {@link ErrorCode#ROUNDING_NECESSARY} when the
     *                             mode is UNNECESSARY and the result is inexact,
     *                             {@link ErrorCode#OVERFLOW} when the result does
     *                             not fit in a signed 64-bit long
     */
    public static long convert(BigInteger value, TimeUnit from, TimeUnit to, RoundingMode rounding) {
        Objects.requireNonNull(value, "value");
        Objects.requireNonNull(from, "from");
        Objects.requireNonNull(to, "to");
        Objects.requireNonNull(rounding, "rounding");

        BigInteger nanos = value.multiply(from.nanosPerUnit());
        BigDecimal quotient;
        try {
            quotient = new BigDecimal(nanos)
                    .divide(new BigDecimal(to.nanosPerUnit()), 0, rounding);
        } catch (ArithmeticException e) {
            throw new ConversionException(ErrorCode.ROUNDING_NECESSARY,
                    "conversion from " + from + " to " + to + " is inexact; "
                            + "supply an explicit rounding mode");
        }
        return toLongExact(quotient.toBigInteger());
    }

    /**
     * Parses a signed decimal integer literal into a 64-bit long.
     *
     * @throws ConversionException {@link ErrorCode#INVALID_VALUE} on malformed
     *                             input, {@link ErrorCode#OVERFLOW} when the
     *                             literal exceeds the signed 64-bit range
     */
    public static long parseLongStrict(String raw) {
        return toLongExact(parseBigInteger(raw));
    }

    /**
     * Parses a signed decimal integer literal without a range restriction.
     *
     * @throws ConversionException {@link ErrorCode#INVALID_VALUE} on malformed input
     */
    public static BigInteger parseBigInteger(String raw) {
        if (raw == null || !raw.trim().matches("[+-]?\\d+")) {
            throw new ConversionException(ErrorCode.INVALID_VALUE,
                    "expected a signed integer literal, got: " + raw);
        }
        return new BigInteger(raw.trim());
    }

    /**
     * Narrows an arbitrary-precision result to a signed 64-bit long.
     *
     * @throws ConversionException {@link ErrorCode#OVERFLOW} when out of range
     */
    public static long toLongExact(BigInteger value) {
        if (value.bitLength() > 63) {
            throw new ConversionException(ErrorCode.OVERFLOW,
                    "result " + value + " does not fit in a signed 64-bit integer");
        }
        return value.longValue();
    }
}
