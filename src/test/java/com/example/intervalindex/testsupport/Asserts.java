package com.example.intervalindex.testsupport;

/**
 * 零依赖的轻量断言工具：失败抛 {@link AssertionError}，由 TestRunner 统一调度。
 */
public final class Asserts {

    private Asserts() {
    }

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertFalse(boolean cond, String message) {
        if (cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " expected=" + expected + " actual=" + actual);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (!java.util.Objects.equals(expected, actual)) {
            throw new AssertionError(message + " expected=[" + expected + "] actual=[" + actual + "]");
        }
    }

    public static void expectThrows(String message, Runnable r) {
        try {
            r.run();
        } catch (IllegalArgumentException e) {
            return;
        }
        throw new AssertionError(message + " (expected IllegalArgumentException)");
    }
}
