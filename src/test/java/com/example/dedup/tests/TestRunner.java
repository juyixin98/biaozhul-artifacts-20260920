package com.example.dedup.tests;

/** Tiny zero-dependency test harness. */
public final class TestRunner {

    public interface Test {
        void run() throws Exception;
    }

    private int passed;
    private int failed;
    private int skipped;

    public void run(String name, Test t) {
        try {
            t.run();
            passed++;
            System.out.println("  PASS  " + name);
        } catch (AssertionError | Exception e) {
            failed++;
            System.out.println("  FAIL  " + name + "  -> " + e);
            for (StackTraceElement ste : e.getStackTrace()) {
                if (ste.getClassName().startsWith("com.example.dedup.tests")) {
                    System.out.println("          at " + ste);
                }
            }
        }
    }

    public void skip(String name, String reason) {
        skipped++;
        System.out.println("  SKIP  " + name + "  (" + reason + ")");
    }

    public int summary() {
        System.out.println();
        System.out.println("Tests: " + passed + " passed, " + failed + " failed, " + skipped + " skipped");
        return failed == 0 ? 0 : 1;
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(msg + " expected=<" + expected + "> actual=<" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " expected=<" + expected + "> actual=<" + actual + ">");
        }
    }
}
