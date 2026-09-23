package seqcep;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test harness. Tests register with {@link #test(String, ThrowingRunnable)}
 * and are executed by {@link #runAll()}; the process exits non-zero if anything failed.
 */
public final class TestRunner {

    @FunctionalInterface
    public interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static final List<String> names = new ArrayList<>();
    private static final List<ThrowingRunnable> bodies = new ArrayList<>();

    public static void test(String name, ThrowingRunnable body) {
        names.add(name);
        bodies.add(body);
    }

    public static int runAll() {
        int failed = 0;
        for (int i = 0; i < names.size(); i++) {
            String name = names.get(i);
            long start = System.nanoTime();
            try {
                bodies.get(i).run();
                long ms = (System.nanoTime() - start) / 1_000_000;
                System.out.println("PASS  " + name + "  (" + ms + " ms)");
            } catch (Throwable t) {
                failed++;
                System.out.println("FAIL  " + name);
                System.out.println("      " + t);
                for (StackTraceElement el : t.getStackTrace()) {
                    if (el.getClassName().startsWith("seqcep")) {
                        System.out.println("        at " + el);
                        break;
                    }
                }
            }
        }
        System.out.println("----------------------------------------");
        System.out.println((names.size() - failed) + "/" + names.size() + " tests passed");
        return failed == 0 ? 0 : 1;
    }

    // ------------------------------------------------------------ asserts

    public static void assertTrue(boolean cond, String message) {
        if (!cond) throw new AssertionError("assertTrue failed: " + message);
    }

    public static void assertFalse(boolean cond, String message) {
        if (cond) throw new AssertionError("assertFalse failed: " + message);
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertThrows(Class<? extends Throwable> type, ThrowingRunnable r, String message) {
        try {
            r.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) return;
            throw new AssertionError(message + " — expected " + type.getSimpleName()
                    + " but got " + t, t);
        }
        throw new AssertionError(message + " — expected " + type.getSimpleName() + " but nothing was thrown");
    }

    private TestRunner() {}
}
