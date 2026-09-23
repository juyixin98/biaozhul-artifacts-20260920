package dedup.test;

/** Minimal assertion helpers, modeled after the JUnit subset everyone actually uses. */
public final class Assert {

    private Assert() {
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    public static void assertTrue(boolean condition) {
        assertTrue(condition, "expected true");
    }

    public static void assertFalse(boolean condition, String message) {
        if (condition) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " — expected <" + expected
                    + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(Object expected, Object actual) {
        assertEquals(expected, actual, "values differ");
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " — expected <" + expected
                    + "> but was <" + actual + ">");
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }

    /** Runs the given block and asserts it throws the expected exception type. */
    @SuppressWarnings("unchecked")
    public static <T extends Throwable> T assertThrows(Class<T> expected, ThrowingRunnable r) {
        try {
            r.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) {
                return (T) t;
            }
            throw new AssertionError("Expected " + expected.getSimpleName()
                    + " but got " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError("Expected " + expected.getSimpleName() + " but nothing was thrown");
    }

    @FunctionalInterface
    public interface ThrowingRunnable {
        void run() throws Throwable;
    }
}
