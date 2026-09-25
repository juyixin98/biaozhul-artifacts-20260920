package sessions.testing;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖测试运行器：扫描给定类中全部 {@link Test} 注解的静态方法并执行。
 *
 * <p>用法：{@code java sessions.testing.TestRunner fully.qualified.TestClass ...}
 */
public final class TestRunner {

    public record CaseResult(String name, boolean passed, long elapsedMs, String error) {
    }

    private TestRunner() {
    }

    public static void main(String[] args) throws Exception {
        List<CaseResult> results = new ArrayList<>();
        for (String className : args) {
            Class<?> clazz = Class.forName(className);
            for (Method m : clazz.getDeclaredMethods()) {
                Test annotation = m.getAnnotation(Test.class);
                if (annotation == null) {
                    continue;
                }
                String name = clazz.getSimpleName() + "."
                        + (annotation.value().isEmpty() ? m.getName() : annotation.value());
                long start = System.nanoTime();
                try {
                    m.setAccessible(true);
                    m.invoke(null);
                    results.add(new CaseResult(name, true,
                            (System.nanoTime() - start) / 1_000_000, null));
                } catch (java.lang.reflect.InvocationTargetException ite) {
                    Throwable cause = ite.getCause();
                    results.add(new CaseResult(name, false,
                            (System.nanoTime() - start) / 1_000_000,
                            cause.getClass().getSimpleName() + ": " + cause.getMessage()));
                }
            }
        }

        int passed = 0;
        int failed = 0;
        for (CaseResult r : results) {
            if (r.passed()) {
                passed++;
                System.out.printf("PASS  %-60s (%d ms)%n", r.name(), r.elapsedMs());
            } else {
                failed++;
                System.out.printf("FAIL  %s%n      %s%n", r.name(), r.error());
            }
        }
        System.out.println("-".repeat(78));
        System.out.printf("tests: %d, passed: %d, failed: %d%n",
                results.size(), passed, failed);
        if (failed > 0) {
            System.exit(1);
        }
    }
}
