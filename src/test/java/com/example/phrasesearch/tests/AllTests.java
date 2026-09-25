package com.example.phrasesearch.tests;

/**
 * 全部自动化测试入口：纯 JDK，无 JUnit。
 * 运行：scripts/run_tests.sh（或 java -cp build/classes:build/test-classes ...AllTests）
 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        System.out.println("== Analyzer tests ==");
        AnalyzerTest.run();
        System.out.println();
        System.out.println("== Search semantics tests ==");
        SearchTest.run();
        System.out.println();
        System.out.println("== stop_gap search tests ==");
        StopGapSearchTest.run();
        System.out.println();
        System.out.println("== Brute-force equivalence tests ==");
        EquivalenceTest.run();
        System.out.println();
        System.out.println("== JSON tests ==");
        JsonTest.run();
        System.out.println();
        System.out.println("== HTTP server tests ==");
        HttpServerTest.run();

        Assert.finish();
    }
}
