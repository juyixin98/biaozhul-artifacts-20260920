package bitemporal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bitemporal.json.JsonMapper;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.MethodOrderer.OrderAnnotation;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.io.TempDir;

/**
 * 通过真实子进程调用 {@link Main}，验证 JSON 标准输入输出、退出码与跨进程持久化。
 * 测试方法按声明顺序执行，共用一个临时 db 文件。
 */
@TestMethodOrder(OrderAnnotation.class)
class CliEndToEndTest {

    private static final ObjectMapper MAPPER = JsonMapper.get();

    @TempDir
    static Path tempDir;

    private static Path dbFile;

    private record Result(int exitCode, JsonNode json) {
    }

    private Result run(String command, String body) throws IOException, InterruptedException {
        if (dbFile == null) {
            dbFile = tempDir.resolve("cli-db.json");
        }
        String classpath = Files.readString(
                Path.of("target/dependency.classpath"), StandardCharsets.UTF_8).trim();
        List<String> cmd = new ArrayList<>(List.of(
                "java", "-cp", "target/classes" + java.io.File.pathSeparator + classpath,
                "bitemporal.Main", command));

        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.environment().put("BITEMPORAL_DB", dbFile.toString());
        Process process = pb.start();
        if (body != null) {
            try (var stdin = process.getOutputStream()) {
                stdin.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        boolean finished = process.waitFor(30, TimeUnit.SECONDS);
        assertTrue(finished, "CLI process timed out");
        String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        int exit = process.exitValue();
        if (output.isBlank()) {
            String err = new String(process.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            throw new IllegalStateException("empty stdout, exit=" + exit + ", stderr=" + err);
        }
        return new Result(exit, MAPPER.readTree(output));
    }

    @Test
    @Order(1)
    void resetsAndSeedsFixedData() throws Exception {
        Result reset = run("reset", null);
        assertEquals(0, reset.exitCode());
        assertTrue(reset.json().path("success").asBoolean());

        Result seed = run("seed", null);
        assertEquals(0, seed.exitCode());
        assertEquals("seed-txn", seed.json().path("data").path("txnId").asText());
        assertEquals(4, seed.json().path("data").path("written").size());
    }

    @Test
    @Order(2)
    void infoReportsTzdbVersion() throws Exception {
        Result info = run("info", null);
        assertEquals(0, info.exitCode());
        JsonNode version = info.json().path("data").path("tzdbVersion");
        assertTrue(version.asText().matches("\\d{4}.*"),
                "expected IANA tzdb version like 2026b, got: " + version.asText());
        assertTrue(info.json().path("data").path("availableZoneCount").asInt() > 300);
    }

    @Test
    @Order(3)
    void queryBeforeRetroFixShowsOriginalValue() throws Exception {
        String q = """
                {
                  "validAt": "2026-02-15T00:00:00Z",
                  "systemAt": "2026-02-15T12:00:00Z",
                  "recordId": "emp-1001"
                }
                """;
        Result r = run("query", q);
        assertEquals(0, r.exitCode());
        JsonNode rows = r.json().path("data").path("rows");
        assertEquals(1, rows.size());
        assertTrue(rows.get(0).path("data").asText().contains("\"level\":\"L3\""));
        assertEquals(1L, rows.get(0).path("versionId").asLong());
    }

    @Test
    @Order(4)
    void commitsRetrospectiveRevision() throws Exception {
        String txn = """
                {
                  "txnId": "fix-q1-level",
                  "committedAt": "2026-03-01T09:00:00Z",
                  "changes": [
                    {
                      "op": "revise",
                      "recordId": "emp-1001",
                      "data": "{\\"level\\":\\"L2\\",\\"team\\":\\"Platform\\"}",
                      "validFrom": "2026-01-15T00:00:00Z",
                      "validTo": "2026-03-01T00:00:00Z"
                    }
                  ]
                }
                """;
        Result r = run("commit", txn);
        assertEquals(0, r.exitCode());
        assertEquals("fix-q1-level", r.json().path("data").path("txnId").asText());
        // 左残段 + 新值 + 右残段三行（修订窗口严格落在 Q1 版本内部）。
        assertEquals(3, r.json().path("data").path("written").size());
    }

    @Test
    @Order(5)
    void queryAfterFixShowsCorrectedAndOldStillReachable() throws Exception {
        String after = """
                {
                  "validAt": "2026-02-15T00:00:00Z",
                  "systemAt": "2026-03-02T00:00:00Z",
                  "recordId": "emp-1001"
                }
                """;
        Result corrected = run("query", after);
        assertEquals(0, corrected.exitCode());
        JsonNode row = corrected.json().path("data").path("rows").get(0);
        assertTrue(row.path("data").asText().contains("\"level\":\"L2\""));
        assertEquals("fix-q1-level", row.path("txnId").asText());

        // 同一业务时刻、修订前的系统观察时刻 → 旧信念仍可重现。
        String before = """
                {
                  "validAt": "2026-02-15T00:00:00Z",
                  "systemAt": "2026-02-15T12:00:00Z",
                  "recordId": "emp-1001"
                }
                """;
        Result old = run("query", before);
        assertTrue(old.json().path("data").path("rows").get(0).path("data").asText()
                .contains("\"level\":\"L3\""));
    }

    @Test
    @Order(6)
    void overlappingInsertIsRejectedWithExitCode2() throws Exception {
        String bad = """
                {
                  "txnId": "overlap-bad",
                  "committedAt": "2026-03-05T00:00:00Z",
                  "changes": [
                    {
                      "op": "insert",
                      "recordId": "emp-1001",
                      "data": "{\\"level\\":\\"XX\\"}",
                      "validFrom": "2026-02-01T00:00:00Z",
                      "validTo": "2026-05-01T00:00:00Z"
                    }
                  ]
                }
                """;
        Result r = run("commit", bad);
        assertEquals(2, r.exitCode(), "business rule violation must exit with code 2");
        assertEquals(false, r.json().path("success").asBoolean());
        assertEquals("OVERLAP_REJECTED", r.json().path("error").path("code").asText());
        assertEquals("emp-1001", r.json().path("error").path("recordId").asText());
    }

    @Test
    @Order(7)
    void sameTransactionCarriesMultipleChanges() throws Exception {
        String multi = """
                {
                  "txnId": "multi-changes",
                  "committedAt": "2026-03-06T00:00:00Z",
                  "changes": [
                    {"op": "insert", "recordId": "emp-1003", "data": "jan",
                     "validFrom": "2026-01-01T00:00:00Z", "validTo": "2026-02-01T00:00:00Z"},
                    {"op": "insert", "recordId": "emp-1003", "data": "feb",
                     "validFrom": "2026-02-01T00:00:00Z", "validTo": "2026-03-01T00:00:00Z"},
                    {"op": "revise", "recordId": "emp-1003", "data": "feb-fixed",
                     "validFrom": "2026-02-10T00:00:00Z", "validTo": "2026-02-20T00:00:00Z"}
                  ]
                }
                """;
        Result commit = run("commit", multi);
        assertEquals(0, commit.exitCode());

        String q = """
                {
                  "validAt": "2026-02-15T00:00:00Z",
                  "systemAt": "2026-03-07T00:00:00Z",
                  "recordId": "emp-1003"
                }
                """;
        Result queried = run("query", q);
        assertEquals("feb-fixed", queried.json().path("data").path("rows").get(0).path("data").asText());
    }

    @Test
    @Order(8)
    void historyExposesClosedAndCurrentRows() throws Exception {
        String body = "{\"recordId\":\"emp-1001\"}";
        Result r = run("history", body);
        assertEquals(0, r.exitCode());
        JsonNode rows = r.json().path("data").path("rows");
        // 种子 3 行 + 修订产生的 3 行 = 6 行；首行已带 systemTo。
        assertEquals(6, rows.size());
        assertEquals(false, rows.get(0).path("systemTo").isMissingNode(),
                "the original Q1 row must be closed but retained");
    }

    @Test
    @Order(9)
    void batchReplaysSeedFixAndQueriesInOneProcess() throws Exception {
        String batch = """
                {
                  "steps": [
                    {"type": "reset"},
                    {"type": "seed"},
                    {"type": "commit", "txnId": "b-fix", "committedAt": "2026-03-01T09:00:00Z",
                     "changes": [
                       {"op": "revise", "recordId": "emp-1001",
                        "data": "{\\"level\\":\\"L2\\",\\"team\\":\\"Platform\\"}",
                        "validFrom": "2026-01-15T00:00:00Z", "validTo": "2026-03-01T00:00:00Z"}
                     ]},
                    {"type": "query", "validAt": "2026-02-15T00:00:00Z",
                     "systemAt": "2026-02-15T12:00:00Z", "recordId": "emp-1001"},
                    {"type": "query", "validAt": "2026-02-15T00:00:00Z",
                     "systemAt": "2026-03-02T00:00:00Z", "recordId": "emp-1001"},
                    {"type": "commit", "txnId": "will-fail", "committedAt": "2026-03-05T00:00:00Z",
                     "changes": [
                       {"op": "insert", "recordId": "emp-1001", "data": "x",
                        "validFrom": "2026-02-01T00:00:00Z", "validTo": "2026-05-01T00:00:00Z"}
                     ], "continueOnError": true},
                    {"type": "history", "recordId": "emp-1001"}
                  ]
                }
                """;
        Result r = run("batch", batch);
        assertEquals(0, r.exitCode());
        JsonNode steps = r.json().path("data").path("steps");
        assertEquals(7, steps.size());

        JsonNode oldBelief = steps.get(3).path("result").path("rows").get(0);
        assertTrue(oldBelief.path("data").asText().contains("\"level\":\"L3\""));
        JsonNode newBelief = steps.get(4).path("result").path("rows").get(0);
        assertTrue(newBelief.path("data").asText().contains("\"level\":\"L2\""));

        assertEquals(false, steps.get(5).path("success").asBoolean());
        assertEquals("OVERLAP_REJECTED", steps.get(5).path("error").path("code").asText());
        // continueOnError=true → 最后的 history 仍然执行。
        assertEquals(true, steps.get(6).path("success").asBoolean());
    }
}
