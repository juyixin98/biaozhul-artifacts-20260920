package com.example.iview;

import com.example.iview.server.HttpIntegrationTest;

/** Runs every test suite; process exit code is 0 only if all suites pass. */
public final class TestAll {

    public static void main(String[] args) throws Exception {
        int failures = 0;
        failures += JsonTest.run();
        failures += MaterializedViewTest.run();
        failures += HttpIntegrationTest.run();

        System.out.println();
        if (failures == 0) {
            System.out.println("ALL TEST SUITES PASSED");
            System.exit(0);
        }
        System.out.println(failures + " SUITE(S) HAD FAILURES");
        System.exit(1);
    }

    private TestAll() {
    }
}
