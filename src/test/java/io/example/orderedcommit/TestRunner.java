package io.example.orderedcommit;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test runner: instantiates every class named *Test in
 * this package and runs each @Test method, reporting a TAP-style summary.
 *
 * <p>Each test class gets a fresh {@link ServiceFixture} in its constructor
 * and is responsible for closing it.
 */
public final class TestRunner {

    private static final String[] TEST_CLASSES = {
        "io.example.orderedcommit.OrderingCoreTest",
        "io.example.orderedcommit.HttpApiTest",
    };

    private TestRunner() {
    }

    public static void main(String[] args) throws Exception {
        int passed = 0;
        int failed = 0;
        List<String> failures = new ArrayList<>();

        for (String className : TEST_CLASSES) {
            Class<?> clazz = Class.forName(className);
            for (Method method : clazz.getDeclaredMethods()) {
                if (method.getAnnotation(Test.class) == null) {
                    continue;
                }
                String name = clazz.getSimpleName() + "." + method.getName();
                Object instance = clazz.getDeclaredConstructor().newInstance();
                long start = System.currentTimeMillis();
                try {
                    method.setAccessible(true);
                    method.invoke(instance);
                    passed++;
                    System.out.printf("ok   - %s (%dms)%n", name, System.currentTimeMillis() - start);
                } catch (Throwable t) {
                    failed++;
                    Throwable cause = t.getCause() != null ? t.getCause() : t;
                    failures.add(name);
                    System.out.printf("FAIL - %s (%dms)%n", name, System.currentTimeMillis() - start);
                    cause.printStackTrace(System.out);
                } finally {
                    if (instance instanceof AutoCloseable closeable) {
                        closeable.close();
                    }
                }
            }
        }

        System.out.printf("%n%d passed, %d failed%n", passed, failed);
        if (failed > 0) {
            System.out.println("failures: " + failures);
            System.exit(1);
        }
    }
}
