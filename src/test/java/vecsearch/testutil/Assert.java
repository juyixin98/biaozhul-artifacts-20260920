package vecsearch.testutil;

/** 极简断言：失败抛 AssertionError，由 TestRunner 捕获。 */
public final class Assert {

    private Assert() {
    }

    public static void isTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void isTrue(boolean cond) {
        isTrue(cond, "expected true");
    }

    public static void eq(Object actual, Object expected) {
        eq(actual, expected, "");
    }

    public static void eq(Object actual, Object expected, String msg) {
        if (actual == null ? expected != null : !actual.equals(expected)) {
            throw new AssertionError(msg + " expected=<" + expected + "> actual=<" + actual + ">");
        }
    }

    public static void approx(double actual, double expected, double tol) {
        approx(actual, expected, tol, "");
    }

    public static void approx(double actual, double expected, double tol, String msg) {
        if (Math.abs(actual - expected) > tol) {
            throw new AssertionError(
                    msg + " expected≈" + expected + " actual=" + actual + " tol=" + tol);
        }
    }

    public static void throws_(Class<? extends Throwable> expected, Runnable r) {
        throws_(expected, r, "");
    }

    public static void throws_(Class<? extends Throwable> expected, Runnable r, String msg) {
        try {
            r.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) {
                return;
            }
            throw new AssertionError(
                    msg + " expected exception " + expected.getName() + " but got " + t);
        }
        throw new AssertionError(msg + " expected exception " + expected.getName() + " but none thrown");
    }

    public static void contains(String haystack, String needle) {
        if (haystack == null || !haystack.contains(needle)) {
            throw new AssertionError("expected text to contain <" + needle + "> but was <" + haystack + ">");
        }
    }

    /** 执行并断言抛出的是 ApiException（或其派生），返回以便检查 status/message。 */
    public static <X extends Throwable> X capture(ThrowingRunnable r) {
        try {
            r.run();
        } catch (Throwable t) {
            @SuppressWarnings("unchecked")
            X x = (X) t;
            return x;
        }
        throw new AssertionError("expected a throwable but none was thrown");
    }

    public interface ThrowingRunnable {
        void run();
    }
}
