package com.example.diff;

import com.example.diff.corpus.SearchEngineTests;
import com.example.diff.json.JsonTests;
import com.example.diff.server.HttpServerTests;

/** Runs every test suite; exit code 0 only when all tests pass. */
public final class RunAllTests {

    private RunAllTests() {
    }

    public static void main(String[] args) {
        int code = TestFramework.runSuites(
                LinesTests::register,
                MyersDiffTests::register,
                DiffPropertyTests::register,
                ApplyEditsTests::register,
                JsonTests::register,
                SearchEngineTests::register,
                HttpServerTests::register
        );
        System.exit(code);
    }
}
