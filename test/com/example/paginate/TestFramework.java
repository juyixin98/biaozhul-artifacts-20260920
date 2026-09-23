package com.example.paginate;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简测试框架（零依赖）：收集断言失败，最后统一汇总并设置退出码。
 */
public final class TestFramework {

    public interface TestCase {
        void run() throws Exception;
    }

    private final String suiteName;
    private int passed;
    private final List<String> failures = new ArrayList<>();

    public TestFramework(String suiteName) {
        this.suiteName = suiteName;
    }

    public void run(String name, TestCase tc) {
        try {
            tc.run();
            passed++;
            System.out.println("  [PASS] " + name);
        } catch (AssertionError | Exception e) {
            failures.add(name + " -> " + e.getMessage());
            System.out.println("  [FAIL] " + name + " -> " + e.getMessage());
        }
    }

    /** @return 全部通过返回 0，否则返回 1 */
    public int summary() {
        System.out.println();
        System.out.println(suiteName + "：通过 " + passed + "，失败 " + failures.size());
        if (!failures.isEmpty()) {
            System.out.println("失败用例：");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            return 1;
        }
        return 0;
    }

    // ---------- 断言 ----------

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + "（期望 " + expected + "，实际 " + actual + "）");
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + "（期望 " + expected + "，实际 " + actual + "）");
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }
}
