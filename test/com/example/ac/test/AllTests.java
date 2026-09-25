package com.example.ac.test;

import java.util.List;

/** 测试入口：运行全部 TestCase，按失败数设置进程退出码。 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        List<TestCase> tests = List.of(
                new CoreAhoCorasickTest(),
                new EmptyPatternTest(),
                new UnicodeTest(),
                new CrossChunkTest(),
                new FuzzTest(),
                new SharedPrefixTest(),
                new CorpusTest(),
                new ServerE2ETest());
        int failures = TestCase.runAll(tests, System.out::println);
        if (failures > 0) {
            System.out.println("RESULT: FAILURE (" + failures + " failures)");
            System.exit(1);
        }
        System.out.println("RESULT: ALL TESTS PASSED");
    }
}
