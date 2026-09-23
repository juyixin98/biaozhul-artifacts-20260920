package test;

/** Tiny assertion helpers (no JUnit dependency). */
public final class Asserts {

    private Asserts() {
    }

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertTrue(boolean cond) {
        assertTrue(cond, "expected true");
    }

    public static void assertFalse(boolean cond, String message) {
        if (cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null
                : (expected instanceof Number && actual instanceof Number
                        ? ((Number) expected).longValue() != ((Number) actual).longValue()
                        : !expected.equals(actual))) {
            throw new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertGreater(long actual, long threshold, String message) {
        if (!(actual > threshold)) {
            throw new AssertionError(message + " — <" + actual + "> not > <" + threshold + ">");
        }
    }

    public static void assertGreaterEqual(long actual, long threshold, String message) {
        if (!(actual >= threshold)) {
            throw new AssertionError(message + " — <" + actual + "> not >= <" + threshold + ">");
        }
    }

    public static void assertLessEqual(long actual, long threshold, String message) {
        if (!(actual <= threshold)) {
            throw new AssertionError(message + " — <" + actual + "> not <= <" + threshold + ">");
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }
}
