package streamagg.test;

/**
 * Test suite entry point. Runs all registered test cases.
 * Usage: {@code java -cp ... streamagg.test.TestAll [name filter...]}
 */
public final class TestAll {

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        JsonTest.register(runner);
        EngineTest.register(runner);
        TimeTest.register(runner);
        HttpTest.register(runner);
        int code = runner.run(args);
        System.exit(code);
    }
}
