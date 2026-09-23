package tvl;

/**
 * 测试套件总入口：运行全部测试类，任一失败则以非 0 退出码结束。
 */
public final class TestRunner {

    public static void main(String[] args) {
        long start = System.currentTimeMillis();

        runSuite("穷举三值真值表", TruthTableTest::run);
        runSuite("NULL 参与比较", NullComparisonTest::run);
        runSuite("运算符优先级", PrecedenceTest::run);
        runSuite("短路求值", ShortCircuitTest::run);
        runSuite("整数溢出与除零", OverflowTest::run);
        runSuite("错误位置", ErrorPositionTest::run);
        runSuite("类型检查", TypeCheckTest::run);
        runSuite("JSON 解析器", JsonParserTest::run);
        runSuite("JSON 请求端到端", JsonRequestTest::run);

        long elapsed = System.currentTimeMillis() - start;
        System.out.println("===================================");
        System.out.println("合计通过：" + TF.passed() + "，失败：" + TF.failed()
                + "，耗时 " + elapsed + " ms");
        int code = TF.finish();
        System.exit(code);
    }

    @FunctionalInterface
    private interface Suite {
        void run() throws Throwable;
    }

    private static void runSuite(String name, Suite suite) {
        int beforeFail = TF.failed();
        int beforePass = TF.passed();
        System.out.print("[" + name + "] ");
        try {
            suite.run();
        } catch (Throwable t) {
            // 套件级意外错误（通常是测试自身 bug）
            System.out.println();
            System.out.println("[套件异常] " + name + " -> " + t);
            t.printStackTrace(System.out);
            TF.countSuiteFailure(name, t);
        }
        int newFails = TF.failed() - beforeFail;
        int newPasses = TF.passed() - beforePass;
        System.out.println("  (" + newPasses + " 通过"
                + (newFails > 0 ? "，" + newFails + " 失败" : "") + ")");
    }
}
