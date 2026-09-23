package com.example.window;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny JDK-only test runner. Each TestX.run() method prints "ok/FAILED" lines
 * via {@link #check(String, boolean)} / {@link #eq(String, Object, Object)}.
 * Exit code is non-zero when any check failed.
 */
public final class TestMain {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    private TestMain() {
    }

    public static void main(String[] args) throws Exception {
        JsonTest.run();
        WindowEngineTest.run();
        HttpApiTest.run();

        System.out.println();
        System.out.println("TOTAL passed: " + passed + ", failed: " + failures.size());
        for (String f : failures) {
            System.out.println("  FAILED - " + f);
        }
        System.exit(failures.isEmpty() ? 0 : 1);
    }

    static void check(String name, boolean condition) {
        if (condition) {
            passed++;
            System.out.println("  ok     - " + name);
        } else {
            failures.add(name);
            System.out.println("  FAILED - " + name);
        }
    }

    static void eq(String name, Object expected, Object actual) {
        boolean ok = expected == null ? actual == null : expected.equals(actual);
        if (!ok) {
            name = name + "  [expected=" + expected + ", actual=" + actual + "]";
        }
        check(name, ok);
    }

    static void expectThrows(String name, Runnable r) {
        try {
            r.run();
        } catch (RuntimeException e) {
            check(name, true);
            return;
        }
        check(name + "  [expected exception]", false);
    }
}
