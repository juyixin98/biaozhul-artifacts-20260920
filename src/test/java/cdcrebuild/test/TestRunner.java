package cdcrebuild.test;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/** 零依赖的迷你测试框架：断言 + 异常断言 + 汇总。 */
public final class TestRunner {

    public static final class Failure {
        public final String test;
        public final String message;

        Failure(String test, String message) {
            this.test = test;
            this.message = message;
        }
    }

    private final String suite;
    private final List<Failure> failures = new ArrayList<>();
    private int count = 0;
    private String current;

    public TestRunner(String suite) {
        this.suite = suite;
    }

    public void test(String name, Runnable body) {
        count++;
        current = name;
        try {
            body.run();
            System.out.println("  [PASS] " + name);
        } catch (AssertionError ae) {
            failures.add(new Failure(name, ae.getMessage()));
            System.out.println("  [FAIL] " + name + " -> " + ae.getMessage());
        } catch (Exception e) {
            failures.add(new Failure(name, "抛出未预期异常: " + e));
            System.out.println("  [ERROR] " + name + " -> " + e);
        }
    }

    public void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public void assertEquals(Object expected, Object actual, String msg) {
        boolean eq = (expected == null) ? actual == null : expected.equals(actual);
        if (!eq) {
            throw new AssertionError(msg + "（期望 " + expected + "，实际 " + actual + "）");
        }
    }

    public void assertNull(Object actual, String msg) {
        if (actual != null) {
            throw new AssertionError(msg + "（实际 " + actual + "）");
        }
    }

    /** JSON 数字一律是 Long，与 int/long 字面量比较时用这个，避免 Integer != Long。 */
    public void assertNum(long expected, Object actual, String msg) {
        if (!(actual instanceof Number) || ((Number) actual).longValue() != expected) {
            throw new AssertionError(msg + "（期望 " + expected + "，实际 " + actual + "）");
        }
    }

    /** 断言代码块抛出给定类型的异常（其子类也算）。 */
    public void assertThrows(Class<? extends Throwable> expected, Runnable body, String msg) {
        try {
            body.run();
        } catch (RuntimeException e) {
            if (expected.isInstance(e)) {
                return;
            }
            throw new AssertionError(msg + "（期望异常 " + expected.getSimpleName()
                    + "，实际 " + e.getClass().getSimpleName() + ": " + e.getMessage() + "）");
        }
        throw new AssertionError(msg + "（未抛出任何异常）");
    }

    /** 捕获指定类型异常并交给 verifier 检查消息。 */
    public <T extends Throwable> void assertThrows(Class<T> expected, Runnable body,
                                                   Consumer<T> verifier, String msg) {
        try {
            body.run();
        } catch (RuntimeException e) {
            if (expected.isInstance(e)) {
                @SuppressWarnings("unchecked")
                T t = (T) e;
                verifier.accept(t);
                return;
            }
            throw new AssertionError(msg + "（期望异常 " + expected.getSimpleName()
                    + "，实际 " + e.getClass().getSimpleName() + ": " + e.getMessage() + "）");
        }
        throw new AssertionError(msg + "（未抛出任何异常）");
    }

    public int failureCount() {
        return failures.size();
    }

    public int count() {
        return count;
    }

    public String suiteName() {
        return suite;
    }
}
