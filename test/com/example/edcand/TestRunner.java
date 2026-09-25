package com.example.edcand;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/**
 * 零依赖的最小测试运行器。
 *
 * <p>断言失败抛 {@link AssertionError}；每个测试方法独立捕获异常。
 * main 在有任何失败时以退出码 1 结束（供 build.sh / CI 使用）。
 */
public final class TestRunner {

    private record Case(String name, Consumer<TestApi> body) {
    }

    private final List<Case> cases = new ArrayList<>();
    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public TestRunner add(String name, Consumer<TestApi> body) {
        cases.add(new Case(name, body));
        return this;
    }

    public int run() {
        long t0 = System.nanoTime();
        for (Case c : cases) {
            try {
                c.body().accept(new TestApi());
                passed++;
                System.out.println("PASS  " + c.name());
            } catch (Throwable t) {
                failed++;
                failures.add(c.name());
                System.out.println("FAIL  " + c.name() + "  -> " + t);
            }
        }
        double ms = (System.nanoTime() - t0) / 1_000_000.0;
        System.out.printf("%n%d tests, %d passed, %d failed (%.1f ms)%n",
                cases.size(), passed, failed, ms);
        if (failed > 0) {
            System.out.println("failed: " + failures);
            return 1;
        }
        return 0;
    }

    /** 断言 API：语义与常见框架一致，失败即 AssertionError。 */
    public static final class TestApi {
        public void check(boolean cond, String msg) {
            if (!cond) {
                throw new AssertionError(msg);
            }
        }

        public void eq(int actual, int expected, String msg) {
            if (actual != expected) {
                throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
            }
        }

        public void eq(Object actual, Object expected, String msg) {
            if (!java.util.Objects.equals(actual, expected)) {
                throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
            }
        }

        public void fail(String msg) {
            throw new AssertionError(msg);
        }
    }
}
