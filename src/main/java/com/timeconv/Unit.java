package com.timeconv;

import java.math.BigInteger;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Supported timestamp units, from seconds down to nanoseconds.
 * Each unit is an exact power-of-ten multiple of the nanosecond.
 */
public enum Unit {
    SECOND(BigInteger.valueOf(1_000_000_000L), "s", "sec", "secs", "second", "seconds"),
    MILLISECOND(BigInteger.valueOf(1_000_000L), "ms", "milli", "millis", "millisecond", "milliseconds"),
    MICROSECOND(BigInteger.valueOf(1_000L), "us", "micro", "micros", "microsecond", "microseconds"),
    NANOSECOND(BigInteger.ONE, "ns", "nano", "nanos", "nanosecond", "nanoseconds");

    /** How many nanoseconds one unit of this type contains. Always a positive power of ten. */
    public final BigInteger nanosPerUnit;

    private final String[] aliases;

    Unit(BigInteger nanosPerUnit, String... aliases) {
        this.nanosPerUnit = nanosPerUnit;
        this.aliases = aliases;
    }

    private static final Map<String, Unit> LOOKUP = new LinkedHashMap<>();
    static {
        for (Unit u : values()) {
            LOOKUP.put(u.name(), u);
            LOOKUP.put(u.name().toLowerCase(), u);
            for (String a : u.aliases) {
                LOOKUP.put(a, u);
            }
        }
    }

    /**
     * Resolve a unit name (enum name or alias, case-insensitive for aliases).
     *
     * @throws ConvertException with code INVALID_UNIT if the name is not supported
     */
    public static Unit parse(String name) {
        if (name == null) {
            throw new ConvertException(ErrorCode.INVALID_UNIT, "unit is required");
        }
        Unit u = LOOKUP.get(name);
        if (u == null) {
            u = LOOKUP.get(name.trim());
        }
        if (u == null) {
            throw new ConvertException(ErrorCode.INVALID_UNIT,
                    "unsupported unit: '" + name + "' (supported: SECOND, MILLISECOND, MICROSECOND, NANOSECOND)");
        }
        return u;
    }
}
