package com.example.segmenter.tests;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简自研测试框架（零依赖）。统计通过/失败数，失败时抛出
 * {@link AssertionError} 风格的异常并在汇总后以非零码退出。
 */
public final class TestFramework {

    private TestFramework() {
    }

    private static int passed;
    private static int failed;
    private static final List<String> failures = new ArrayList<>();
    private static String currentSuite = "";

    public static void suite(String name) {
        currentSuite = name;
        System.out.println("== suite: " + name);
    }

    public static void check(String name, boolean condition) {
        if (condition) {
            passed++;
            System.out.println("  [PASS] " + name);
        } else {
            failed++;
            String record = currentSuite + " :: " + name;
            failures.add(record);
            System.out.println("  [FAIL] " + name);
        }
    }

    public static void fail(String name, String detail) {
        failed++;
        String record = currentSuite + " :: " + name + " — " + detail;
        failures.add(record);
        System.out.println("  [FAIL] " + name + " — " + detail);
    }

    public static void assertEquals(String name, Object expected, Object actual) {
        boolean ok = expected == null ? actual == null : expected.equals(actual);
        if (!ok) {
            fail(name, "expected=<" + expected + "> actual=<" + actual + ">");
        } else {
            check(name, true);
        }
    }

    public static int passed() {
        return passed;
    }

    public static int failed() {
        return failed;
    }

    public static List<String> failures() {
        return failures;
    }

    public static int finish() {
        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("tests passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            System.out.println("FAILED CASES:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            return 1;
        }
        System.out.println("ALL TESTS PASSED");
        return 0;
    }
}
