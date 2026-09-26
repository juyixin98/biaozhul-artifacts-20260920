package com.example.dstexpand;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * CLI contract: stdin/file input, JSON error body, documented exit codes.
 * Stdout is captured so the build log stays clean.
 */
class MainTest {

    private static final String REQUEST = """
            {
              "zoneId": "America/New_York",
              "startDate": "2026-03-08",
              "endDate": "2026-03-08",
              "rules": [ {"time": "02:30"} ],
              "gapPolicy": "ERROR"
            }
            """;

    /** Result of one CLI invocation. */
    private record RunResult(int exitCode, String stdout) {
    }

    private static RunResult run(String[] args, String stdin) {
        ByteArrayOutputStream captured = new ByteArrayOutputStream();
        PrintStream original = System.out;
        System.setOut(new PrintStream(captured, true, StandardCharsets.UTF_8));
        try {
            int code = Main.run(args,
                    new ByteArrayInputStream(stdin.getBytes(StandardCharsets.UTF_8)));
            return new RunResult(code, captured.toString(StandardCharsets.UTF_8));
        } finally {
            System.setOut(original);
        }
    }

    @Test
    void stdinRequestWithPolicyErrorExitsTwoAndEmitsJsonError() {
        RunResult r = run(new String[0], REQUEST);
        assertEquals(2, r.exitCode());
        assertTrue(r.stdout().contains("\"error\""));
        assertTrue(r.stdout().contains("does not exist"));
    }

    @Test
    void fileRequestSucceeds() throws Exception {
        Path tmp = Files.createTempFile("request", ".json");
        Files.writeString(tmp, REQUEST.replace("\"ERROR\"", "\"SKIP\""));
        RunResult r = run(new String[]{tmp.toString()}, "");
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("GAP_SKIPPED"));
    }

    @Test
    void malformedJsonExitsOne() {
        RunResult r = run(new String[0], "{not json");
        assertEquals(1, r.exitCode());
        assertTrue(r.stdout().contains("malformed JSON"));
    }

    @Test
    void missingFileExitsOne() {
        RunResult r = run(new String[]{"/nonexistent/request.json"}, "");
        assertEquals(1, r.exitCode());
    }

    @Test
    void successOutputContainsTzdbVersion() {
        RunResult r = run(new String[0], REQUEST.replace("\"ERROR\"", "\"LATER\""));
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("tzdbVersion"));
    }
}
