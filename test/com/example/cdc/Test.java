package com.example.cdc;

import java.util.ArrayList;
import java.util.List;

/** 零依赖迷你测试框架：断言 + 用例注册 + 汇总退出码。 */
final class Test {

    @FunctionalInterface
    interface Case {
        void run() throws Exception;
    }

    private static final List<Case> CASES = new ArrayList<>();
    private static int passed = 0;

    static void it(String name, Case c) {
        CASES.add(() -> {
            try {
                c.run();
                passed++;
                System.out.println("  PASS  " + name);
            } catch (AssertionError | Exception e) {
                System.out.println("  FAIL  " + name);
                System.out.println("        " + e);
                for (StackTraceElement ste : e.getStackTrace()) {
                    if (ste.getClassName().startsWith("com.example.cdc")) {
                        System.out.println("          at " + ste);
                    }
                }
                throw e;
            }
        });
    }

    static void run() {
        int failed = 0;
        for (Case c : CASES) {
            try {
                c.run();
            } catch (Throwable t) {
                failed++;
            }
        }
        System.out.println();
        System.out.println("结果: " + passed + " 通过, " + failed + " 失败, 共 " + CASES.size());
        if (failed > 0) {
            System.exit(1);
        }
    }

    static void check(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    static void eq(Object actual, Object expected, String msg) {
        if (!java.util.Objects.equals(actual, expected)) {
            throw new AssertionError(msg + "\n        期望: " + expected + "\n        实际: " + actual);
        }
    }

    static void fail(String msg) {
        throw new AssertionError(msg);
    }

    private Test() {
    }
}
