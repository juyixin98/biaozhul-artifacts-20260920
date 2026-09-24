package com.example.diff;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/**
 * Tiny zero-dependency test framework. Each test method is registered as a
 * named Runnable; failures are collected and reported so a full run shows
 * every broken case rather than stopping at the first.
 */
public final class TestFramework {

    public static final class Failure {
        public final String suite;
        public final String name;
        public final String detail;

        Failure(String suite, String name, String detail) {
            this.suite = suite;
            this.name = name;
            this.detail = detail;
        }
    }

    private final List<Failure> failures = new ArrayList<>();
    private int passed = 0;
    private int run = 0;

    public void test(String name, Runnable body) {
        run++;
        try {
            body.run();
            passed++;
        } catch (AssertionError e) {
            failures.add(new Failure(callerSuite(), name, e.getMessage() == null ? "assertion failed" : e.getMessage()));
            System.out.println("FAIL  " + callerSuite() + " :: " + name + " -> " + e.getMessage());
        } catch (Exception e) {
            failures.add(new Failure(callerSuite(), name, e.getClass().getSimpleName() + ": " + e.getMessage()));
            System.out.println("ERROR " + callerSuite() + " :: " + name + " -> " + e);
        }
    }

    private static String callerSuite() {
        String cn = new Throwable().getStackTrace()[2].getClassName();
        int dot = cn.lastIndexOf('.');
        return cn.substring(dot + 1);
    }

    public int failures() {
        return failures.size();
    }

    public int passed() {
        return passed;
    }

    public int run() {
        return run;
    }

    public List<Failure> failureList() {
        return failures;
    }

    // ------------------------------------------------------------------
    // Assertions
    // ------------------------------------------------------------------

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertTrue(boolean cond) {
        assertTrue(cond, "expected true");
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertFalse(boolean cond) {
        assertFalse(cond, "expected false");
    }

    public static void assertEquals(Object expected, Object actual) {
        assertEquals(expected, actual, "expected <" + expected + "> but was <" + actual + ">");
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        boolean eq = (expected == null) ? actual == null : expected.equals(actual);
        assertTrue(eq, msg);
    }

    public static void assertContains(String haystack, String needle) {
        assertTrue(haystack != null && haystack.contains(needle),
                "expected string to contain <" + needle + "> but was <" + haystack + ">");
    }

    public static void fail(String msg) {
        throw new AssertionError(msg);
    }

    /**
     * Runs every registered suite and prints a summary. Returns the process
     * exit code to use (0 = all passed).
     */
    @SafeVarargs
    public static int runSuites(Consumer<TestFramework>... suites) {
        TestFramework tf = new TestFramework();
        for (Consumer<TestFramework> s : suites) {
            s.accept(tf);
        }
        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("tests run: " + tf.run() + ", passed: " + tf.passed()
                + ", failed: " + tf.failures());
        for (Failure f : tf.failureList()) {
            System.out.println("  FAIL " + f.suite + " :: " + f.name + " :: " + f.detail);
        }
        return tf.failures() == 0 ? 0 : 1;
    }
}
