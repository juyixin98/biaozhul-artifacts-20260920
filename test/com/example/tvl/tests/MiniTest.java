package com.example.tvl.tests;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/** 零依赖迷你测试框架。 */
public final class MiniTest {

    public static final class Suite {
        private final String name;
        private final List<Case> cases = new ArrayList<>();
        private int passed;
        private int failed;
        private final List<String> failures = new ArrayList<>();

        Suite(String name) {
            this.name = name;
        }

        public Suite test(String name, ThrowingRunnable body) {
            cases.add(new Case(name, body));
            return this;
        }

        boolean run() {
            System.out.println("== " + name + " (" + cases.size() + " 个用例) ==");
            for (Case c : cases) {
                try {
                    c.body().run();
                    passed++;
                    System.out.println("  [PASS] " + c.name());
                } catch (AssertionError | Exception e) {
                    failed++;
                    failures.add(c.name() + " -> " + e);
                    System.out.println("  [FAIL] " + c.name() + " : "
                            + e.getClass().getSimpleName() + ": " + e.getMessage());
                    e.printStackTrace(System.out);
                }
            }
            System.out.println("   小计: " + passed + " 通过, " + failed + " 失败");
            return failed == 0;
        }
    }

    /** 允许用例抛出任意异常（含受检异常）。 */
    @FunctionalInterface
    public interface ThrowingRunnable {
        void run() throws Exception;
    }

    private record Case(String name, ThrowingRunnable body) {}

    public static Suite suite(String name) {
        return new Suite(name);
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        boolean equal;
        if (expected instanceof Number en && actual instanceof Number an) {
            // 数值按值比较，避免 Integer/Byte/Long/Double 装箱差异
            if (en instanceof Double || en instanceof Float || an instanceof Double || an instanceof Float) {
                equal = en.doubleValue() == an.doubleValue();
            } else {
                equal = en.longValue() == an.longValue();
            }
        } else {
            equal = expected == null ? actual == null : expected.equals(actual);
        }
        if (!equal) {
            throw new AssertionError(msg + " 期望=<" + expected + "> 实际=<" + actual + ">");
        }
    }

    public static void assertThrows(Class<? extends Throwable> type, String contains, Runnable r) {
        try {
            r.run();
        } catch (Throwable t) {
            if (!type.isInstance(t)) {
                throw new AssertionError("期望异常 " + type.getSimpleName() + "，实际 " + t.getClass().getSimpleName()
                        + ": " + t.getMessage());
            }
            if (contains != null && (t.getMessage() == null || !t.getMessage().contains(contains))) {
                throw new AssertionError("异常信息期望包含 '" + contains + "'，实际: " + t.getMessage());
            }
            return;
        }
        throw new AssertionError("期望抛出 " + type.getSimpleName() + " 但正常返回");
    }

    public static Consumer<Integer> noop() {
        return i -> {};
    }

    private MiniTest() {}
}
