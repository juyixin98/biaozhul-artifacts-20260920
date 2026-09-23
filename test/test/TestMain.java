package test;

import test.cms.BoundedCandidateSetTest;
import test.cms.CountMinSketchTest;
import test.cms.ExactCounterTest;
import test.exp.AcceptanceExperiments;
import test.json.JsonTest;
import test.server.HttpServiceTest;
import test.stream.FrequentItemsEngineTest;

/** Entry point for all automated tests (run via {@code scripts/run-tests.sh}). */
public final class TestMain {

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        CountMinSketchTest.register(runner);
        BoundedCandidateSetTest.register(runner);
        ExactCounterTest.register(runner);
        FrequentItemsEngineTest.register(runner);
        JsonTest.register(runner);
        HttpServiceTest.register(runner);
        AcceptanceExperiments.register(runner);
        int exit = runner.runAll();
        System.exit(exit);
    }
}
