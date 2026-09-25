package com.example.cptx.tests;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖迷你测试框架：断言失败收集为 AssertionError，最后统一汇报并以退出码反映结果。
 */
public final class TestRunner {

    private final String suite;
    private int checks;
    private final List<String> failures = new ArrayList<>();

    public TestRunner(String suite) {
        this.suite = suite;
    }

    public void check(boolean cond, String message) {
        checks++;
        if (!cond) failures.add(message);
    }

    public void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = (actual == null) ? expected == null : actual.equals(expected);
        if (!ok) {
            failures.add(message + " —— 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    public void approxEq(double actual, double expected, double eps, String message) {
        checks++;
        if (Math.abs(actual - expected) > eps) {
            failures.add(message + " —— 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    public int checks() {
        return checks;
    }

    public List<String> failures() {
        return failures;
    }

    public void section(String name) {
        System.out.println("  · " + name);
    }

    public void finish() {
        if (failures.isEmpty()) {
            System.out.println(suite + ": 通过 (" + checks + " 项断言)");
        } else {
            System.out.println(suite + ": 失败 " + failures.size() + " / " + checks + " 项断言");
            for (String f : failures) System.out.println("    ✗ " + f);
        }
    }
}
