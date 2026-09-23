package com.example.txflow;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简测试框架（不依赖 JUnit——锁定零第三方依赖）。
 *
 * 用法：
 *   Assert.check(name, cond)
 *   Assert.equals(name, expected, actual)
 * 结束时 Assert.summary()，有失败则 System.exit(1)。
 */
public final class Assert {

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<>();

    private Assert() {
    }

    public static void check(String name, boolean cond) {
        if (cond) {
            passed++;
            System.out.println("  PASS " + name);
        } else {
            failures.add(name + " (expected true)");
            System.out.println("  FAIL " + name);
        }
    }

    public static void equals(String name, Object expected, Object actual) {
        boolean ok = expected == null ? actual == null : expected.equals(actual);
        if (ok) {
            passed++;
            System.out.println("  PASS " + name + " [" + actual + "]");
        } else {
            failures.add(name + " (expected=" + expected + ", actual=" + actual + ")");
            System.out.println("  FAIL " + name + " (expected=" + expected + ", actual=" + actual + ")");
        }
    }

    public static void fail(String name, String detail) {
        failures.add(name + ": " + detail);
        System.out.println("  FAIL " + name + ": " + detail);
    }

    public static boolean allPassed() {
        return failures.isEmpty();
    }

    public static int passedCount() {
        return passed;
    }

    public static int failedCount() {
        return failures.size();
    }

    public static List<String> failures() {
        return failures;
    }

    public static int summary() {
        System.out.println();
        System.out.println("结果：" + passed + " 通过, " + failures.size() + " 失败");
        return failures.isEmpty() ? 0 : 1;
    }
}
