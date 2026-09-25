package drvb.tests;

/** 极简断言库：失败抛 {@link AssertionError}，信息包含预期与实际。 */
public final class Asserts {

    private Asserts() {
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        if (cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        boolean eq = (expected == null) ? actual == null : expected.equals(actual);
        if (!eq) {
            throw new AssertionError(msg + " —— 预期: <" + expected + "> 实际: <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " —— 预期: <" + expected + "> 实际: <" + actual + ">");
        }
    }

    public static void assertEquals(double expected, double actual, double tol, String msg) {
        if (Math.abs(expected - actual) > tol) {
            throw new AssertionError(msg + " —— 预期: <" + expected + "> 实际: <" + actual + ">");
        }
    }

    /** 断言给定代码块抛出指定类型异常，并返回该异常以便继续断言错误码。 */
    public static Throwable assertThrows(Class<? extends Throwable> expected, ThrowingRunnable body,
                                         String msg) {
        try {
            body.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) {
                return t;
            }
            throw new AssertionError(msg + " —— 预期异常 " + expected.getSimpleName()
                    + "，实际抛出 " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError(msg + " —— 预期抛出 " + expected.getSimpleName() + "，但正常返回");
    }

    @FunctionalInterface
    public interface ThrowingRunnable {
        void run() throws Throwable;
    }
}
