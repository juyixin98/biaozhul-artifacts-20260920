package ij;

import java.util.Objects;

/** 极简断言工具：断言失败累计计数，由 TestRunner 汇总。 */
final class Assert {

    private int failures;
    private int checks;

    void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            failures++;
            System.out.println("    FAIL: " + message);
        }
    }

    void eq(long actual, long expected, String message) {
        check(actual == expected, message + " (expected=" + expected + ", actual=" + actual + ")");
    }

    void eq(Object actual, Object expected, String message) {
        check(Objects.equals(actual, expected),
                message + " (expected=" + expected + ", actual=" + actual + ")");
    }

    void fail(String message) {
        check(false, message);
    }

    int failures() {
        return failures;
    }

    int checks() {
        return checks;
    }
}
