package topk;

import java.util.Objects;

/** 极简断言工具。 */
public final class Assert {
    private Assert() {}

    public static void eq(Object expected, Object actual, String what) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(what + "\n  expected: " + expected + "\n  actual:   " + actual);
        }
    }

    public static void eq(boolean expected, boolean actual, String what) {
        eq(Boolean.valueOf(expected), Boolean.valueOf(actual), what);
    }

    public static void eq(long expected, long actual, String what) {
        eq(Long.valueOf(expected), Long.valueOf(actual), what);
    }

    public static void eq(int expected, int actual, String what) {
        eq(Integer.valueOf(expected), Integer.valueOf(actual), what);
    }

    public static void contains(String haystack, String needle, String what) {
        if (haystack == null || !haystack.contains(needle)) {
            throw new AssertionError(what + "\n  expected to contain: " + needle + "\n  actual: " + haystack);
        }
    }
}
