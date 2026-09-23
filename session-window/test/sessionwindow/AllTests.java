package sessionwindow;

import java.util.List;

/** Entry point that runs every zero-dependency test suite. */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        int failures = TestRunner.runAll(List.of(
                AggregatorTest.build(),
                JsonTest.build(),
                HttpIntegrationTest.build()));
        System.exit(failures);
    }
}
