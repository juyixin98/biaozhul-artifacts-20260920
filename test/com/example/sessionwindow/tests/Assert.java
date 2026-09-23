package com.example.sessionwindow.tests;

import java.util.List;
import java.util.Objects;

/** Assertion helpers. */
public final class Assert {

    private Assert() {
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    public static void assertFalse(boolean condition, String message) {
        assertTrue(!condition, message);
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(message + " — expected: <" + expected + "> but was: <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " — expected: <" + expected + "> but was: <" + actual + ">");
        }
    }

    public static void assertEquals(double expected, double actual, double epsilon, String message) {
        if (Math.abs(expected - actual) > epsilon) {
            throw new AssertionError(message + " — expected: <" + expected + "> but was: <" + actual + ">");
        }
    }

    public static void assertNull(Object value, String message) {
        assertTrue(value == null, message + " — expected null but was: <" + value + ">");
    }

    public static void assertNotNull(Object value, String message) {
        assertTrue(value != null, message);
    }

    public static void assertThrows(Class<? extends Throwable> expected, Runnable action, String message) {
        try {
            action.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) {
                return;
            }
            throw new AssertionError(message + " — expected " + expected.getSimpleName()
                    + " but got " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError(message + " — expected " + expected.getSimpleName() + " but nothing was thrown");
    }

    /** Assert that {@code actual} contains {@code expected} as a subsequence. */
    public static <T> void assertSubsequence(List<T> expected, List<T> actual, String message) {
        int i = 0;
        for (T a : actual) {
            if (i < expected.size() && Objects.equals(expected.get(i), a)) {
                i++;
            }
        }
        if (i != expected.size()) {
            throw new AssertionError(message
                    + "\n  expected subsequence: " + expected
                    + "\n  actual:                " + actual);
        }
    }
}
