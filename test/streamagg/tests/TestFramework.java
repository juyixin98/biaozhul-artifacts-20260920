package streamagg.tests;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/** 零依赖迷你测试框架：断言计数、失败收集、main 返回非零退出码。 */
public final class TestFramework {

    /** 允许抛出任意受检异常的测试体。 */
    @FunctionalInterface
    public interface ThrowingRunnable {
        void run() throws Exception;
    }

    public static final class Failure {
        final String test;
        final String message;

        Failure(String test, String message) {
            this.test = test;
            this.message = message;
        }
    }

    private final List<Failure> failures = new ArrayList<>();
    private int passed;
    private String currentTest;

    public void test(String name, ThrowingRunnable body) {
        currentTest = name;
        try {
            body.run();
            passed++;
            System.out.println("  [PASS] " + name);
        } catch (AssertionError ae) {
            failures.add(new Failure(name, ae.getMessage()));
            System.out.println("  [FAIL] " + name + " -> " + ae.getMessage());
        } catch (Throwable e) {
            failures.add(new Failure(name, e.getClass().getSimpleName() + ": " + e.getMessage()));
            System.out.println("  [ERROR] " + name + " -> " + e);
        } finally {
            currentTest = null;
        }
    }

    public void check(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public void eq(long actual, long expected, String label) {
        if (actual != expected) {
            throw new AssertionError(label + ": 期望 " + expected + "，实际 " + actual);
        }
    }

    public void eq(Object actual, Object expected, String label) {
        boolean same;
        if (actual instanceof BigDecimal a && expected instanceof BigDecimal b) {
            same = a.compareTo(b) == 0;
        } else {
            same = java.util.Objects.equals(actual, expected);
        }
        if (!same) {
            throw new AssertionError(label + ": 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    public void eqKeyStats(streamagg.core.KeyStats actual, long count, String sum, String label) {
        eq(actual.count(), count, label + " count");
        if (actual.sum().compareTo(new BigDecimal(sum)) != 0) {
            throw new AssertionError(label + " sum: 期望 " + sum + "，实际 " + actual.sum().toPlainString());
        }
    }

    public void expectThrows(Class<? extends Throwable> expected, String contains,
                             ThrowingRunnable body, String label) {
        try {
            body.run();
        } catch (Throwable e) {
            if (!expected.isInstance(e)) {
                throw new AssertionError(label + ": 期望异常 " + expected.getSimpleName()
                        + "，实际 " + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
            if (contains != null && (e.getMessage() == null || !e.getMessage().contains(contains))) {
                throw new AssertionError(label + ": 异常信息应包含 \"" + contains
                        + "\"，实际 \"" + e.getMessage() + "\"");
            }
            return;
        }
        throw new AssertionError(label + ": 期望抛出 " + expected.getSimpleName() + " 但正常返回");
    }

    /** 汇总并返回退出码（0=全部通过）。 */
    public int summary(String suiteName) {
        int total = passed + failures.size();
        System.out.println();
        System.out.println(suiteName + ": " + passed + "/" + total + " 通过");
        if (!failures.isEmpty()) {
            System.out.println("失败项:");
            for (Failure f : failures) {
                System.out.println("  - " + f.test + ": " + f.message);
            }
            return 1;
        }
        return 0;
    }

    public void withProcessor(Consumer<streamagg.core.StreamProcessor> body) {
        try (var ignored = new java.io.Closeable() {
            public void close() {
            }
        }) {
            body.accept(new streamagg.core.StreamProcessor());
        }
    }
}
