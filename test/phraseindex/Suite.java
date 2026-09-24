package phraseindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Tiny zero-dependency test harness used by every test class in this project. */
public final class Suite {

    @FunctionalInterface
    public interface Test {
        void run() throws Exception;
    }

    private final String name;
    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public Suite(String name) {
        this.name = name;
    }

    public void test(String label, Test test) {
        try {
            test.run();
            passed++;
        } catch (Throwable t) {
            failed++;
            failures.add(label + " -> " + t);
        }
    }

    /** Returns true when every test in this suite passed. */
    public boolean report() {
        System.out.printf("[%s] passed=%d failed=%d%n", name, passed, failed);
        for (String f : failures) {
            System.out.println("  FAIL: " + f);
        }
        return failed == 0;
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    public static void assertFalse(boolean condition, String message) {
        assertTrue(!condition, message);
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(message + " | expected=" + expected + " actual=" + actual);
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }
}
