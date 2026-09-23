package vecq.test;

import vecq.Json;

/** 极简断言框架（无 JUnit 依赖）。 */
public final class Assert {

    private Assert() {}

    public static int passed = 0;
    public static int failed = 0;

    public static void that(boolean ok, String name) {
        if (ok) { passed++; }
        else { failed++; System.out.println("  [FAIL] " + name); }
    }

    public static void eq(Object expected, Object actual, String name) {
        // 通过 JSON 文本比较，消除 Integer/Long 等装箱差异
        String e = Json.write(expected);
        String a = Json.write(actual);
        that(e.equals(a), name + "（期望 " + e + "，实际 " + a + "）");
    }

    public static void eqInt(int expected, int actual, String name) {
        that(expected == actual, name + "（期望 " + expected + "，实际 " + actual + "）");
    }

    public static void eqDouble(double expected, double actual, String name) {
        that(Double.compare(expected, actual) == 0,
                name + "（期望 " + expected + "，实际 " + actual + "）");
    }

    public static void fails(Runnable r, Class<? extends Throwable> ex, String name) {
        try {
            r.run();
        } catch (RuntimeException e) {
            that(ex.isInstance(e), name + "（期望异常 " + ex.getSimpleName()
                    + "，实际 " + e.getClass().getSimpleName() + ": " + e.getMessage() + "）");
            return;
        }
        that(false, name + "（期望抛出 " + ex.getSimpleName() + "，但正常返回）");
    }
}
