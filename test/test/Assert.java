package test;

import java.util.ArrayList;
import java.util.List;

/** 极简断言/测试框架，零第三方依赖。 */
final class Assert {

    interface Test {
        void run() throws Exception;
    }

    private final List<String> failures = new ArrayList<>();
    private int passed;

    void run(String name, Test t) {
        try {
            t.run();
            passed++;
            System.out.println("  PASS " + name);
        } catch (Throwable e) {
            failures.add(name);
            System.out.println("  FAIL " + name + " : " + e);
            Throwable c = e.getCause();
            while (c != null) {
                System.out.println("        caused by: " + c);
                c = c.getCause();
            }
        }
    }

    int finish() {
        System.out.println();
        System.out.println("passed=" + passed + " failed=" + failures.size());
        if (!failures.isEmpty()) {
            System.out.println("failed tests:");
            failures.forEach(f -> System.out.println("  - " + f));
            return 1;
        }
        return 0;
    }

    static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    static void eq(long actual, long expected, String msg) {
        if (actual != expected) {
            throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
        }
    }

    static void eq(Object actual, Object expected, String msg) {
        if (!java.util.Objects.equals(actual, expected)) {
            throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
        }
    }
}
