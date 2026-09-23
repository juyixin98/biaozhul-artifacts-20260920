package dedup.test;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test runner. Discovers @Test methods reflectively,
 * runs each in a fresh instance of the test class, and reports a TAP-ish
 * summary. Exits non-zero on any failure (used by test.sh / CI).
 */
public final class TestRunner {

    public record Result(String suite, String test, boolean passed, long millis, String error) {
    }

    public static void main(String[] args) throws Exception {
        List<Class<?>> suites = new ArrayList<>();
        for (String name : args) {
            suites.add(Class.forName(name));
        }

        int passed = 0;
        int failed = 0;
        List<Result> failures = new ArrayList<>();

        for (Class<?> suite : suites) {
            System.out.println("== " + suite.getSimpleName() + " ==");
            for (Method m : suite.getDeclaredMethods()) {
                if (!m.isAnnotationPresent(Test.class)) {
                    continue;
                }
                m.setAccessible(true);
                long start = System.nanoTime();
                boolean ok = false;
                String error = null;
                try {
                    Object instance = suite.getDeclaredConstructor().newInstance();
                    m.invoke(instance);
                    ok = true;
                    passed++;
                } catch (java.lang.reflect.InvocationTargetException e) {
                    Throwable cause = e.getCause();
                    error = cause == null ? "?" : (cause.getClass().getSimpleName()
                            + ": " + cause.getMessage());
                    failed++;
                    failures.add(new Result(suite.getSimpleName(), m.getName(), false, 0, error));
                } catch (Throwable t) {
                    error = t.getClass().getSimpleName() + ": " + t.getMessage();
                    failed++;
                    failures.add(new Result(suite.getSimpleName(), m.getName(), false, 0, error));
                }
                long millis = (System.nanoTime() - start) / 1_000_000;
                System.out.printf("  [%s] %s (%d ms)%n", ok ? "PASS" : "FAIL", m.getName(), millis);
            }
        }

        System.out.println();
        if (!failures.isEmpty()) {
            System.out.println("Failures:");
            for (Result r : failures) {
                System.out.println("  " + r.suite() + "." + r.test() + " -> " + r.error());
            }
        }
        System.out.printf("Tests: %d passed, %d failed, %d total%n",
                passed, failed, passed + failed);
        if (failed > 0) {
            System.exit(1);
        }
    }
}
