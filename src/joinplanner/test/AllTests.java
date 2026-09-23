package joinplanner.test;

/** Runs every test suite; process exit code is 0 only if all checks pass. */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) throws Exception {
        TestRunner r = new TestRunner();
        PlannerTest.run(r);
        ValidationTest.run(r);
        EdgeCaseTest.run(r);
        SkewTest.run(r);
        HttpTest.run(r);
        System.exit(r.finish());
    }
}
