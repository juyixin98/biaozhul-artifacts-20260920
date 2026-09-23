package com.opp16.engine.tests;

/** Entry point for all built-in tests; exits non-zero on any failure. */
public final class RunTests {
    public static void main(String[] args) {
        NullBitmapTests.run();
        SelectionVectorTests.run();
        PredicateTests.run();
        JsonTests.run();
        EngineTests.run();
        BatchBoundaryTests.run();
        System.exit(Assert.summary());
    }
}
