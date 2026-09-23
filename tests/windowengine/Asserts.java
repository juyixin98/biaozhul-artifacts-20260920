package windowengine;

/**
 * 极简测试断言：失败抛 AssertionError，由 TestRunner 捕获计数。
 * 故意不引入 JUnit，保持零第三方依赖、纯 javac/java 可跑。
 */
public final class Asserts {

    private Asserts() {
    }

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertFalse(boolean cond, String message) {
        if (cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " —— 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " —— 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }
}
