package com.example.watermark.test;

/**
 * Tiny assertion library so the project needs no external test dependency.
 */
public final class Assert {

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionFailure(message);
        }
    }

    public static void assertTrue(boolean condition) {
        assertTrue(condition, "expected true");
    }

    public static void assertFalse(boolean condition, String message) {
        if (condition) {
            throw new AssertionFailure(message);
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionFailure(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual) {
        assertEquals(expected, actual, "values differ");
    }

    public static void assertEquals(double expected, double actual, double delta, String message) {
        if (Math.abs(expected - actual) > delta) {
            throw new AssertionFailure(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(double expected, double actual, double delta) {
        assertEquals(expected, actual, delta, "values differ");
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionFailure(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(Object expected, Object actual) {
        assertEquals(expected, actual, "values differ");
    }

    public static void assertNotNull(Object value, String message) {
        if (value == null) {
            throw new AssertionFailure(message);
        }
    }

    public static void assertNull(Object value, String message) {
        if (value != null) {
            throw new AssertionFailure(message + " — was <" + value + ">");
        }
    }

    /** Assert that {@code runnable} throws an exception whose message contains {@code contains}. */
    public static void assertThrows(Class<? extends Throwable> expected, String contains,
                                    Runnable runnable, String message) {
        try {
            runnable.run();
        } catch (Throwable t) {
            if (!expected.isInstance(t)) {
                throw new AssertionFailure(message + " — expected exception "
                        + expected.getSimpleName() + " but got " + t.getClass().getSimpleName());
            }
            if (contains != null && (t.getMessage() == null || !t.getMessage().contains(contains))) {
                throw new AssertionFailure(message + " — message <" + t.getMessage()
                        + "> did not contain <" + contains + ">");
            }
            return;
        }
        throw new AssertionFailure(message + " — expected " + expected.getSimpleName()
                + " but nothing was thrown");
    }

    private Assert() {
    }
}
