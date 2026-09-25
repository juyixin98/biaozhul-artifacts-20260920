package com.timeconv;

import java.math.BigInteger;

/**
 * Core integer timestamp conversion between units.
 *
 * <p>All arithmetic is done in {@link BigInteger}; no floating point is ever involved.
 * Results are checked against the signed 64-bit range: overflow raises
 * {@link ErrorCode#OVERFLOW} instead of silently truncating.
 */
public final class TimeConverter {

    private TimeConverter() {
    }

    /** Result of a conversion: the converted value plus whether it was exact. */
    public record Result(BigInteger value, boolean exact) {
    }

    /**
     * Convert an integer timestamp from one unit to another.
     *
     * @param value  the integer timestamp, arbitrary precision
     * @param from   unit of {@code value}
     * @param to     target unit
     * @param mode   rounding mode applied when the conversion is inexact;
     *               {@link Rounding#UNNECESSARY} fails on any inexact conversion
     * @return the converted value and whether the conversion was exact
     * @throws ConvertException INEXACT if rounding is UNNECESSARY and the conversion loses information
     */
    public static Result convert(BigInteger value, Unit from, Unit to, Rounding mode) {
        if (value == null || from == null || to == null || mode == null) {
            throw new ConvertException(ErrorCode.BAD_REQUEST, "value, fromUnit, toUnit and rounding must not be null");
        }
        BigInteger numerator = value.multiply(from.nanosPerUnit);
        BigInteger denominator = to.nanosPerUnit;
        return divide(numerator, denominator, mode);
    }

    /**
     * Convert and require the result to fit in a signed 64-bit integer.
     *
     * @throws ConvertException OVERFLOW if the result does not fit in a long
     */
    public static long convertToLong(long value, Unit from, Unit to, Rounding mode) {
        Result r = convert(BigInteger.valueOf(value), from, to, mode);
        return toLongChecked(r.value());
    }

    /**
     * Divide {@code num} by positive {@code den}, rounding the quotient per {@code mode}.
     * Signs follow Java's truncating division; rounding adjustments are applied explicitly
     * so negative values behave exactly as the mode names promise.
     */
    static Result divide(BigInteger num, BigInteger den, Rounding mode) {
        if (den.signum() <= 0) {
            throw new IllegalArgumentException("denominator must be positive");
        }
        BigInteger[] qr = num.divideAndRemainder(den);
        BigInteger quotient = qr[0];
        BigInteger remainder = qr[1];
        if (remainder.signum() == 0) {
            return new Result(quotient, true);
        }
        int sign = num.signum(); // non-zero because remainder is non-zero
        BigInteger adjusted = switch (mode) {
            case UNNECESSARY -> throw new ConvertException(ErrorCode.INEXACT,
                    "conversion is not exact and no rounding mode was given (or UNNECESSARY was requested)");
            case DOWN -> quotient;
            case UP -> quotient.add(BigInteger.valueOf(sign));
            case FLOOR -> remainder.signum() < 0 ? quotient.subtract(BigInteger.ONE) : quotient;
            case CEILING -> remainder.signum() > 0 ? quotient.add(BigInteger.ONE) : quotient;
            case HALF_UP -> roundHalf(quotient, remainder, den, sign, false);
            case HALF_EVEN -> roundHalf(quotient, remainder, den, sign, true);
        };
        return new Result(adjusted, false);
    }

    private static BigInteger roundHalf(BigInteger quotient, BigInteger remainder, BigInteger den,
                                        int sign, boolean toEvenOnTie) {
        int cmp = remainder.abs().shiftLeft(1).compareTo(den);
        if (cmp > 0) {
            return quotient.add(BigInteger.valueOf(sign));
        }
        if (cmp < 0) {
            return quotient;
        }
        // Exact tie.
        if (toEvenOnTie && !quotient.testBit(0)) {
            return quotient; // already even
        }
        return quotient.add(BigInteger.valueOf(sign));
    }

    /**
     * @throws ConvertException OVERFLOW if the value does not fit in a signed 64-bit long
     */
    public static long toLongChecked(BigInteger value) {
        try {
            return value.longValueExact();
        } catch (ArithmeticException e) {
            throw new ConvertException(ErrorCode.OVERFLOW,
                    "result " + value + " does not fit in a signed 64-bit integer");
        }
    }

    /**
     * @throws ConvertException OVERFLOW if the text is not a valid integer in the signed 64-bit range
     */
    public static long parseLongChecked(String text) {
        BigInteger v;
        try {
            v = new BigInteger(text.trim());
        } catch (NumberFormatException e) {
            throw new ConvertException(ErrorCode.INVALID_VALUE, "not an integer: '" + text + "'");
        }
        return toLongChecked(v);
    }
}
