package dev.timeprecision;

import java.math.BigInteger;
import java.math.RoundingMode;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses decimal-seconds text (e.g. {@code "123.456789012"}, {@code "-0.5"})
 * into exact integer timestamps. Parsing is purely positional decimal
 * arithmetic — no floating point is involved at any step.
 */
public final class DecimalSecondsParser {

    /** Nanoseconds per second, the finest supported precision. */
    public static final BigInteger NANOS_PER_SECOND = BigInteger.valueOf(1_000_000_000L);

    /** Maximum supported fractional digits (nanosecond resolution). */
    public static final int MAX_FRACTION_DIGITS = 9;

    private static final Pattern LITERAL = Pattern.compile("^([+-]?)(\\d+)(?:\\.(\\d+))?$");

    private DecimalSecondsParser() {
    }

    /**
     * Converts decimal-seconds text to a total nanosecond count.
     *
     * @throws ConversionException {@link ErrorCode#INVALID_VALUE} on malformed
     *                             text, {@link ErrorCode#INVALID_PRECISION} when
     *                             the fraction exceeds nanosecond resolution
     */
    public static BigInteger toNanos(String text) {
        Matcher m = LITERAL.matcher(text == null ? "" : text.trim());
        if (!m.matches()) {
            throw new ConversionException(ErrorCode.INVALID_VALUE,
                    "not a decimal-seconds literal: " + text);
        }
        String frac = m.group(3);
        BigInteger fractionNanos = BigInteger.ZERO;
        if (frac != null) {
            if (frac.length() > MAX_FRACTION_DIGITS) {
                throw new ConversionException(ErrorCode.INVALID_PRECISION,
                        "fraction has " + frac.length() + " digits; at most "
                                + MAX_FRACTION_DIGITS + " (nanoseconds) are supported: " + text);
            }
            fractionNanos = new BigInteger(frac + "0".repeat(MAX_FRACTION_DIGITS - frac.length()));
        }
        BigInteger total = new BigInteger(m.group(2))
                .multiply(NANOS_PER_SECOND)
                .add(fractionNanos);
        return "-".equals(m.group(1)) ? total.negate() : total;
    }

    /**
     * Parses decimal-seconds text directly into the target unit.
     *
     * @throws ConversionException on malformed text, excess precision,
     *                             inexact UNNECESSARY rounding, or overflow
     */
    public static long parse(String text, TimeUnit to, RoundingMode rounding) {
        return TimeConverter.convert(toNanos(text), TimeUnit.NANOSECONDS, to, rounding);
    }
}
