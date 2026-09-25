package com.example.seg;

/**
 * 零依赖的最小测试框架：每个测试方法抛异常即失败，main 汇总通过/失败数，
 * 有失败时以非零退出码结束（方便脚本/CI 判断）。
 */
public abstract class TestCase {

    private int passed;
    private int failed;

    protected abstract void run() throws Exception;

    protected void check(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    protected void checkEq(Object actual, Object expected, String message) {
        boolean eq = (actual == null) ? expected == null : actual.equals(expected);
        if (!eq) {
            throw new AssertionError(message + " — 期望: [" + expected + "] 实际: [" + actual + "]");
        }
    }

    protected void test(String name, RunnableEx body) {
        try {
            body.run();
            passed++;
            System.out.println("  PASS " + name);
        } catch (Throwable t) {
            failed++;
            System.out.println("  FAIL " + name + " -> " + t.getMessage());
        }
    }

    @FunctionalInterface
    protected interface RunnableEx {
        void run() throws Exception;
    }

    public static int run(TestCase tc) throws Exception {
        tc.run();
        System.out.println("结果: " + tc.passed + " 通过, " + tc.failed + " 失败");
        return tc.failed == 0 ? 0 : 1;
    }
}
