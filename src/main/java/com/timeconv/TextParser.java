package com.timeconv;

import java.math.BigInteger;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses textual timestamps — decimal numbers with fractional seconds and ISO-8601
 * instants — into integer timestamps in a target unit, without ever using floating point.
 */
public final class TextParser {

    private TextParser() {
    }

    /** Maximum accepted fraction length; guards against absurd input sizes. */
    static final int MAX_FRACTION_DIGITS = 1000;

    private static final Pattern DECIMAL = Pattern.compile("([+-]?)(\\d+)(?:\\.(\\d+))?");
    private static final Pattern ISO_INSTANT = Pattern.compile(
            "(\\d{4,}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2})(?:\\.(\\d+))?(Z|[+-]\\d{2}:\\d{2})");

    /**
     * Parse a decimal number of {@code unit} (e.g. {@code "-0.5"} seconds) into the target unit.
     * The fraction may have more digits than the target unit can represent; the rounding
     * mode decides the outcome (UNNECESSARY fails).
     */
    public static TimeConverter.Result parseDecimal(String text, Unit unit, Unit target, Rounding mode) {
        Matcher m = DECIMAL.matcher(text == null ? "" : text.trim());
        if (!m.matches()) {
            throw new ConvertException(ErrorCode.INVALID_TEXT,
                    "not a decimal number: '" + text + "' (expected e.g. 123 or -1.25, no exponent)");
        }
        String sign = m.group(1);
        String intPart = m.group(2);
        String fracPart = m.group(3) == null ? "" : m.group(3);
        checkFractionLength(fracPart, text);

        // value_in_unit = (intPart + frac/10^f)  =>  scaled = int*10^f + frac, denominator 10^f
        BigInteger scale = BigInteger.TEN.pow(fracPart.length());
        BigInteger scaled = new BigInteger(intPart).multiply(scale);
        if (!fracPart.isEmpty()) {
            scaled = scaled.add(new BigInteger(fracPart));
        }
        if ("-".equals(sign)) {
            scaled = scaled.negate();
        }
        // target value = scaled * unit.nanos / (10^f * target.nanos)
        BigInteger numerator = scaled.multiply(unit.nanosPerUnit);
        BigInteger denominator = scale.multiply(target.nanosPerUnit);
        return TimeConverter.divide(numerator, denominator, mode);
    }

    /**
     * Parse an ISO-8601 instant with optional fractional seconds (any number of digits)
     * into the target unit. Supports negative epochs, e.g. {@code 1969-12-31T23:59:59.5Z}.
     */
    public static TimeConverter.Result parseIsoInstant(String text, Unit target, Rounding mode) {
        Matcher m = ISO_INSTANT.matcher(text == null ? "" : text.trim());
        if (!m.matches()) {
            throw new ConvertException(ErrorCode.INVALID_TEXT,
                    "not an ISO-8601 instant: '" + text + "' (expected e.g. 1969-12-31T23:59:59.5Z)");
        }
        String secondPart = m.group(1);
        String fracPart = m.group(2) == null ? "" : m.group(2);
        String offset = m.group(3);
        checkFractionLength(fracPart, text);

        // Epoch second of the whole-second part, computed exactly by java.time (integer arithmetic).
        final long epochSecond;
        try {
            if ("Z".equals(offset)) {
                epochSecond = Instant.parse(secondPart + "Z").getEpochSecond();
            } else {
                epochSecond = OffsetDateTime.parse(secondPart + offset).toInstant().getEpochSecond();
            }
        } catch (DateTimeParseException e) {
            throw new ConvertException(ErrorCode.INVALID_TEXT, "invalid ISO-8601 instant: '" + text + "'");
        }

        // value_nanos = epochSecond*1e9 + frac*1e9/10^f  (fraction is always a non-negative offset)
        int f = fracPart.length();
        BigInteger scale = BigInteger.TEN.pow(f);
        BigInteger frac = fracPart.isEmpty() ? BigInteger.ZERO : new BigInteger(fracPart);
        BigInteger numerator = BigInteger.valueOf(epochSecond).multiply(scale).add(frac)
                .multiply(Unit.SECOND.nanosPerUnit);
        BigInteger denominator = scale.multiply(target.nanosPerUnit);
        return TimeConverter.divide(numerator, denominator, mode);
    }

    private static void checkFractionLength(String fracPart, String original) {
        if (fracPart.length() > MAX_FRACTION_DIGITS) {
            throw new ConvertException(ErrorCode.INVALID_TEXT,
                    "fraction has " + fracPart.length() + " digits, maximum is " + MAX_FRACTION_DIGITS
                            + ": '" + abbreviate(original) + "'");
        }
    }

    private static String abbreviate(String s) {
        return s.length() <= 64 ? s : s.substring(0, 61) + "...";
    }
}
