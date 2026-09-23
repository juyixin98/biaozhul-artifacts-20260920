package io.example.orderedcommit;

/** Minimal JUnit-free assertion library used by the project's tests. */
final class Asserts {

    private Asserts() {
    }

    static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    static void assertFalse(boolean condition, String message) {
        assertTrue(!condition, message);
    }

    static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " — expected " + expected + " but was " + actual);
        }
    }

    static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " — expected [" + expected + "] but was [" + actual + "]");
        }
    }

    static void assertInstanceOf(Class<?> expected, Object actual, String message) {
        if (actual == null || !expected.isInstance(actual)) {
            throw new AssertionError(
                    message + " — expected instance of " + expected.getName()
                            + " but was " + (actual == null ? "null" : actual.getClass().getName()));
        }
    }

    /** Runs the runnable and asserts it throws an exception of {@code expectedType}. */
    static void assertThrows(
            Class<? extends Throwable> expectedType, ThrowingRunnable body, String message) {
        try {
            body.run();
        } catch (Throwable t) {
            if (expectedType.isInstance(t)) {
                return;
            }
            throw new AssertionError(
                    message + " — expected " + expectedType.getName() + " but got "
                            + t.getClass().getName() + ": " + t.getMessage());
        }
        throw new AssertionError(message + " — expected " + expectedType.getName() + " but nothing was thrown");
    }

    @FunctionalInterface
    interface ThrowingRunnable {
        void run() throws Throwable;
    }

    @FunctionalInterface
    interface Condition {
        void run() throws Exception;
    }

    /**
     * Polls {@code condition} until it returns true or {@code timeoutMillis}
     * elapses. Keeps tests fast in the happy path while tolerating scheduling
     * jitter.
     */
    static void waitFor(long timeoutMillis, String description, Condition condition)
            throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        AssertionError lastFailure = null;
        while (System.currentTimeMillis() < deadline) {
            try {
                condition.run();
                return;
            } catch (AssertionError e) {
                lastFailure = e;
                Thread.sleep(10);
            }
        }
        throw new AssertionError("timed out waiting for: " + description
                + (lastFailure != null ? " (" + lastFailure.getMessage() + ")" : ""));
    }
}
