package bitemporal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bitemporal.json.JsonMapper;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * 进程内直接调用 {@link Main#run}，覆盖命令分发、JSON 信封与退出码映射。
 */
class MainInProcessTest {

    private static final ObjectMapper MAPPER = JsonMapper.get();

    @TempDir
    Path tempDir;

    private Path db;

    @BeforeEach
    void setUp() {
        db = tempDir.resolve("inproc-db.json");
    }

    private record Executed(int exitCode, JsonNode json, String stderr) {
    }

    private Executed exec(String command, String body) {
        InputStream in = body == null ? InputStream.nullInputStream()
                : new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = Main.run(new String[]{command}, in,
                new PrintStream(out, true, StandardCharsets.UTF_8),
                new PrintStream(err, true, StandardCharsets.UTF_8), db);
        try {
            JsonNode json = out.size() == 0 ? null : MAPPER.readTree(out.toString(StandardCharsets.UTF_8));
            return new Executed(code, json, err.toString(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    @Test
    void missingArgumentsExitInternalAndPrintUsageToStderr() {
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = Main.run(new String[]{}, InputStream.nullInputStream(),
                new PrintStream(new ByteArrayOutputStream()),
                new PrintStream(err), db);
        assertEquals(Main.EXIT_INTERNAL, code);
        assertTrue(err.toString(StandardCharsets.UTF_8).contains("missing command"));
    }

    @Test
    void unknownCommandIsBusinessValidationError() {
        Executed r = exec("frobnicate", null);
        assertEquals(Main.EXIT_BUSINESS, r.exitCode());
        assertEquals(false, r.json().path("success").asBoolean());
        assertEquals("VALIDATION_ERROR", r.json().path("error").path("code").asText());
    }

    @Test
    void malformedJsonReturnsBusinessError() {
        Executed r = exec("query", "{ not json");
        assertEquals(Main.EXIT_BUSINESS, r.exitCode());
        assertEquals("VALIDATION_ERROR", r.json().path("error").path("code").asText());
    }

    @Test
    void seedQueryAndQueryMissFlow() {
        Executed seed = exec("seed", null);
        assertEquals(Main.EXIT_OK, seed.exitCode());
        assertEquals("seed-txn", seed.json().path("data").path("txnId").asText());

        String q = """
                {"validAt":"2026-05-01T00:00:00Z","systemAt":"2026-02-01T00:00:00Z"}
                """;
        Executed hit = exec("query", q);
        assertEquals(Main.EXIT_OK, hit.exitCode());
        assertEquals(2, hit.json().path("data").path("count").asInt(),
                "both seeded records are valid in May at an observation time after seed");

        String miss = """
                {"observationTime":"2025-01-01T00:00:00Z"}
                """;
        Executed empty = exec("query", miss);
        assertEquals(0, empty.json().path("data").path("count").asInt());

        Executed info = exec("info", null);
        assertTrue(info.json().path("data").path("tzdbVersion").asText().matches("\\d{4}.*"));
    }

    @Test
    void overlapCommitReturnsExitCode2() {
        exec("seed", null);
        String bad = """
                {
                  "committedAt": "2026-03-05T00:00:00Z",
                  "changes": [
                    {"op":"insert","recordId":"emp-1001","data":"x",
                     "validFrom":"2026-02-01T00:00:00Z","validTo":"2026-05-01T00:00:00Z"}
                  ]
                }
                """;
        Executed r = exec("commit", bad);
        assertEquals(Main.EXIT_BUSINESS, r.exitCode());
        assertEquals("OVERLAP_REJECTED", r.json().path("error").path("code").asText());
    }

    @Test
    void reviseWithoutBodyFailsValidation() {
        exec("seed", null);
        Executed r = exec("history", null);
        // Jackson 对空流会产生连接闭合异常，归入业务校验错误。
        assertEquals(Main.EXIT_BUSINESS, r.exitCode());
    }

    @Test
    void batchOverStdinRunsStepsAndAborts() {
        String batch = """
                {
                  "steps": [
                    {"type":"seed"},
                    {"type":"history","recordId":"emp-1001"},
                    {"type":"commit", "committedAt":"2026-03-05T00:00:00Z",
                     "changes":[{"op":"insert","recordId":"emp-1001","data":"x",
                       "validFrom":"2026-02-01T00:00:00Z","validTo":"2026-05-01T00:00:00Z"}]}
                  ]
                }
                """;
        Executed r = exec("batch", batch);
        assertEquals(Main.EXIT_OK, r.exitCode());
        JsonNode steps = r.json().path("data").path("steps");
        assertEquals(3, steps.size(), "the failing step is recorded, then the batch aborts");
        assertEquals(2, r.json().path("data").path("abortedAt").asInt());
        assertEquals(true, steps.get(1).path("success").asBoolean());
        assertEquals(false, steps.get(2).path("success").asBoolean());
        assertEquals("OVERLAP_REJECTED", steps.get(2).path("error").path("code").asText());
    }

    @Test
    void resetCommandWipesData() {
        exec("seed", null);
        Executed reset = exec("reset", null);
        assertEquals(Main.EXIT_OK, reset.exitCode());
        Executed info = exec("info", null);
        assertEquals(0, info.json().path("data").path("recordIds").size());
    }

    @Test
    void corruptDbFileFailsInternally() throws Exception {
        exec("seed", null);
        java.nio.file.Files.writeString(db, "{ this is not valid json");
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = Main.run(new String[]{"info"}, InputStream.nullInputStream(),
                new PrintStream(new ByteArrayOutputStream()), new PrintStream(err), db);
        assertEquals(Main.EXIT_INTERNAL, code);
        assertTrue(err.toString(StandardCharsets.UTF_8).contains("failed to read"));
    }
}
