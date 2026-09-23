package com.example.uninorm;

import java.util.Objects;

/** Tiny assertion helpers so the test suite needs no external framework. */
public final class TestSupport {

    private TestSupport() {
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError("assertTrue failed: " + msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        if (cond) {
            throw new AssertionError("assertFalse failed: " + msg);
        }
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(msg + " -- expected=<" + expected
                    + "> but was=<" + actual + ">");
        }
    }

    public static void assertEquals(int expected, int actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " -- expected=<" + expected
                    + "> but was=<" + actual + ">");
        }
    }

    public static void assertThrows(Runnable r, String msg) {
        try {
            r.run();
        } catch (IllegalArgumentException e) {
            return;
        }
        throw new AssertionError("expected IllegalArgumentException: " + msg);
    }
}
