package tvl;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * 极简单元测试框架（零依赖）：收集用例、执行、统计并输出失败详情。
 * 进程退出码非 0 表示存在失败，可直接用于 CI / 构建脚本。
 *
 * 计数约定：每条成功的断言（assertTrue/assertEquals/assertThrows 等）
 * 计一次通过；断言失败计一次失败并记录详情、继续执行后续断言。
 */
public final class TF {

    @FunctionalInterface
    public interface Case {
        void run() throws Throwable;
    }

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    /** 以命名用例方式执行（失败时附带用例名）。 */
    public static void test(String name, Case c) {
        try {
            c.run();
        } catch (Throwable t) {
            recordFailure(name, t);
        }
    }

    private static void recordFailure(String name, Throwable t) {
        failed++;
        failures.add("[FAIL] " + name + " -> " + t);
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
        passed++;
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertEquals(Object expected, Object actual) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError("期望 <" + expected + ">，实际 <" + actual + ">");
        }
        passed++;
    }

    public static void assertEquals(long expected, long actual) {
        if (expected != actual) {
            throw new AssertionError("期望 <" + expected + ">，实际 <" + actual + ">");
        }
        passed++;
    }

    public static void assertNull(Object v, String msg) {
        if (v != null) {
            throw new AssertionError(msg + "（实际非空：" + v + "）");
        }
        passed++;
    }

    public static void assertNotNull(Object v, String msg) {
        if (v == null) {
            throw new AssertionError(msg);
        }
        passed++;
    }

    public static void fail(String msg) {
        throw new AssertionError(msg);
    }

    @SuppressWarnings("unchecked")
    public static <T extends Throwable> T assertThrows(Class<T> type, Case c) {
        try {
            c.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) {
                passed++;
                return (T) t;
            }
            throw new AssertionError("期望抛出 " + type.getSimpleName()
                    + "，实际抛出 " + t.getClass().getSimpleName() + ": " + t);
        }
        throw new AssertionError("期望抛出 " + type.getSimpleName() + "，但正常返回");
    }

    public static int passed() {
        return passed;
    }

    public static int failed() {
        return failed;
    }

    public static List<String> failures() {
        return failures;
    }

    public static void reset() {
        passed = 0;
        failed = 0;
        failures.clear();
    }

    /** 供套件在断言意外抛出时统一记账。 */
    public static void countSuiteFailure(String name, Throwable t) {
        recordFailure(name, t);
    }

    public static int finish() {
        System.out.println();
        for (String f : failures) {
            System.out.println(f);
        }
        System.out.println("-----------------------------------");
        System.out.println("测试通过：" + passed + "，失败：" + failed);
        return failed == 0 ? 0 : 1;
    }
}
