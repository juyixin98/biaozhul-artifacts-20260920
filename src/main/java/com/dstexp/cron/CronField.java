package com.dstexp.cron;

import java.util.BitSet;
import java.util.Locale;

/**
 * One field of a five-field cron expression, parsed into a 64-bit bitset of accepted values.
 * Supports {@code *}, numeric lists ({@code a,b,c}), ranges ({@code a-b}), and steps
 * ({@code *&#47;n}, {@code a-b/n}, {@code a/n}). Month and day-of-week also accept the usual
 * three-letter English names (JAN..DEC, SUN..SAT).
 */
final class CronField {

    private final String name;
    private final int min;
    private final int max;
    private final BitSet allowed;
    private final boolean wildcard;

    private CronField(String name, int min, int max, BitSet allowed, boolean wildcard) {
        this.name = name;
        this.min = min;
        this.max = max;
        this.allowed = allowed;
        this.wildcard = wildcard;
    }

    boolean isWildcard() {
        return wildcard;
    }

    boolean matches(int value) {
        return value >= min && value <= max && allowed.get(value);
    }

    /** Values of this field in ascending order that lie within [lo, hi]. */
    int[] valuesBetween(int lo, int hi) {
        BitSet window = allowed.get(lo, hi + 1);
        int[] out = new int[window.cardinality()];
        int i = 0;
        for (int v = window.nextSetBit(0); v >= 0; v = window.nextSetBit(v + 1)) {
            out[i++] = v + lo;
        }
        return out;
    }

    static CronField parse(String raw, String name, int min, int max, String[] names) {
        if (raw == null || raw.isBlank()) {
            throw new IllegalArgumentException("Cron field '" + name + "' is empty");
        }
        String text = raw.trim().toUpperCase(Locale.ROOT);
        boolean wildcard = text.equals("*") || text.equals("?");
        BitSet bits = new BitSet(max + 1);
        for (String part : text.split(",")) {
            parsePart(part, name, min, max, names, bits);
        }
        if (bits.isEmpty()) {
            throw new IllegalArgumentException("Cron field '" + name + "' matches no values: " + raw);
        }
        return new CronField(name, min, max, bits, wildcard);
    }

    /** Builds a field from pre-computed allowed values, preserving the wildcard flag. */
    static CronField fromBits(String name, int min, int max, BitSet bits, boolean wildcard) {
        if (bits.isEmpty()) {
            throw new IllegalArgumentException("Cron field '" + name + "' matches no values");
        }
        return new CronField(name, min, max, bits, wildcard);
    }

    private static void parsePart(String part, String name, int min, int max,
                                  String[] names, BitSet bits) {
        String stepText = part;
        int step = 1;
        int slash = part.indexOf('/');
        if (slash >= 0) {
            stepText = part.substring(0, slash);
            String stepRaw = part.substring(slash + 1);
            if (stepRaw.isEmpty() || stepText.isEmpty()) {
                throw new IllegalArgumentException("Malformed step in cron field '" + name + "': " + part);
            }
            step = parseNumber(stepRaw, name, names);
            if (step <= 0) {
                throw new IllegalArgumentException("Step must be positive in field '" + name + "': " + part);
            }
        }

        int rangeLo;
        int rangeHi;
        if (stepText.equals("*") || stepText.equals("?")) {
            rangeLo = min;
            rangeHi = max;
        } else if (stepText.contains("-")) {
            String[] ends = stepText.split("-", -1);
            if (ends.length != 2 || ends[0].isEmpty() || ends[1].isEmpty()) {
                throw new IllegalArgumentException("Malformed range in cron field '" + name + "': " + part);
            }
            rangeLo = parseNumber(ends[0], name, names);
            rangeHi = parseNumber(ends[1], name, names);
        } else {
            rangeLo = parseNumber(stepText, name, names);
            rangeHi = slash >= 0 ? max : rangeLo;
        }

        if (rangeLo < min || rangeHi > max || rangeLo > rangeHi) {
            throw new IllegalArgumentException(String.format(
                    "Value out of range in cron field '%s': %s (allowed %d..%d)", name, part, min, max));
        }
        for (int v = rangeLo; v <= rangeHi; v += step) {
            bits.set(v);
        }
    }

    private static int parseNumber(String token, String name, String[] names) {
        for (int i = 0; i < names.length; i++) {
            if (names[i] != null && names[i].equals(token)) {
                return i;
            }
        }
        try {
            return Integer.parseInt(token);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("Not a number in cron field '" + name + "': " + token, e);
        }
    }

    static final String[] NO_NAMES = new String[0];
    static final String[] MONTH_NAMES = {
            null, "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
            "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"
    };
    static final String[] DOW_NAMES = {
            "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"
    };
}
