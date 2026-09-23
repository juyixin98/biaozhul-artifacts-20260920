package dedup.tests;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖迷你测试框架：断言收集失败而不是立即抛出，
 * 每个用例结束后统一报告，便于一次看到所有回归。
 */
public final class TestFramework {

    public static final class Failure {
        final String test;
        final String message;

        Failure(String test, String message) {
            this.test = test;
            this.message = message;
        }
    }

    public interface TestCase {
        void run(Asserts a) throws Exception;
    }

    private final List<Failure> failures = new ArrayList<>();
    private int passedCount;
    private int runCount;

    public void run(String name, TestCase tc) {
        runCount++;
        Asserts asserts = new Asserts(name);
        try {
            tc.run(asserts);
        } catch (Throwable t) {
            asserts.fail("用例抛出未预期异常: " + t);
            for (StackTraceElement ste : t.getStackTrace()) {
                if (ste.getClassName().startsWith("dedup.")) {
                    asserts.fail("    at " + ste);
                    break;
                }
            }
        }
        if (asserts.failures.isEmpty()) {
            passedCount++;
            System.out.println("  PASS  " + name);
        } else {
            failures.addAll(asserts.failures);
            System.out.println("  FAIL  " + name);
            for (String f : asserts.failMessages) {
                System.out.println("        - " + f);
            }
        }
    }

    public int summary() {
        System.out.println();
        System.out.println("结果: " + passedCount + "/" + runCount + " 用例通过");
        if (!failures.isEmpty()) {
            System.out.println("失败 " + failures.size() + " 处断言");
            return 1;
        }
        System.out.println("全部通过");
        return 0;
    }

    public static void main(String[] args) {
        // 占位：实际入口在 AllTests
    }

    /** 断言收集器。 */
    public static final class Asserts {
        private final String name;
        private final List<Failure> failures = new ArrayList<>();
        private final List<String> failMessages = new ArrayList<>();

        Asserts(String name) { this.name = name; }

        public void fail(String message) {
            failures.add(new Failure(name, message));
            failMessages.add(message);
        }

        public void check(boolean cond, String message) {
            if (!cond) fail(message);
        }

        public void eq(Object actual, Object expected, String what) {
            boolean ok = (actual == null) ? expected == null : actual.equals(expected);
            if (!ok) {
                fail(what + " — 期望 <" + expected + ">，实际 <" + actual + ">");
            }
        }

        public void eqLong(long actual, long expected, String what) {
            if (actual != expected) {
                fail(what + " — 期望 <" + expected + ">，实际 <" + actual + ">");
            }
        }

        public void holds(java.util.function.BooleanSupplier cond, String message) {
            check(cond.getAsBoolean(), message);
        }

        public <T extends Throwable> void throws_(Class<T> type, Runnable r, String what) {
            try {
                r.run();
                fail(what + " — 期望抛出 " + type.getSimpleName() + " 但没有抛出");
            } catch (Throwable t) {
                if (!type.isInstance(t)) {
                    fail(what + " — 期望抛出 " + type.getSimpleName()
                            + "，实际抛出 " + t.getClass().getSimpleName() + ": " + t.getMessage());
                }
            }
        }
    }
}
