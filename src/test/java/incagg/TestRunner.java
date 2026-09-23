package incagg;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖迷你测试框架：收集断言失败，main 结束时以退出码反映结果。
 * 不引入 JUnit —— 本项目刻意保持“仅 JDK”。
 */
public final class TestRunner {

    public interface Test { void run() throws Exception; }

    private static int passed = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void check(String name, Test t) {
        try {
            t.run();
            passed++;
            System.out.println("  PASS  " + name);
        } catch (AssertionError | Exception e) {
            failures.add(name + " -> " + e);
            System.out.println("  FAIL  " + name + " : " + e.getMessage());
        }
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    public static void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
    }

    public static void assertEquals(String expected, String actual, String msg) {
        if (!java.util.Objects.equals(expected, actual))
            throw new AssertionError(msg + " expected=" + expected + " actual=" + actual);
    }

    public static void assertEqualsMoney(String expected, BigDecimal actual, String msg) {
        String a = actual.setScale(2, java.math.RoundingMode.HALF_UP).toPlainString();
        if (!expected.equals(a)) throw new AssertionError(msg + " expected=" + expected + " actual=" + a);
    }

    public static int finish() {
        System.out.println();
        System.out.println("通过 " + passed + " 项，失败 " + failures.size() + " 项");
        if (!failures.isEmpty()) {
            failures.forEach(f -> System.out.println("  [失败] " + f));
            return 1;
        }
        return 0;
    }
}
