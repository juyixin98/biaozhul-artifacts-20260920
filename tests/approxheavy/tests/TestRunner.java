package approxheavy.tests;

import java.util.ArrayList;
import java.util.List;

/** Tiny zero-dependency test harness: static asserts, @Test-style main methods. */
public final class TestRunner {
    @FunctionalInterface
    public interface TestCase {
        void run() throws Exception;
    }

    private final String suiteName;
    private final List<NamedCase> cases = new ArrayList<>();

    private static final class NamedCase {
        final String name;
        final Runnable body;

        NamedCase(String name, Runnable body) {
            this.name = name;
            this.body = body;
        }
    }

    public TestRunner(String suiteName) {
        this.suiteName = suiteName;
    }

    public TestRunner add(String name, TestCase testCase) {
        cases.add(new NamedCase(name, () -> {
            try {
                testCase.run();
                System.out.println("  PASS " + name);
            } catch (AssertionError | Exception e) {
                throw new AssertionError(name + ": " + e.getMessage(), e);
            }
        }));
        return this;
    }

    public void run() {
        int failures = 0;
        System.out.println(suiteName);
        for (NamedCase c : cases) {
            try {
                c.body.run();
            } catch (AssertionError e) {
                failures++;
                System.out.println("  FAIL " + e.getMessage());
            }
        }
        if (failures > 0) {
            System.out.println(suiteName + ": " + failures + " FAILURE(S)");
            System.exit(1);
        }
        System.out.println(suiteName + ": all " + cases.size() + " tests passed");
    }

    public static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    public static void checkEq(long actual, long expected, String message) {
        if (actual != expected) {
            throw new AssertionError(message + " (expected=" + expected + ", actual=" + actual + ")");
        }
    }

    public static void checkEq(String actual, String expected, String message) {
        if (!actual.equals(expected)) {
            throw new AssertionError(message + " (expected=" + expected + ", actual=" + actual + ")");
        }
    }
}
