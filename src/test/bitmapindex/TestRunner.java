package bitmapindex;

import java.util.List;

/**
 * Test entry point. Runs every suite with the zero-dependency harness and
 * exits non-zero if anything failed.
 *
 *   java -cp build/classes:build/test-classes bitmapindex.TestRunner
 */
public final class TestRunner {

    public static void main(String[] args) throws Exception {
        boolean http = !List.of(args).contains("--no-http");

        Asserts bitmapAsserts = new Asserts();
        RoaringBitmapTest.run(bitmapAsserts);
        bitmapAsserts.summary("RoaringBitmap");

        Asserts indexAsserts = new Asserts();
        BitMapIndexAcceptanceTest.run(indexAsserts);
        indexAsserts.summary("BitMapIndex acceptance");

        Asserts httpAsserts = new Asserts();
        if (http) {
            HttpE2ETest.run(httpAsserts);
        }
        httpAsserts.summary("HTTP end-to-end");

        int totalPass = bitmapAsserts.passed() + indexAsserts.passed() + httpAsserts.passed();
        int totalFail = bitmapAsserts.failed() + indexAsserts.failed() + httpAsserts.failed();
        System.out.println("------------------------------------------------------------");
        System.out.println("TOTAL: " + totalPass + " passed, " + totalFail + " failed");

        if (totalFail > 0) {
            System.out.println("\nFailures:");
            for (String f : bitmapAsserts.failures()) {
                System.out.println("  " + f);
            }
            for (String f : indexAsserts.failures()) {
                System.out.println("  " + f);
            }
            for (String f : httpAsserts.failures()) {
                System.out.println("  " + f);
            }
            System.exit(1);
        }
        System.out.println("ALL TESTS PASSED");
    }
}
