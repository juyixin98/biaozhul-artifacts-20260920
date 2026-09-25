package com.example.segmenter.tests;

/**
 * 测试入口：运行全部自动化测试，全部通过退出码 0，否则 1。
 *
 * <pre>java -cp build/classes:build/test-classes com.example.segmenter.tests.AllTests</pre>
 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) throws Exception {
        BasicSegmentationTest.run();
        ExhaustiveComparisonTest.run();
        TieBreakTest.run();
        NBestTest.run();
        EmptyStringTest.run();
        PolynomialComplexityTest.run();
        CorpusLoaderTest.run();
        JsonTest.run();
        HttpServerTest.run();

        System.exit(TestFramework.finish());
    }
}
