package com.example.watermark.test;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * Minimal reflective test runner: discovers {@code public void} methods
 * annotated with {@link Test}, executes each in a fresh instance of the test
 * class, and reports pass/fail counts. Exit code is non-zero on any failure.
 */
public final class TestRunner {

    @java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)
    @java.lang.annotation.Target(java.lang.annotation.ElementType.METHOD)
    public @interface Test {
    }

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public void run(Class<?> testClass) {
        Object instance;
        try {
            instance = testClass.getDeclaredConstructor().newInstance();
        } catch (Exception e) {
            throw new RuntimeException("cannot instantiate " + testClass, e);
        }
        for (Method m : testClass.getDeclaredMethods()) {
            if (m.isAnnotationPresent(Test.class)) {
                runOne(instance, m);
            }
        }
    }

    private void runOne(Object instance, Method method) {
        String name = instance.getClass().getSimpleName() + "." + method.getName();
        try {
            method.setAccessible(true);
            method.invoke(instance);
            passed++;
            System.out.println("  PASS  " + name);
        } catch (java.lang.reflect.InvocationTargetException e) {
            Throwable cause = e.getCause();
            failed++;
            String detail = "  FAIL  " + name + "  -> " + cause;
            failures.add(detail);
            System.out.println(detail);
        } catch (Exception e) {
            failed++;
            String detail = "  FAIL  " + name + "  -> " + e;
            failures.add(detail);
            System.out.println(detail);
        }
    }

    public int getPassed() {
        return passed;
    }

    public int getFailed() {
        return failed;
    }

    public List<String> getFailures() {
        return failures;
    }

    public static void main(String[] args) throws Exception {
        List<Class<?>> classes = new ArrayList<>();
        for (String name : args) {
            classes.add(Class.forName(name));
        }
        TestRunner runner = new TestRunner();
        for (Class<?> c : classes) {
            System.out.println("Running " + c.getSimpleName());
            runner.run(c);
        }
        System.out.println();
        System.out.println("Tests run: " + (runner.getPassed() + runner.getFailed())
                + ", passed: " + runner.getPassed() + ", failed: " + runner.getFailed());
        if (runner.getFailed() > 0) {
            System.exit(1);
        }
    }
}
