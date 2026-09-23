package tvl.test;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖的极简测试框架：收集通过/失败计数与失败信息，main 统一输出。
 */
public abstract class TestBase {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public abstract String name();

    public abstract void run() throws Exception;

    protected void check(boolean condition, String message) {
        if (condition) {
            passed++;
        } else {
            failed++;
            failures.add(message);
        }
    }

    protected void fail(String message) {
        check(false, message);
    }

    protected <T> void expectEq(T expected, T actual, String message) {
        if (expected == null ? actual == null : expected.equals(actual)) {
            passed++;
        } else {
            failed++;
            failures.add(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public int passed() {
        return passed;
    }

    public int failed() {
        return failed;
    }

    public List<String> failureMessages() {
        return failures;
    }
}
