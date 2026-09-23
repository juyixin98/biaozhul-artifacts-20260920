package com.opp16.engine.tests;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency assertion harness.
 * Collects failures and exits non-zero if anything failed.
 */
public final class Assert {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();
    private static String suite = "";

    private Assert() {}

    public static void suite(String name) { suite = name; }

    public static void check(String name, boolean cond) {
        if (cond) {
            passed++;
            System.out.println("  PASS [" + suite + "] " + name);
        } else {
            failed++;
            String msg = "FAIL [" + suite + "] " + name;
            failures.add(msg);
            System.out.println("  " + msg);
        }
    }

    public static void eq(String name, Object actual, Object expected) {
        boolean ok = (actual == null) ? expected == null : actual.equals(expected);
        if (!ok) {
            check(name + " (actual=" + actual + ", expected=" + expected + ")", false);
        } else {
            check(name, true);
        }
    }

    /** Assert that running r throws EngineException whose message contains frag. */
    public static void throwsEngineEx(String name, String expectedFragment, Runnable r) {
        throwsWith(name, RuntimeException.class, expectedFragment, r);
    }

    /** Assert that running r throws the given runtime exception type with a message fragment. */
    public static void throwsWith(String name, Class<? extends RuntimeException> type,
                                  String expectedFragment, Runnable r) {
        try {
            r.run();
        } catch (RuntimeException e) {
            boolean typeOk = type.isInstance(e);
            boolean msgOk = expectedFragment == null
                    || (e.getMessage() != null && e.getMessage().contains(expectedFragment));
            check(name + " (got " + e.getClass().getSimpleName() + ": " + e.getMessage() + ")",
                    typeOk && msgOk);
            return;
        }
        check(name + " -> no exception thrown", false);
    }

    public static int summary() {
        System.out.println();
        System.out.println("------------------------------------------------------------");
        System.out.println("TOTAL: " + (passed + failed) + ", PASS: " + passed + ", FAIL: " + failed);
        if (failed > 0) {
            System.out.println("Failures:");
            for (String f : failures) System.out.println("  " + f);
        }
        System.out.println("------------------------------------------------------------");
        return failed == 0 ? 0 : 1;
    }
}
