package neardup;

/** Test entry point: runs every test group and exits non-zero on any failure. */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) throws Exception {
        ShinglerTest.run();
        JaccardTest.run();
        MinHashTest.run();
        LshTest.run();
        ClustererTest.run();
        ApiServerTest.run();
        System.exit(TestRunner.finish());
    }
}
