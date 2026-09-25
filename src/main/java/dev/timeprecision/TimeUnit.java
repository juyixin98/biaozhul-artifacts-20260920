package dev.timeprecision;

import java.math.BigInteger;
import java.util.Locale;
import java.util.Map;

/** Supported timestamp units, each an exact power-of-1000 multiple of the nanosecond. */
public enum TimeUnit {
    NANOSECONDS(BigInteger.ONE),
    MICROSECONDS(BigInteger.valueOf(1_000L)),
    MILLISECONDS(BigInteger.valueOf(1_000_000L)),
    SECONDS(BigInteger.valueOf(1_000_000_000L));

    private static final Map<String, TimeUnit> ALIASES = Map.ofEntries(
            Map.entry("NANOSECONDS", NANOSECONDS),
            Map.entry("NANOSECOND", NANOSECONDS),
            Map.entry("NANOS", NANOSECONDS),
            Map.entry("NANO", NANOSECONDS),
            Map.entry("NS", NANOSECONDS),
            Map.entry("MICROSECONDS", MICROSECONDS),
            Map.entry("MICROSECOND", MICROSECONDS),
            Map.entry("MICROS", MICROSECONDS),
            Map.entry("MICRO", MICROSECONDS),
            Map.entry("US", MICROSECONDS),
            Map.entry("MILLISECONDS", MILLISECONDS),
            Map.entry("MILLISECOND", MILLISECONDS),
            Map.entry("MILLIS", MILLISECONDS),
            Map.entry("MILLI", MILLISECONDS),
            Map.entry("MS", MILLISECONDS),
            Map.entry("SECONDS", SECONDS),
            Map.entry("SECOND", SECONDS),
            Map.entry("S", SECONDS));

    private final BigInteger nanosPerUnit;

    TimeUnit(BigInteger nanosPerUnit) {
        this.nanosPerUnit = nanosPerUnit;
    }

    /** Exact count of nanoseconds in one unit of this precision. */
    public BigInteger nanosPerUnit() {
        return nanosPerUnit;
    }

    /**
     * Resolves a unit name (case-insensitive, common aliases accepted).
     *
     * @throws ConversionException with {@link ErrorCode#INVALID_UNIT} when unknown
     */
    public static TimeUnit fromName(String name) {
        if (name == null) {
            throw new ConversionException(ErrorCode.INVALID_UNIT, "unit name is required");
        }
        TimeUnit unit = ALIASES.get(name.trim().toUpperCase(Locale.ROOT));
        if (unit == null) {
            throw new ConversionException(ErrorCode.INVALID_UNIT, "unknown unit: " + name);
        }
        return unit;
    }
}
