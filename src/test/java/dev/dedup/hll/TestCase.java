package dev.dedup.hll;

/** 极简断言工具：累计失败数，结束时由 AllTests 决定退出码。 */
public abstract class TestCase {

    private int checks;
    private int failures;

    public abstract String name();

    public abstract void run() throws Exception;

    protected void check(boolean cond, String message) {
        checks++;
        if (!cond) {
            failures++;
            System.out.println("    [FAIL] " + message);
        }
    }

    protected void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = (actual == null) ? expected == null : actual.equals(expected);
        if (!ok) {
            failures++;
            System.out.println("    [FAIL] " + message + " — 期望 <" + expected + ">，实际 <" + actual + ">");
        }
    }

    protected void eqLong(long actual, long expected, String message) {
        eq(actual, expected, message);
    }

    protected void eqDouble(double actual, double expected, double eps, String message) {
        checks++;
        if (Math.abs(actual - expected) > eps) {
            failures++;
            System.out.println("    [FAIL] " + message + " — 期望 " + expected + "，实际 " + actual);
        }
    }

    protected void fails(Class<? extends Throwable> expected, Runnable r, String message) {
        checks++;
        try {
            r.run();
        } catch (Throwable t) {
            if (!expected.isInstance(t)) {
                failures++;
                System.out.println("    [FAIL] " + message + " — 期望异常 " + expected.getSimpleName()
                        + "，实际 " + t.getClass().getSimpleName() + ": " + t.getMessage());
            }
            return;
        }
        failures++;
        System.out.println("    [FAIL] " + message + " — 期望抛出 " + expected.getSimpleName() + "，但未抛出");
    }

    public int checkCount() {
        return checks;
    }

    public int failureCount() {
        return failures;
    }
}
