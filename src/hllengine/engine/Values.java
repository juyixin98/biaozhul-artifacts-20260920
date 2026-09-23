package hllengine.engine;

/**
 * Comparison semantics for JSON scalars stored in rows.
 *
 * <p>Equality follows JSON value semantics: numbers compare numerically
 * ({@code 1} equals {@code 1.0}); strings/booleans/null compare by value;
 * values of different non-numeric kinds are never equal. Ordering is defined
 * for numbers and for strings only; comparing incomparable kinds yields
 * neither less, equal nor greater (comparison result {@code 2}), so a
 * comparison predicate evaluates to false rather than throwing mid-query.
 */
public final class Values {

    private Values() {
    }

    public static boolean equal(Object a, Object b) {
        if (a == null || b == null) {
            return a == null && b == null;
        }
        if (a instanceof Number && b instanceof Number) {
            return ((Number) a).doubleValue() == ((Number) b).doubleValue();
        }
        if (a instanceof Boolean && b instanceof Boolean) {
            return ((Boolean) a) == ((Boolean) b);
        }
        if (a.getClass() != b.getClass()) {
            return false;
        }
        return a.equals(b);
    }

    /**
     * @return -1, 0, 1 for comparable values; 2 when the pair cannot be ordered
     *         (different types, non-numeric non-string values).
     */
    public static int compare(Object a, Object b) {
        if (a instanceof Number && b instanceof Number) {
            return Double.compare(((Number) a).doubleValue(), ((Number) b).doubleValue());
        }
        if (a instanceof String && b instanceof String) {
            return ((String) a).compareTo((String) b);
        }
        return 2;
    }
}
