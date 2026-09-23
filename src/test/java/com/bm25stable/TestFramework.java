package com.bm25stable;

import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖迷你测试框架：TestCase 注解 + 反射扫描 + 断言工具。
 */
public final class TestFramework {

    private TestFramework() {
    }

    @Retention(RetentionPolicy.RUNTIME)
    public @interface TestCase {
    }

    private static int passed;
    private static int failed;
    private static final List<String> failures = new ArrayList<>();

    public static void run(Class<?> testClass) {
        System.out.println("== " + testClass.getSimpleName() + " ==");
        for (var method : testClass.getDeclaredMethods()) {
            if (!method.isAnnotationPresent(TestCase.class)) {
                continue;
            }
            method.setAccessible(true);
            try {
                method.invoke(null);
                passed++;
                System.out.println("  PASS " + method.getName());
            } catch (Exception e) {
                failed++;
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                failures.add(testClass.getSimpleName() + "." + method.getName() + " -> " + cause);
                System.out.println("  FAIL " + method.getName() + " -> " + cause);
            }
        }
    }

    public static boolean summary() {
        System.out.println();
        System.out.println("passed=" + passed + " failed=" + failed);
        if (!failures.isEmpty()) {
            System.out.println("failures:");
            for (String f : failures) {
                System.out.println("  " + f);
            }
        }
        return failed == 0;
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError("expected true: " + message);
        }
    }

    public static void assertFalse(boolean condition, String message) {
        if (condition) {
            throw new AssertionError("expected false: " + message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " | expected=" + expected + " actual=" + actual);
        }
    }

    public static void assertClose(double expected, double actual, double tolerance, String message) {
        if (Math.abs(expected - actual) > tolerance) {
            throw new AssertionError(message + " | expected=" + expected + " actual=" + actual
                    + " tolerance=" + tolerance);
        }
    }

    public static <T extends Throwable> T assertThrows(Class<T> type, Runnable action, String message) {
        try {
            action.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) {
                return type.cast(t);
            }
            throw new AssertionError(message + " | expected " + type.getSimpleName()
                    + " but got " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError(message + " | expected " + type.getSimpleName() + " but nothing was thrown");
    }
}
