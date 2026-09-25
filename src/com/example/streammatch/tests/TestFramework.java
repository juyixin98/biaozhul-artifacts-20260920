package com.example.streammatch.tests;

import java.util.ArrayList;
import java.util.List;
import java.util.function.BooleanSupplier;

/**
 * 零依赖迷你测试框架：assert 计数、失败信息收集、进程退出码反映结果。
 */
public final class TestFramework {

    static int passed;
    static int failed;
    static final List<String> failures = new ArrayList<>();

    private TestFramework() {
    }

    public static void assertTrue(boolean cond, String msg) {
        if (cond) {
            passed++;
        } else {
            failed++;
            failures.add(msg);
            System.out.println("  [FAIL] " + msg);
        }
    }

    public static void assertTrue(String msg, BooleanSupplier cond) {
        assertTrue(cond.getAsBoolean(), msg);
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        assertTrue(java.util.Objects.equals(expected, actual),
                msg + " —— 期望 <" + expected + ">，实际 <" + actual + ">");
    }

    public static void section(String name, Runnable r) {
        System.out.println("== " + name);
        r.run();
    }

    /** 所有测试类的统一入口。 */
    public static void runAll() {
        long t0 = System.nanoTime();
        AhoCorasickTest.run();
        StreamMatcherTest.run();
        UnicodeTest.run();
        EmptyPatternTest.run();
        NaiveEquivalenceTest.run();
        SharedPrefixTest.run();
        CorpusTest.run();
        JsonTest.run();
        ServiceTest.run();
        PerfTest.run();
        long ms = (System.nanoTime() - t0) / 1_000_000;

        System.out.println();
        System.out.println("================================================");
        System.out.println("测试结果: " + passed + " 通过, " + failed + " 失败, 耗时 " + ms + " ms");
        if (failed > 0) {
            System.out.println("失败明细:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            System.exit(1);
        }
    }
}
