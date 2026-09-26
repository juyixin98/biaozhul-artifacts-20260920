package com.example.recurrence;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MainEndToEndTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private record RunResult(int exitCode, JsonNode json) {
    }

    private RunResult runWithStdin(String body) throws Exception {
        ByteArrayInputStream in = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            int exit = Main.run(new String[0], in, out);
            return new RunResult(exit, mapper.readTree(bytes.toString(StandardCharsets.UTF_8)));
        }
    }

    @Test
    void endToEndDailySuccess() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "e2e-1",
                  "rule": { "dtStart": "2026-01-01", "freq": "DAILY", "count": 3 },
                  "windowStart": "2026-01-01",
                  "windowEnd": "2026-01-31"
                }
                """;

        // Act
        RunResult result = runWithStdin(body);

        // Assert
        assertEquals(0, result.exitCode());
        assertTrue(result.json().get("ok").asBoolean());
        assertEquals("e2e-1", result.json().get("requestId").asText());
        assertEquals(3, result.json().get("count").asInt());
        assertEquals("2026-01-01", result.json().get("occurrences").get(0).asText());
        assertFalse(result.json().get("tzdbVersion").asText().isBlank());
    }

    @Test
    void endToEndMonthly31stSkip() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "e2e-31",
                  "rule": { "dtStart": "2025-01-31", "freq": "MONTHLY",
                            "monthEndPolicy": "SKIP" },
                  "windowStart": "2025-01-01",
                  "windowEnd": "2025-12-31"
                }
                """;

        // Act
        RunResult result = runWithStdin(body);

        // Assert: 7 months have 31 days in 2025 (Feb has 28, non-leap year)
        assertEquals(0, result.exitCode());
        assertEquals(7, result.json().get("count").asInt());
    }

    @Test
    void endToEndEmptyWindow() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "e2e-empty",
                  "rule": { "dtStart": "2030-01-01", "freq": "MONTHLY" },
                  "windowStart": "2026-01-01",
                  "windowEnd": "2026-12-31"
                }
                """;

        // Act
        RunResult result = runWithStdin(body);

        // Assert
        assertEquals(0, result.exitCode());
        assertEquals(0, result.json().get("count").asInt());
        assertTrue(result.json().get("occurrences").isEmpty());
    }

    @Test
    void endToEndLimitExceededReturnsErrorCodeAndExit2() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "e2e-limit",
                  "rule": { "dtStart": "2026-01-01", "freq": "DAILY" },
                  "windowStart": "2026-01-01",
                  "windowEnd": "2026-12-31",
                  "maxExpansions": 5
                }
                """;

        // Act
        RunResult result = runWithStdin(body);

        // Assert
        assertEquals(2, result.exitCode());
        assertFalse(result.json().get("ok").asBoolean());
        assertEquals("EXPANSION_LIMIT_EXCEEDED",
                result.json().get("error").get("code").asText());
    }

    @Test
    void endToEndValidationErrorReturnsStableCode() throws Exception {
        // Arrange
        String body = """
                {
                  "requestId": "e2e-bad",
                  "rule": { "dtStart": "2026-01-01", "freq": "HOURLY" },
                  "windowStart": "2026-01-01",
                  "windowEnd": "2026-01-31"
                }
                """;

        // Act
        RunResult result = runWithStdin(body);

        // Assert
        assertEquals(2, result.exitCode());
        assertEquals("INVALID_FREQ", result.json().get("error").get("code").asText());
    }

    @Test
    void endToEndInvalidJsonIsRejected() throws Exception {
        // Act
        RunResult result = runWithStdin("{ not json");

        // Assert
        assertEquals(2, result.exitCode());
        assertEquals("INVALID_JSON", result.json().get("error").get("code").asText());
    }

    @Test
    void endToEndReadsRequestFileArgument() throws Exception {
        // Arrange: a real file on disk, as the CLI is invoked in the README
        Path file = Files.createTempFile("request", ".json");
        Files.writeString(file, """
                {
                  "requestId": "e2e-file",
                  "rule": { "dtStart": "2024-02-29", "freq": "MONTHLY", "interval": 12,
                            "count": 2, "monthEndPolicy": "SKIP" },
                  "windowStart": "2024-01-01",
                  "windowEnd": "2030-12-31"
                }
                """);

        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (PrintStream out = new PrintStream(bytes, true, StandardCharsets.UTF_8)) {
            // Act
            int exit = Main.run(new String[]{file.toString()},
                    new ByteArrayInputStream(new byte[0]), out);
            JsonNode json = mapper.readTree(bytes.toString(StandardCharsets.UTF_8));

            // Assert: leap days 2024-02-29 and 2028-02-29
            assertEquals(0, exit);
            assertEquals(2, json.get("count").asInt());
            assertEquals("2024-02-29", json.get("occurrences").get(0).asText());
            assertEquals("2028-02-29", json.get("occurrences").get(1).asText());
        }
    }
}
