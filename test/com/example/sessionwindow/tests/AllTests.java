package com.example.sessionwindow.tests;

/** Runs all test suites. Exit code 0 iff everything passes. */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        int exitCode = TestRunner.run(
                JsonTest.class,
                SessionWindowEngineTest.class,
                ReferenceEquivalenceTest.class,
                BatchProcessorTest.class,
                WatermarkGeneratorTest.class,
                TimerServiceTest.class,
                SessionWindowServiceTest.class,
                HttpServerTest.class);
        System.exit(exitCode);
    }
}
