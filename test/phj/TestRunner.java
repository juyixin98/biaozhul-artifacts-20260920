package phj;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简测试框架（零依赖）：静态断言 + 反射收集 @Test 方法。
 * 用法：java phj.TestRunner phj.JsonTest phj.JoinFuzzTest ...
 */
public final class TestRunner {

    public int testsRun;
    public int testsFailed;
    public final List<String> failures = new ArrayList<>();
    private long startMs;

    public void run(Class<?> testClass) {
        System.out.println("== " + testClass.getSimpleName() + " ==");
        Object instance;
        try {
            instance = testClass.getDeclaredConstructor().newInstance();
        } catch (Exception e) {
            throw new RuntimeException("无法实例化 " + testClass, e);
        }
        List<java.lang.reflect.Method> methods = new ArrayList<>();
        for (var m : testClass.getDeclaredMethods()) {
            if (m.isAnnotationPresent(Test.class)) methods.add(m);
        }
        methods.sort(java.util.Comparator.comparing(java.lang.reflect.Method::getName));
        for (var m : methods) {
            testsRun++;
            long t0 = System.nanoTime();
            try {
                m.setAccessible(true);
                m.invoke(instance);
                double ms = (System.nanoTime() - t0) / 1e6;
                System.out.printf("  PASS %-48s %8.2f ms%n", m.getName(), ms);
            } catch (java.lang.reflect.InvocationTargetException ite) {
                testsFailed++;
                Throwable cause = ite.getCause() != null ? ite.getCause() : ite;
                String msg = testClass.getSimpleName() + "." + m.getName()
                        + " -> " + cause.getClass().getSimpleName() + ": " + cause.getMessage();
                failures.add(msg);
                System.out.println("  FAIL " + m.getName() + " -> " + cause);
                cause.printStackTrace(System.out);
            } catch (Exception e) {
                testsFailed++;
                failures.add(testClass.getSimpleName() + "." + m.getName() + " -> " + e);
                System.out.println("  FAIL " + m.getName() + " -> " + e);
            }
        }
    }

    public int summary() {
        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("总计 " + testsRun + "，失败 " + testsFailed);
        if (testsFailed > 0) {
            System.out.println("失败用例：");
            for (String f : failures) System.out.println("  - " + f);
            return 1;
        }
        System.out.println("全部通过 ✅");
        return 0;
    }

    // --------------------------------------------------------------- 断言

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    public static void assertFalse(boolean cond) {
        assertTrue(!cond, "期望 false，实际 true");
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertTrue(boolean cond) {
        assertTrue(cond, "期望 true，实际 false");
    }

    public static void assertEquals(Object expected, Object actual) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError("期望 <" + expected + "> 实际 <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual) {
        if (expected != actual) {
            throw new AssertionError("期望 <" + expected + "> 实际 <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, Object actual) {
        if (actual instanceof Number n) assertEquals(expected, n.longValue());
        else throw new AssertionError("期望 <" + expected + "> 实际 <" + actual + ">");
    }

    public static void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + "（期望 <" + expected + "> 实际 <" + actual + ">）");
        }
    }

    public static void assertEquals(double expected, double actual, double delta) {
        if (Math.abs(expected - actual) > delta) {
            throw new AssertionError("期望 <" + expected + "> 实际 <" + actual + ">");
        }
    }

    public static void fail(String msg) {
        throw new AssertionError(msg);
    }
}
