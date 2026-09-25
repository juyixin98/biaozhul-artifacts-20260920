package tests;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖微型测试框架：收集断言失败并在最后统一打印、以退出码反馈。
 * 不依赖 JUnit，使整个项目可用裸 JDK 构建与运行（见 README）。
 */
public abstract class TestCase {

    private final List<String> failures = new ArrayList<>();
    private int checks = 0;

    protected abstract void run() throws Exception;

    public List<String> failures() {
        return List.copyOf(failures);
    }

    public int checkCount() {
        return checks;
    }

    protected void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            failures.add(message);
        }
    }

    protected void fail(String message) {
        check(false, message);
    }

    protected void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = expected == null ? actual == null : expected.equals(actual);
        if (!ok) {
            failures.add(message + "  ==> expected=[" + expected + "] actual=[" + actual + "]");
        }
    }

    protected void contains(java.util.Collection<?> haystack, Object needle, String message) {
        checks++;
        if (!haystack.contains(needle)) {
            failures.add(message + "  ==> collection " + haystack + " does not contain " + needle);
        }
    }

    protected static String pair(String key, String a, String b) {
        return key + "|" + a + "|" + b;
    }
}
