package com.example.sessionwindow.tests;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/** Tiny annotation-based test runner (no JUnit dependency). */
public final class TestRunner {

    private TestRunner() {
    }

    public record Result(String suite, String test, boolean passed, String failure) {
    }

    public static int run(Class<?>... suites) {
        List<Result> results = new ArrayList<>();
        for (Class<?> suite : suites) {
            runSuite(suite, results);
        }
        int failures = 0;
        for (Result r : results) {
            if (r.passed()) {
                System.out.println("PASS " + r.suite() + " :: " + r.test());
            } else {
                failures++;
                System.out.println("FAIL " + r.suite() + " :: " + r.test());
                System.out.println("     " + r.failure());
            }
        }
        System.out.println();
        System.out.println("tests: " + results.size() + ", passed: "
                + (results.size() - failures) + ", failed: " + failures);
        return failures == 0 ? 0 : 1;
    }

    private static void runSuite(Class<?> suite, List<Result> results) {
        Object instance;
        try {
            instance = suite.getDeclaredConstructor().newInstance();
        } catch (Exception e) {
            throw new RuntimeException("cannot instantiate " + suite.getName(), e);
        }
        for (Method m : suite.getDeclaredMethods()) {
            if (m.isAnnotationPresent(Test.class)) {
                m.setAccessible(true);
                String name = m.getName();
                try {
                    m.invoke(instance);
                    results.add(new Result(suite.getSimpleName(), name, true, null));
                } catch (InvocationTargetException e) {
                    Throwable cause = e.getCause() == null ? e : e.getCause();
                    results.add(new Result(suite.getSimpleName(), name, false,
                            cause.getClass().getSimpleName() + ": " + cause.getMessage()));
                } catch (Exception e) {
                    results.add(new Result(suite.getSimpleName(), name, false, e.toString()));
                }
            }
        }
    }
}
