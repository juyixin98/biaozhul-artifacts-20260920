package hlc;

import java.util.Objects;

/**
 * An immutable Hybrid Logical Clock timestamp: {@code (l, c)} where {@code l} is the HLC
 * logical wall time in <b>microseconds</b> and {@code c} is the non-negative logical counter.
 *
 * <p>Ordering is the lexicographic order on {@code (l, c)}. It is <b>not</b> the happens-
 * before relation: timestamps are totally ordered (with node ids breaking ties only in wire
 * messages), while causality is partial. A smaller timestamp never implies the event
 * happened-before a larger one — concurrent events are simply ordered by their clocks.
 */
public final class HLCTimestamp implements Comparable<HLCTimestamp> {

    public static final HLCTimestamp ZERO = new HLCTimestamp(0L, 0L);

    private final long l;
    private final long c;

    public HLCTimestamp(long l, long c) {
        if (l < 0) {
            throw new HLCException("logical wall time l must be non-negative, got " + l);
        }
        if (c < 0) {
            throw new HLCException("logical counter c must be non-negative, got " + c);
        }
        this.l = l;
        this.c = c;
    }

    public long l() {
        return l;
    }

    public long c() {
        return c;
    }

    /** Lexicographic comparison on {@code (l, c)} (both components are non-negative). */
    @Override
    public int compareTo(HLCTimestamp o) {
        int cmpL = Long.compare(l, o.l);
        if (cmpL != 0) {
            return cmpL;
        }
        return Long.compare(c, o.c);
    }

    public boolean isBefore(HLCTimestamp o) {
        return compareTo(o) < 0;
    }

    public boolean isAfter(HLCTimestamp o) {
        return compareTo(o) > 0;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof HLCTimestamp other)) {
            return false;
        }
        return l == other.l && c == other.c;
    }

    @Override
    public int hashCode() {
        return Objects.hash(l, c);
    }

    /** Canonical wire/storage form {@code <l>:<c>}, e.g. {@code 1700000000000000:42}. */
    @Override
    public String toString() {
        return l + ":" + c;
    }

    /** Parses the canonical {@code <l>:<c>} form. Fails fast on malformed input. */
    public static HLCTimestamp parse(String text) {
        if (text == null) {
            throw new HLCException("timestamp text is null");
        }
        int idx = text.indexOf(':');
        if (idx <= 0 || idx == text.length() - 1) {
            throw new HLCException("malformed HLC timestamp (expected '<l>:<c>'): " + text);
        }
        try {
            long parsedL = Long.parseLong(text.substring(0, idx));
            long parsedC = Long.parseLong(text.substring(idx + 1));
            return new HLCTimestamp(parsedL, parsedC);
        } catch (NumberFormatException e) {
            throw new HLCException("malformed HLC timestamp (expected '<l>:<c>'): " + text, e);
        }
    }
}
