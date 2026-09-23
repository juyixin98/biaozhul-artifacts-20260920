package cep;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖的极简测试框架：不引入 JUnit 等任何第三方包，只用 JDK 反射运行
 * 所有 {@code test*} 开头、无参、抛 Throwable 的方法。
 *
 * 每个测试方法在执行前后调用引擎的 reset（测试类自行在方法内构造实例）。
 * 输出为行式文本，进程退出码 0 表示全部通过，非 0 表示存在失败。
 */
final class TestRunner {

    static final class Failure {
        final String name;
        final Throwable error;

        Failure(String name, Throwable error) {
            this.name = name;
            this.error = error;
        }
    }

    private TestRunner() {
    }

    static int run(Class<?>... suites) {
        int passed = 0;
        int failed = 0;
        List<Failure> failures = new ArrayList<>();

        for (Class<?> suite : suites) {
            System.out.println("== " + suite.getSimpleName() + " ==");
            for (java.lang.reflect.Method m : suite.getDeclaredMethods()) {
                if (!m.getName().startsWith("test") || m.getParameterCount() != 0) {
                    continue;
                }
                String label = suite.getSimpleName() + "." + m.getName();
                try {
                    m.setAccessible(true);
                    Object instance = suite.getDeclaredConstructor().newInstance();
                    m.invoke(instance);
                    passed++;
                    System.out.println("  PASS " + label);
                } catch (java.lang.reflect.InvocationTargetException ite) {
                    failed++;
                    failures.add(new Failure(label, ite.getCause()));
                    System.out.println("  FAIL " + label);
                } catch (Exception e) {
                    failed++;
                    failures.add(new Failure(label, e));
                    System.out.println("  FAIL " + label);
                }
            }
        }

        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("通过: " + passed + "，失败: " + failed);
        if (failed > 0) {
            System.out.println();
            for (Failure f : failures) {
                System.out.println("FAIL " + f.name);
                f.error.printStackTrace(System.out);
                System.out.println();
            }
            return 1;
        }
        return 0;
    }

    // ------------------------------------------------------------ 断言

    static void check(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    static void checkEq(long actual, long expected, String message) {
        if (actual != expected) {
            throw new AssertionError(message + " (期望 " + expected + ", 实际 " + actual + ")");
        }
    }

    static void checkEq(Object actual, Object expected, String message) {
        if (actual == null ? expected != null : !actual.equals(expected)) {
            throw new AssertionError(message + " (期望 " + expected + ", 实际 " + actual + ")");
        }
    }

    static void fail(String message) {
        throw new AssertionError(message);
    }
}
