package com.example.ac.test;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/** 零依赖迷你测试框架：assert 收集错误，main 退出码反映成败。 */
public abstract class TestCase {

    public final String name;
    private final List<Throwable> failures = new ArrayList<>();
    private int checks;

    protected TestCase(String name) {
        this.name = name;
    }

    protected abstract void run() throws Exception;

    protected void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            failures.add(new AssertionError(message));
        }
    }

    protected void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = actual == null ? expected == null : actual.equals(expected);
        if (!ok) {
            failures.add(new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">"));
        }
    }

    protected void fail(String message) {
        failures.add(new AssertionError(message));
    }

    int failureCount() {
        return failures.size();
    }

    int checks() {
        return checks;
    }

    int checkCount() {
        return checks;
    }

    List<Throwable> failures() {
        return failures;
    }

    /** 运行一组测试，返回全部失败数；并打印进度。 */
    public static int runAll(List<TestCase> tests, Consumer<String> out) {
        int totalFailures = 0;
        int totalChecks = 0;
        for (TestCase t : tests) {
            try {
                t.run();
            } catch (Throwable th) {
                t.failures.add(th);
            }
            totalChecks += t.checks();
            if (t.failureCount() == 0) {
                out.accept("PASS " + t.name + " (" + t.checks() + " checks)");
            } else {
                totalFailures += t.failureCount();
                out.accept("FAIL " + t.name + " (" + t.failureCount() + " failures / " + t.checks() + " checks)");
                for (Throwable f : t.failures()) {
                    out.accept("       - " + f);
                }
            }
        }
        out.accept("----");
        out.accept(tests.size() + " test cases, " + totalChecks + " assertions, "
                + (tests.size() - countFailedCases(tests)) + " passed cases, "
                + totalFailures + " failures");
        return totalFailures;
    }

    private static long countFailedCases(List<TestCase> tests) {
        return tests.stream().filter(t -> t.failureCount() > 0).count();
    }
}
