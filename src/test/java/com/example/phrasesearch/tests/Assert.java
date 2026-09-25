package com.example.phrasesearch.tests;

import java.util.ArrayList;
import java.util.List;
import java.util.function.BooleanSupplier;

/**
 * 零依赖迷你测试框架（配合 javac/java 直接运行，无需 JUnit）。
 *
 * 用法：
 *   Assert.check("名称", () -> 实际.equals(期望));
 *   Assert.equals(期望, 实际);
 * 全部用例跑完后调用 Assert.finish()，有失败则 System.exit(1)。
 */
public final class Assert {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    private Assert() {
    }

    public static void check(String name, BooleanSupplier cond) {
        boolean ok;
        try {
            ok = cond.getAsBoolean();
        } catch (Throwable t) {
            ok = false;
            failures.add(name + " -> threw " + t);
            System.out.println("FAIL  " + name + " (threw " + t + ")");
            failed++;
            return;
        }
        if (ok) {
            passed++;
            System.out.println("PASS  " + name);
        } else {
            failed++;
            failures.add(name);
            System.out.println("FAIL  " + name);
        }
    }

    /** 数值相等性：JSON 解析出的整数是 Long，字面量是 Integer，按数值比较而非类型比较。 */
    private static boolean deepEquals(Object expected, Object actual) {
        if (expected instanceof Number en && actual instanceof Number an) {
            // 本项目只涉及整数与简单小数；统一转 double 比较并额外校验整数情形
            if (en instanceof Double || an instanceof Double
                    || en instanceof Float || an instanceof Float) {
                return Double.compare(en.doubleValue(), an.doubleValue()) == 0;
            }
            return en.longValue() == an.longValue();
        }
        if (expected instanceof List<?> el && actual instanceof List<?> al) {
            if (el.size() != al.size()) {
                return false;
            }
            for (int i = 0; i < el.size(); i++) {
                if (!deepEquals(el.get(i), al.get(i))) {
                    return false;
                }
            }
            return true;
        }
        return java.util.Objects.equals(expected, actual);
    }

    public static void equals(Object expected, Object actual) {
        check("assertEquals expected=" + expected + " actual=" + actual,
                () -> deepEquals(expected, actual));
    }

    public static void equals(String name, Object expected, Object actual) {
        check(name, () -> deepEquals(expected, actual));
    }

    public static void isTrue(String name, boolean condition) {
        check(name, () -> condition);
    }

    public static int passed() {
        return passed;
    }

    public static int failed() {
        return failed;
    }

    public static void reset() {
        passed = 0;
        failed = 0;
        failures.clear();
    }

    public static void finish() {
        System.out.println();
        System.out.println("============================================");
        System.out.println("tests passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            System.out.println("failed cases:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            System.out.println("============================================");
            System.exit(1);
        }
        System.out.println("ALL TESTS PASSED");
        System.out.println("============================================");
    }
}
