package qsummary;

/** Tiny dependency-free assertion harness used by all test classes. */
final class Asserts {

    private int checks;
    private final String suite;

    Asserts(String suite) {
        this.suite = suite;
    }

    void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            throw new AssertionError("[" + suite + "] FAIL: " + message);
        }
    }

    void checkEq(long actual, long expected, String message) {
        checks++;
        if (actual != expected) {
            throw new AssertionError("[" + suite + "] FAIL: " + message
                    + " (expected " + expected + ", got " + actual + ")");
        }
    }

    void checkEq(Object actual, Object expected, String message) {
        checks++;
        if (!expected.equals(actual)) {
            throw new AssertionError("[" + suite + "] FAIL: " + message
                    + " (expected " + expected + ", got " + actual + ")");
        }
    }

    int checks() {
        return checks;
    }
}
