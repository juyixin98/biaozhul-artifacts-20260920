package com.example.edcand;

/** 测试总入口：编译后执行 main，有失败时退出码 1。 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        LevenshteinTest.register(runner);
        IndexRecallTest.register(runner);
        NormalizationTest.register(runner);
        JsonTest.register(runner);
        ServerE2ETest.register(runner);
        int code = runner.run();
        System.exit(code);
    }
}
