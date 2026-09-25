package dev.timeprecision;

import java.math.BigInteger;
import java.util.Objects;

/**
 * Formats integer timestamps as canonical decimal-seconds text, the exact
 * inverse of {@link DecimalSecondsParser}: for any value representable in a
 * supported unit, {@code parse(format(v)) == v}.
 */
public final class DecimalSecondsFormatter {

    private DecimalSecondsFormatter() {
    }

    /**
     * Formats {@code value} (in {@code unit}) as decimal seconds, e.g.
     * {@code -1500 MILLISECONDS -> "-1.5"}, {@code 2 SECONDS -> "2"}.
     * Trailing fractional zeros are stripped; the result never uses
     * scientific notation or floating point.
     */
    public static String format(BigInteger value, TimeUnit unit) {
        Objects.requireNonNull(value, "value");
        Objects.requireNonNull(unit, "unit");

        BigInteger nanos = value.multiply(unit.nanosPerUnit());
        String sign = nanos.signum() < 0 ? "-" : "";
        BigInteger[] parts = nanos.abs()
                .divideAndRemainder(DecimalSecondsParser.NANOS_PER_SECOND);
        String frac = String.format("%09d", parts[1]).replaceAll("0+$", "");
        return sign + parts[0] + (frac.isEmpty() ? "" : "." + frac);
    }
}
