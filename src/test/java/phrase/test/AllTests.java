package phrase.test;

/** 测试总入口：注册并运行全部用例，进程退出码反映通过情况。 */
public final class AllTests {

    private AllTests() {}

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        AnalyzerTest.register(runner);
        MatcherTest.register(runner);
        BruteEquivTest.register(runner);
        SearcherTest.register(runner);
        JsonTest.register(runner);
        ApiTest.register(runner);
        System.exit(runner.run());
    }
}
