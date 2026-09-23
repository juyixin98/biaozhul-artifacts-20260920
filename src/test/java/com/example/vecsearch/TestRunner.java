package com.example.vecsearch;

/** Test entry point: runs core tests then HTTP end-to-end tests. */
public class TestRunner {

    public static void main(String[] args) throws Exception {
        TestFramework t = new TestFramework();
        CoreTests.run(t);
        HttpE2eTests.run(t);
        System.exit(t.finish());
    }
}
