package com.timeconv;

import java.nio.file.Path;

/** Test entry point: runs all suites and exits non-zero on any failure. */
public final class TestMain {

    public static void main(String[] args) {
        Path root = args.length > 0 ? Path.of(args[0]) : Path.of("").toAbsolutePath();
        System.out.println("tzdb version: " + TzdbInfo.version());

        JsonTest.register();
        TimeConverterTest.register();
        TextParserTest.register();
        CaseFileTest.register(root.resolve("data/cases"));
        ApiIntegrationTest.register(root.resolve("samples"));

        System.exit(TestRunner.finish());
    }

    private TestMain() {
    }
}
