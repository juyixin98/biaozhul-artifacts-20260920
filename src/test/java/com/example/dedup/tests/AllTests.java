package com.example.dedup.tests;

/** Entry point for the zero-dependency test suite. */
public final class AllTests {

    public static void main(String[] args) {
        TestRunner r = new TestRunner();
        System.out.println("== unit: dedup ==");
        DedupStateTest.register(r);
        System.out.println("== unit: tombstones ==");
        TombstoneStoreTest.register(r);
        System.out.println("== unit: windows ==");
        WindowOperatorTest.register(r);
        System.out.println("== unit: watermarks ==");
        WatermarkTest.register(r);
        System.out.println("== unit: time/scheduler ==");
        TimeTest.register(r);
        System.out.println("== acceptance ==");
        AcceptanceTest.register(r);
        System.out.println("== differential vs reference ==");
        DifferentialTest.register(r);
        System.out.println("== json ==");
        JsonTest.register(r);
        System.out.println("== http service ==");
        HttpServiceTest.register(r);
        int code = r.summary();
        System.exit(code);
    }
}
