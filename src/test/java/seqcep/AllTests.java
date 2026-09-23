package seqcep;

/** Test entry point: registers all suites and runs them. */
public final class AllTests {
    public static void main(String[] args) {
        JsonTests.register();
        EngineTests.register();
        RecoveryTests.register();
        ApiTests.register();
        System.exit(TestRunner.runAll());
    }

    private AllTests() {}
}
