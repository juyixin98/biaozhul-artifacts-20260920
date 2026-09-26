package com.example.intervals.model;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;

import java.util.Objects;

/**
 * An immutable convex interval over a totally ordered domain, expressed as a
 * pair of {@link Cut}s.
 *
 * <p>The four classical endpoint kinds are represented exactly by the cuts:
 * <pre>
 *   [a, b]  lower = BELOW(a)   upper = ABOVE(b)
 *   (a, b)  lower = ABOVE(a)   upper = BELOW(b)
 *   [a, b)  lower = BELOW(a)   upper = BELOW(b)
 *   (a, b]  lower = ABOVE(a)   upper = ABOVE(b)
 * </pre>
 * Either end may be the infinite cut. An interval whose endpoint values are
 * reversed (e.g. {@code [5, 2]}) is rejected at construction; degenerate
 * zero-length forms at equal values ({@code (x,x)}, {@code [x,x)}, {@code (x,x]})
 * are detectable via {@link #isEmpty()} — only the singleton {@code [x,x]} is
 * non-empty.
 */
public final class Interval<T extends Comparable<? super T>> {

    private final Cut<T> lower;
    private final Cut<T> upper;

    private Interval(Cut<T> lower, Cut<T> upper) {
        this.lower = lower;
        this.upper = upper;
    }

    /**
     * Constructs an interval from explicit cuts.
     *
     * @throws IntervalException {@code reversed_interval} if the finite endpoint
     *         values are in descending order
     */
    public static <T extends Comparable<? super T>> Interval<T> of(Cut<T> lower, Cut<T> upper) {
        Objects.requireNonNull(lower, "lower");
        Objects.requireNonNull(upper, "upper");
        rejectIfReversed(lower, upper);
        return new Interval<>(lower, upper);
    }

    /**
     * Constructs an interval from nullable endpoint values and open/closed
     * flags. A {@code null} endpoint means the corresponding infinity.
     *
     * @param lowerValue nullable lower endpoint value
     * @param lowerOpen  true when the lower endpoint is excluded
     * @param upperValue nullable upper endpoint value
     * @param upperOpen  true when the upper endpoint is excluded
     */
    public static <T extends Comparable<? super T>> Interval<T> between(
            T lowerValue, boolean lowerOpen, T upperValue, boolean upperOpen) {
        Cut<T> lo = lowerValue == null ? Cut.negInfinity()
                : lowerOpen ? Cut.above(lowerValue) : Cut.below(lowerValue);
        Cut<T> hi = upperValue == null ? Cut.posInfinity()
                : upperOpen ? Cut.below(upperValue) : Cut.above(upperValue);
        return of(lo, hi);
    }

    public static <T extends Comparable<? super T>> Interval<T> open(T low, T high) {
        return between(low, true, high, true);
    }

    public static <T extends Comparable<? super T>> Interval<T> closed(T low, T high) {
        return between(low, false, high, false);
    }

    public static <T extends Comparable<? super T>> Interval<T> closedOpen(T low, T high) {
        return between(low, false, high, true);
    }

    public static <T extends Comparable<? super T>> Interval<T> openClosed(T low, T high) {
        return between(low, true, high, false);
    }

    public static <T extends Comparable<? super T>> Interval<T> all() {
        return Interval.of(Cut.<T>negInfinity(), Cut.<T>posInfinity());
    }

    public Cut<T> lowerCut() {
        return lower;
    }

    public Cut<T> upperCut() {
        return upper;
    }

    /** @return true for zero-length forms such as {@code (x,x)} or {@code [x,x)}. */
    public boolean isEmpty() {
        return lower.compareTo(upper) >= 0;
    }

    public boolean contains(T value) {
        Cut<T> below = Cut.below(value);
        Cut<T> above = Cut.above(value);
        return lower.compareTo(below) <= 0 && upper.compareTo(above) >= 0;
    }

    private static <T extends Comparable<? super T>> void rejectIfReversed(Cut<T> lower, Cut<T> upper) {
        if (lower.isInfinite() || upper.isInfinite()) {
            return;
        }
        int cmp = lower.endpoint().compareTo(upper.endpoint());
        if (cmp > 0) {
            throw new IntervalException(ErrorCode.REVERSED_INTERVAL,
                    "reversed interval: lower endpoint " + lower.endpoint()
                            + " is greater than upper endpoint " + upper.endpoint());
        }
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Interval<?> interval)) {
            return false;
        }
        return lower.equals(interval.lower) && upper.equals(interval.upper);
    }

    @Override
    public int hashCode() {
        return Objects.hash(lower, upper);
    }

    @Override
    public String toString() {
        String lo = lower.kind() == Cut.Kind.NEG_INFINITY ? "(-∞"
                : (lower.kind() == Cut.Kind.BELOW ? "[" : "(") + lower.endpoint();
        String hi = upper.kind() == Cut.Kind.POS_INFINITY ? "+∞)"
                : upper.endpoint() + (upper.kind() == Cut.Kind.ABOVE ? "]" : ")");
        return lo + ", " + hi;
    }
}
