package streamagg.test;

import java.math.BigDecimal;
import java.util.List;
import java.util.Objects;

/** Tiny assertion helpers (no JUnit dependency). */
public final class Assert {

    private Assert() {
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new TestFailure(msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        if (cond) {
            throw new TestFailure(msg);
        }
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        if (!valueEquals(expected, actual)) {
            throw new TestFailure(msg + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    /** Numeric-aware equality so 3 (Integer), 3L (Long) and 3 (BigDecimal) compare equal. */
    static boolean valueEquals(Object a, Object b) {
        if (Objects.equals(a, b)) {
            return true;
        }
        if (a instanceof Number && b instanceof Number) {
            try {
                return new BigDecimal(a.toString()).compareTo(new BigDecimal(b.toString())) == 0;
            } catch (NumberFormatException e) {
                return false;
            }
        }
        return false;
    }

    public static void assertEq(BigDecimal expected, BigDecimal actual, String msg) {
        if (expected == null || actual == null || expected.compareTo(actual) != 0) {
            throw new TestFailure(msg + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertNull(Object o, String msg) {
        if (o != null) {
            throw new TestFailure(msg + " — expected null but was <" + o + ">");
        }
    }

    public static void assertNotNull(Object o, String msg) {
        if (o == null) {
            throw new TestFailure(msg + " — expected non-null");
        }
    }

    public static void assertContains(String haystack, String needle, String msg) {
        if (haystack == null || !haystack.contains(needle)) {
            throw new TestFailure(msg + " — <" + haystack + "> does not contain <" + needle + ">");
        }
    }

    public static void assertListEquals(List<?> expected, List<?> actual, String msg) {
        if (expected.size() != actual.size()) {
            throw new TestFailure(msg + " — list lengths differ: expected "
                    + expected + " but was " + actual);
        }
        for (int i = 0; i < expected.size(); i++) {
            if (!valueEquals(expected.get(i), actual.get(i))) {
                throw new TestFailure(msg + " — at index " + i + " expected <"
                        + expected.get(i) + "> but was <" + actual.get(i) + ">");
            }
        }
    }
}
