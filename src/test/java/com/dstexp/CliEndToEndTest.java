package com.dstexp;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.dstexp.cli.Main;
import com.dstexp.json.JsonMappers;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

class CliEndToEndTest {

    private static final ObjectMapper mapper = JsonMappers.mapper();

    private record Outputs(int exitCode, String stdout, String stderr) {
        JsonNode stdoutJson() throws Exception {
            return mapper.readTree(stdout);
        }
    }

    private static Outputs run(String... args) {
        PrintStream originalOut = System.out;
        PrintStream originalErr = System.err;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        try {
            System.setOut(new PrintStream(out, true, StandardCharsets.UTF_8));
            System.setErr(new PrintStream(err, true, StandardCharsets.UTF_8));
            int code = Main.execute(args);
            return new Outputs(code, out.toString(StandardCharsets.UTF_8), err.toString(StandardCharsets.UTF_8));
        } finally {
            System.setOut(originalOut);
            System.setErr(originalErr);
        }
    }

    private static Outputs runWithStdin(String json, String... args) {
        var originalIn = System.in;
        try {
            System.setIn(new ByteArrayInputStream(json.getBytes(StandardCharsets.UTF_8)));
            return run(args);
        } finally {
            System.setIn(originalIn);
        }
    }

    @Test
    @DisplayName("expand reads a request file and writes a success JSON envelope to stdout")
    void expandFileToStdout(@TempDir Path dir) throws Exception {
        Path request = Files.writeString(dir.resolve("req.json"), """
                {
                  "zoneId": "America/New_York",
                  "fromDate": "2026-03-08",
                  "toDate": "2026-03-08",
                  "rules": [ { "ruleId": "r", "type": "daily", "localTime": "02:30" } ],
                  "gapPolicy": "earlier",
                  "overlapPolicy": "later"
                }
                """);

        var result = run("expand", request.toString());

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("success").asBoolean()).isTrue();
        assertThat(json.get("zoneId").asText()).isEqualTo("America/New_York");
        assertThat(json.get("zoneRules").get("tzdbVersion").asText()).matches("20\\d{2}[a-z]");
        assertThat(json.get("occurrences").get(0).get("utcInstant").asText())
                .isEqualTo("2026-03-08T06:30:00Z");
        assertThat(json.get("occurrences").get(0).get("kind").asText()).isEqualTo("GAP_EARLIER");
    }

    @Test
    @DisplayName("expand reads JSON from stdin when no file is supplied")
    void expandFromStdin() throws Exception {
        var result = runWithStdin("""
                {
                  "zoneId": "UTC",
                  "fromDate": "2026-01-01",
                  "toDate": "2026-01-01",
                  "rules": [ { "ruleId": "r", "type": "cron", "cron": "0 0 1 1 *" } ]
                }
                """, "expand");

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("occurrences").get(0).get("utcInstant").asText())
                .isEqualTo("2026-01-01T00:00:00Z");
    }

    @Test
    @DisplayName("expand -o writes the response to a file")
    void expandToOutputFile(@TempDir Path dir) throws Exception {
        Path request = dir.resolve("req.json");
        Path response = dir.resolve("resp.json");
        Files.writeString(request, """
                {
                  "zoneId": "UTC",
                  "fromDate": "2026-01-01",
                  "toDate": "2026-01-01",
                  "rules": [ { "ruleId": "r", "type": "daily", "localTime": "00:00" } ]
                }
                """);

        var result = run("expand", request.toString(), "-o", response.toString());

        assertThat(result.exitCode()).isZero();
        assertThat(result.stdout()).isEmpty();
        JsonNode json = mapper.readTree(response.toFile());
        assertThat(json.get("success").asBoolean()).isTrue();
        assertThat(json.get("occurrenceCount").asInt()).isOne();
    }

    @Test
    @DisplayName("gapPolicy=error exits non-zero with a GAP_ENCOUNTERED error envelope")
    void errorEnvelope(@TempDir Path dir) throws Exception {
        Path request = Files.writeString(dir.resolve("req.json"), """
                {
                  "zoneId": "America/New_York",
                  "fromDate": "2026-03-08",
                  "toDate": "2026-03-08",
                  "rules": [ { "ruleId": "r", "type": "daily", "localTime": "02:30" } ],
                  "gapPolicy": "error",
                  "overlapPolicy": "error"
                }
                """);

        var result = run("expand", request.toString());

        assertThat(result.exitCode()).isEqualTo(2);
        JsonNode json = mapper.readTree(result.stderr());
        assertThat(json.get("success").asBoolean()).isFalse();
        assertThat(json.get("code").asText()).isEqualTo("GAP_ENCOUNTERED");
    }

    @Test
    @DisplayName("malformed JSON yields INVALID_JSON and exit code 2")
    void invalidJson(@TempDir Path dir) throws Exception {
        Path request = Files.writeString(dir.resolve("req.json"), "{ not json");

        var result = run("expand", request.toString());

        assertThat(result.exitCode()).isEqualTo(2);
        assertThat(mapper.readTree(result.stderr()).get("code").asText()).isEqualTo("INVALID_JSON");
    }

    @Test
    @DisplayName("missing input file yields an I/O failure exit code")
    void missingFile() {
        var result = run("expand", "/nonexistent/does-not-exist.json");
        assertThat(result.exitCode()).isEqualTo(74);
    }

    @Test
    @DisplayName("tzdb reports the bundled database version")
    void tzdbCommand() throws Exception {
        var result = run("tzdb");

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("tzdbVersion").asText()).matches("20\\d{2}[a-z]");
        assertThat(json.get("availableZoneCount").asInt()).isGreaterThan(300);
    }

    @Test
    @DisplayName("bundled spring demo expands the fixed gap dataset")
    void demoSpring() throws Exception {
        var result = run("demo", "spring");

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("occurrenceCount").asInt()).isGreaterThanOrEqualTo(3);
        assertThat(json.get("zoneRules").get("relevantTransitions"))
                .extracting(t -> t.get("type").asText())
                .containsOnly("GAP");
    }

    @Test
    @DisplayName("bundled fall demo expands the fixed overlap dataset")
    void demoFall() throws Exception {
        var result = run("demo", "fall");

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("zoneId").asText()).isEqualTo("America/New_York");
        assertThat(json.get("zoneRules").get("relevantTransitions"))
                .extracting(t -> t.get("type").asText())
                .containsOnly("OVERLAP");
        assertThat(json.get("occurrences")).isNotEmpty();
    }

    @Test
    @DisplayName("bundled cross-year demo spans both years in Sydney")
    void demoCrossYear() throws Exception {
        var result = run("demo", "cross-year");

        assertThat(result.exitCode()).isZero();
        JsonNode json = result.stdoutJson();
        assertThat(json.get("fromDate").asText()).isEqualTo("2026-12-30");
        assertThat(json.get("toDate").asText()).isEqualTo("2027-01-03");
        var instants = json.get("occurrences").findValuesAsText("utcInstant");
        assertThat(instants).anyMatch(s -> s.startsWith("2026-"));
        assertThat(instants).anyMatch(s -> s.startsWith("2027-"));
    }

    @Test
    @DisplayName("bundled error demo fails with GAP_ENCOUNTERED")
    void demoError() throws Exception {
        var result = run("demo", "error");

        assertThat(result.exitCode()).isEqualTo(2);
        assertThat(mapper.readTree(result.stderr()).get("code").asText()).isEqualTo("GAP_ENCOUNTERED");
    }

    @Test
    @DisplayName("unknown demo dataset fails with usage exit code")
    void unknownDemo() {
        assertThat(run("demo", "bogus").exitCode()).isEqualTo(64);
    }

    @Test
    @DisplayName("unknown command fails with usage exit code")
    void unknownCommand() {
        assertThat(run("nope").exitCode()).isEqualTo(64);
        assertThat(run().exitCode()).isEqualTo(64);
    }
}
