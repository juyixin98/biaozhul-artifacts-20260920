package com.tvl.test;

/**
 * 测试总入口：java -cp ... com.tvl.test.TestMain
 * 退出码 0 = 全部通过，1 = 有失败。
 */
public final class TestMain {

    private TestMain() {
    }

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        TernaryTruthTableTest.register(runner);
        CrossCheckTest.register(runner);
        BatchTest.register(runner);
        TypeCheckTest.register(runner);
        ParserTest.register(runner);
        JsonTest.register(runner);
        HttpServerTest.register(runner);
        int code = runner.run();
        System.out.println(code == 0 ? "ALL TESTS PASSED" : "TESTS FAILED");
        System.exit(code);
    }
}
