package com.example.uninorm;

import java.util.ArrayList;
import java.util.List;

/**
 * Dependency-free test runner. Every public static no-arg void method on the
 * listed classes is one test. Exit code is 0 only if all tests passed.
 */
public final class TestRunner {

    private record TestCase(String name, ThrowingRunnable body) {
    }

    @FunctionalInterface
    interface ThrowingRunnable {
        void run() throws Throwable;
    }

    public static void main(String[] args) {
        List<TestCase> tests = new ArrayList<>();
        add(tests, NormalizationTest.class);
        add(tests, OffsetMappingTest.class);
        add(tests, SearchEngineTest.class);
        add(tests, FuzzBoundaryTest.class);
        add(tests, HttpServiceTest.class);

        int passed = 0;
        int failed = 0;
        long start = System.nanoTime();
        for (TestCase t : tests) {
            try {
                t.body().run();
                System.out.println("PASS  " + t.name());
                passed++;
            } catch (Throwable ex) {
                Throwable cause = ex instanceof java.lang.reflect
                        .InvocationTargetException ite ? ite.getCause() : ex;
                System.out.println("FAIL  " + t.name() + " -> "
                        + cause.getClass().getSimpleName() + ": "
                        + cause.getMessage());
                failed++;
            }
        }
        long ms = (System.nanoTime() - start) / 1_000_000;
        System.out.println("---------------------------------------------");
        System.out.println("tests run: " + tests.size()
                + ", passed: " + passed + ", failed: " + failed
                + " (" + ms + " ms)");
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static void add(List<TestCase> tests, Class<?> cls) {
        for (var m : cls.getDeclaredMethods()) {
            if (java.lang.reflect.Modifier.isStatic(m.getModifiers())
                    && m.getParameterCount() == 0
                    && m.getReturnType() == void.class) {
                tests.add(new TestCase(cls.getSimpleName() + "." + m.getName(),
                        () -> m.invoke(null)));
            }
        }
    }
}
