package com.timeconv;

import java.util.ArrayList;
import java.util.List;

/** Minimal zero-dependency test harness: collects failures, exits non-zero on any. */
public final class TestRunner {

    @FunctionalInterface
    public interface TestBody {
        void run();
    }

    private static final List<String> failures = new ArrayList<>();
    private static int passed = 0;

    public static void test(String name, TestBody body) {
        try {
            body.run();
            passed++;
            System.out.println("PASS " + name);
        } catch (Throwable t) {
            failures.add(name + " -> " + t);
            System.out.println("FAIL " + name + " -> " + t);
        }
    }

    public static void assertEquals(Object expected, Object actual) {
        if (!java.util.Objects.equals(expected, actual)) {
            throw new AssertionError("expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError("expected true: " + message);
        }
    }

    public static void assertThrows(ErrorCode code, TestBody body) {
        try {
            body.run();
        } catch (ConvertException e) {
            if (e.code() == code) {
                return;
            }
            throw new AssertionError("expected error " + code + " but got " + e.code() + ": " + e.getMessage());
        }
        throw new AssertionError("expected ConvertException " + code + " but nothing was thrown");
    }

    public static int finish() {
        System.out.println("----");
        System.out.println("passed: " + passed + ", failed: " + failures.size());
        for (String f : failures) {
            System.out.println("FAILED: " + f);
        }
        return failures.isEmpty() ? 0 : 1;
    }

    private TestRunner() {
    }
}
