package cep.test;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖迷你测试框架：提供静态断言与测试注册/执行，失败时收集堆栈信息并返回非零退出码。
 */
public final class TestFramework {

    private final List<TestCase> cases = new ArrayList<>();
    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public void addTest(String name, Runnable test) {
        cases.add(new TestCase(name, test));
    }

    public int run() {
        long t0 = System.nanoTime();
        for (TestCase tc : cases) {
            try {
                tc.body.run();
                passed++;
                System.out.println("  PASS  " + tc.name);
            } catch (AssertionError | Exception t) {
                failed++;
                String detail = t.getClass().getSimpleName() + ": " + t.getMessage();
                failures.add(tc.name + " —— " + detail);
                System.out.println("  FAIL  " + tc.name + "  [" + detail + "]");
            }
        }
        double ms = (System.nanoTime() - t0) / 1_000_000.0;
        System.out.printf("%n共 %d 个测试：通过 %d，失败 %d，耗时 %.1f ms%n",
                cases.size(), passed, failed, ms);
        if (failed > 0) {
            System.out.println("\n失败明细:");
            failures.forEach(f -> System.out.println("  - " + f));
        }
        return failed == 0 ? 0 : 1;
    }

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertFalse(boolean cond, String message) {
        assertTrue(!cond, message);
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (!java.util.Objects.equals(expected, actual)) {
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

    private record TestCase(String name, Runnable body) {}
}
