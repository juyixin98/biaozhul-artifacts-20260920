package com.example.intervals.cli;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * In-process integration tests for the CLI boundary: argument handling,
 * stdin/file input, success and error envelopes and exit codes.
 */
class MainTest {

    private final PrintStream originalOut = System.out;
    private final PrintStream originalErr = System.err;
    private final java.io.InputStream originalIn = System.in;
    private ByteArrayOutputStream out;
    private ByteArrayOutputStream err;

    @BeforeEach
    void redirect() {
        out = new ByteArrayOutputStream();
        err = new ByteArrayOutputStream();
        System.setOut(new PrintStream(out, true, StandardCharsets.UTF_8));
        System.setErr(new PrintStream(err, true, StandardCharsets.UTF_8));
    }

    @AfterEach
    void restore() {
        System.setOut(originalOut);
        System.setErr(originalErr);
        System.setIn(originalIn);
    }

    private String validRequest() {
        return """
                {
                  "domain": "time",
                  "sets": {
                    "A": [{"lower":"2024-01-01T00:00:00Z","lowerOpen":false,
                           "upper":"2024-06-01T00:00:00Z","upperOpen":true}],
                    "B": [{"lower":"2024-03-01T00:00:00Z","lowerOpen":false,
                           "upper":"2024-09-01T00:00:00Z","upperOpen":false}]
                  },
                  "expression": {"op":"intersection","left":{"set":"A"},"right":{"set":"B"}}
                }
                """;
    }

    @Test
    void readsRequestFromStdinAndReturnsSuccess() {
        System.setIn(new ByteArrayInputStream(validRequest().getBytes(StandardCharsets.UTF_8)));
        int exit = new Main().run(new String[0]);
        assertEquals(0, exit);
        String json = out.toString(StandardCharsets.UTF_8);
        assertTrue(json.contains("\"success\" : true"));
        assertTrue(json.contains("jreTzDataVersion"));
    }

    @Test
    void readsRequestFromFile(@TempDir Path dir) throws Exception {
        Path file = dir.resolve("req.json");
        Files.writeString(file, validRequest(), StandardCharsets.UTF_8);
        int exit = new Main().run(new String[]{"--file", file.toString()});
        assertEquals(0, exit);
        assertTrue(out.toString(StandardCharsets.UTF_8).contains("\"success\" : true"));
    }

    @Test
    void reversedIntervalYieldsErrorEnvelopeAndExitOne(@TempDir Path dir) throws Exception {
        String bad = """
                {"domain":"version",
                 "sets":{"A":[{"lower":"v9","lowerOpen":false,"upper":"v1","upperOpen":false}]},
                 "expression":{"set":"A"}}
                """;
        Path file = dir.resolve("bad.json");
        Files.writeString(file, bad, StandardCharsets.UTF_8);
        int exit = new Main().run(new String[]{"--file", file.toString()});
        assertEquals(1, exit);
        String json = out.toString(StandardCharsets.UTF_8);
        assertTrue(json.contains("\"success\" : false"));
        assertTrue(json.contains("reversed_interval"));
    }

    @Test
    void invalidJsonYieldsInvalidRequest(@TempDir Path dir) throws Exception {
        Path file = dir.resolve("broken.json");
        Files.writeString(file, "{ not json", StandardCharsets.UTF_8);
        int exit = new Main().run(new String[]{"--file", file.toString()});
        assertEquals(1, exit);
        assertTrue(out.toString(StandardCharsets.UTF_8).contains("invalid_request"));
    }

    @Test
    void missingFileIsRejected() {
        int exit = new Main().run(new String[]{"--file", "/no/such/file.json"});
        assertEquals(1, exit);
        assertTrue(out.toString(StandardCharsets.UTF_8).contains("invalid_request"));
    }

    @Test
    void fileFlagWithoutPathIsUsageError() {
        int exit = new Main().run(new String[]{"--file"});
        assertEquals(2, exit);
        assertTrue(err.toString(StandardCharsets.UTF_8).contains("usage"));
    }

    @Test
    void datasetsCommandListsFixedData() {
        int exit = new Main().run(new String[]{"datasets"});
        assertEquals(0, exit);
        String json = out.toString(StandardCharsets.UTF_8);
        assertTrue(json.contains("time-adjacency"));
        assertTrue(json.contains("version-rollout"));
    }

    @Test
    void timezoneCommandPrintsProvenance() {
        int exit = new Main().run(new String[]{"timezone"});
        assertEquals(0, exit);
        assertTrue(out.toString(StandardCharsets.UTF_8).contains("jreTzDataVersion"));
    }

    @Test
    void helpSucceedsAndUnknownArgIsUsageError() {
        assertEquals(0, new Main().run(new String[]{"help"}));
        assertTrue(out.toString().contains("Interval Set Algebra"));

        redirect();
        assertEquals(2, new Main().run(new String[]{"bogus"}));
        assertTrue(err.toString().contains("unknown argument"));
    }
}
