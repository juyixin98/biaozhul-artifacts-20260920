package com.tvl.test;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖微型测试框架：assert* 累积失败，最后由 TestRunner 汇总并以退出码反映结果。
 */
public final class Assertions {

    private final List<String> failures = new ArrayList<>();
    private int checks;

    public void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            failures.add(message);
        }
    }

    public void fail(String message) {
        check(false, message);
    }

    public void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = (expected == null) ? actual == null : expected.equals(actual);
        if (!ok) {
            failures.add(message + " — 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    /** 断言抛出指定异常（或其子类），返回异常对象以便继续校验消息。 */
    public RuntimeException expectThrows(Class<? extends RuntimeException> type, Runnable r,
                                         String message) {
        checks++;
        try {
            r.run();
        } catch (RuntimeException e) {
            if (type.isInstance(e)) {
                return e;
            }
            failures.add(message + " — 期望异常 " + type.getSimpleName()
                    + "，实际抛出 " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return e;
        }
        failures.add(message + " — 期望抛出 " + type.getSimpleName() + "，但正常返回");
        return null;
    }

    public int checkCount() {
        return checks;
    }

    public List<String> failures() {
        return failures;
    }
}
