package com.example.positiondiff;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/**
 * Minimal dependency-free test harness. Tests are named lambdas registered
 * with {@link #test}; assertions throw {@link AssertionError}. A failing
 * assertion is recorded, does not abort the suite, and the JVM exits non-zero
 * if anything failed.
 */
public final class TestRunner {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();
    private final List<String> stack = new ArrayList<>();

    public void test(String name, Runnable body) {
        stack.add(name);
        try {
            body.run();
            passed++;
            System.out.println("PASS " + name);
        } catch (AssertionError | Exception e) {
            failed++;
            String label = String.join(" > ", stack);
            failures.add(label + " :: " + e.getMessage());
            System.out.println("FAIL " + label + " :: " + e.getMessage());
        } finally {
            stack.remove(stack.size() - 1);
        }
    }

    public void group(String name, Runnable body) {
        stack.add(name);
        try {
            body.run();
        } finally {
            stack.remove(stack.size() - 1);
        }
    }

    public void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    public void assertFalse(boolean cond, String msg) {
        if (cond) throw new AssertionError(msg);
    }

    public void assertEquals(Object expected, Object actual, String msg) {
        boolean eq = (expected == null) ? actual == null : expected.equals(actual);
        if (!eq) {
            throw new AssertionError(msg + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public int finish() {
        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("tests: " + (passed + failed) + ", passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            System.out.println();
            for (String f : failures) System.out.println("  - " + f);
            return 1;
        }
        return 0;
    }
}
