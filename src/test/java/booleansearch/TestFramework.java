package booleansearch;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖的极简测试框架：断言失败被收集，最后统一输出；
 * {@link #main(String[])} 风格的测试类只要调用静态断言方法即可。
 *
 * <p>全部通过时退出码 0，有失败时退出码 1，便于 CI/脚本判定。
 */
public final class TestFramework {

    private TestFramework() {
    }

    private static int checks = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void assertTrue(boolean condition, String message) {
        checks++;
        if (!condition) {
            failures.add(message);
            System.out.println("  [失败] " + message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        checks++;
        boolean equal = (expected == null) ? actual == null : expected.equals(actual);
        if (!equal) {
            String msg = message + " | 期望: " + expected + "，实际: " + actual;
            failures.add(msg);
            System.out.println("  [失败] " + msg);
        }
    }

    public static void section(String name) {
        System.out.println("== " + name);
    }

    /** 每个测试类运行前重置本类计数器。 */
    public static void reset() {
        checks = 0;
        failures.clear();
    }

    /** 由各测试类在结束时调用；返回是否全部通过。 */
    public static boolean finish() {
        System.out.println("   断言数: " + checks + "，失败数: " + failures.size());
        return failures.isEmpty();
    }

    /** 进程级汇总：供 RunAllTests 使用。 */
    public static int totalChecks() {
        return checks;
    }

    public static List<String> failures() {
        return List.copyOf(failures);
    }
}
