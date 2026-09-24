package phraseindex;

import java.util.ArrayList;
import java.util.List;

/** Runs every test suite and exits non-zero if anything failed. */
public final class AllTests {

    public static void main(String[] args) {
        List<Suite> suites = new ArrayList<>();
        register(suites, "Tokenizer", TokenizerTest::register);
        register(suites, "QueryParser", QueryParserTest::register);
        register(suites, "InvertedIndex", InvertedIndexTest::register);
        register(suites, "Differential", DifferentialTest::register);
        register(suites, "HttpE2E", HttpE2ETest::register);

        boolean allPassed = true;
        int totalPassed = 0;
        for (Suite suite : suites) {
            allPassed &= suite.report();
        }
        if (!allPassed) {
            System.out.println("\nTEST RUN FAILED");
            System.exit(1);
        }
        System.out.println("\nALL TESTS PASSED");
    }

    private static void register(List<Suite> suites, String name,
                                 java.util.function.Consumer<Suite> registration) {
        Suite suite = new Suite(name);
        registration.accept(suite);
        suites.add(suite);
    }
}
