package com.example.intervals.model;

import java.util.Objects;

/**
 * A boundary position in the dense cut ordering of a totally ordered domain.
 *
 * <p>A cut is one of:
 * <ul>
 *   <li>{@code NEG_INFINITY} — below every value;</li>
 *   <li>{@code BELOW(v)}   — the cut immediately <em>below</em> value {@code v},
 *       i.e. a closed (included) endpoint;</li>
 *   <li>{@code ABOVE(v)}   — the cut immediately <em>above</em> value {@code v},
 *       i.e. an open (excluded) endpoint;</li>
 *   <li>{@code POS_INFINITY} — above every value.</li>
 * </ul>
 *
 * <p>For any value {@code v} the total ordering is
 * {@code -∞ < BELOW(v) < v < ABOVE(v) < +∞}, and {@code BELOW(v) < ABOVE(v)}.
 * Open/closed endpoints therefore compare correctly without any special casing
 * in the algebra. This is the classic Guava {@code Cut} encoding, reimplemented
 * here with no third-party dependency beyond Jackson.
 *
 * @param <T> the domain value type (instantiated with a fixed {@code Comparator})
 */
public final class Cut<T> implements Comparable<Cut<T>> {

    public enum Kind { NEG_INFINITY, BELOW, ABOVE, POS_INFINITY }

    private final Kind kind;
    private final T endpoint; // non-null iff kind is BELOW or ABOVE

    private Cut(Kind kind, T endpoint) {
        this.kind = kind;
        this.endpoint = endpoint;
    }

    public static <T> Cut<T> negInfinity() {
        return new Cut<>(Kind.NEG_INFINITY, null);
    }

    public static <T> Cut<T> posInfinity() {
        return new Cut<>(Kind.POS_INFINITY, null);
    }

    /** Closed (included) endpoint at {@code v}. */
    public static <T> Cut<T> below(T v) {
        return new Cut<>(Kind.BELOW, Objects.requireNonNull(v, "endpoint"));
    }

    /** Open (excluded) endpoint at {@code v}. */
    public static <T> Cut<T> above(T v) {
        return new Cut<>(Kind.ABOVE, Objects.requireNonNull(v, "endpoint"));
    }

    public Kind kind() {
        return kind;
    }

    /** @return the finite endpoint value; only valid when {@link #kind()} is BELOW or ABOVE. */
    public T endpoint() {
        if (endpoint == null) {
            throw new IllegalStateException("infinite cut has no endpoint: " + kind);
        }
        return endpoint;
    }

    public boolean isFinite() {
        return endpoint != null;
    }

    public boolean isInfinite() {
        return endpoint == null;
    }

    @Override
    public int compareTo(Cut<T> other) {
        if (kind == Kind.NEG_INFINITY) {
            return other.kind == Kind.NEG_INFINITY ? 0 : -1;
        }
        if (kind == Kind.POS_INFINITY) {
            return other.kind == Kind.POS_INFINITY ? 0 : 1;
        }
        if (other.kind == Kind.NEG_INFINITY) {
            return 1;
        }
        if (other.kind == Kind.POS_INFINITY) {
            return -1;
        }
        // Both cuts are finite and sit around an actual endpoint value.
        @SuppressWarnings("unchecked")
        Comparable<? super T> self = (Comparable<? super T>) endpoint;
        int c = self.compareTo(other.endpoint);
        if (c != 0) {
            return c;
        }
        // Same value: BELOW(v) precedes ABOVE(v) (ordinal BELOW=1 < ABOVE=2).
        return Integer.compare(kind.ordinal(), other.kind.ordinal());
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Cut<?> cut)) {
            return false;
        }
        return kind == cut.kind && Objects.equals(endpoint, cut.endpoint);
    }

    @Override
    public int hashCode() {
        return Objects.hash(kind, endpoint);
    }

    @Override
    public String toString() {
        return switch (kind) {
            case NEG_INFINITY -> "(-∞";
            case POS_INFINITY -> "+∞)";
            case BELOW -> "[" + endpoint;
            case ABOVE -> endpoint + ")";
        };
    }
}
