package com.example.iview;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test kit. Collects failures; {@link #finish()} prints
 * the summary and returns a process exit code (0 = all passed).
 */
public final class TestKit {

    private final String suiteName;
    private final List<String> failures = new ArrayList<>();
    private int checks;

    public TestKit(String suiteName) {
        this.suiteName = suiteName;
    }

    public void check(boolean condition, String message) {
        checks++;
        if (!condition) {
            failures.add(message);
            System.out.println("  FAIL: " + message);
        }
    }

    public void checkEq(Object expected, Object actual, String message) {
        checks++;
        boolean eq = deepEquals(expected, actual);
        if (!eq) {
            String detail = message + " [expected=" + expected + ", actual=" + actual + "]";
            failures.add(detail);
            System.out.println("  FAIL: " + detail);
        }
    }

    /** Equality with numeric coercion (Long vs Integer, BigDecimal vs Long...). */
    private static boolean deepEquals(Object expected, Object actual) {
        if (expected == null || actual == null) {
            return expected == null && actual == null;
        }
        if (expected instanceof Number && actual instanceof Number) {
            return new java.math.BigDecimal(expected.toString())
                    .compareTo(new java.math.BigDecimal(actual.toString())) == 0;
        }
        return expected.equals(actual);
    }

    public void section(String name) {
        System.out.println("- " + name);
    }

    /** @return true if every check so far passed */
    public boolean ok() {
        return failures.isEmpty();
    }

    /** @return process exit code */
    public int finish() {
        System.out.println(suiteName + ": " + checks + " checks, "
                + failures.size() + " failures");
        return failures.isEmpty() ? 0 : 1;
    }
}
