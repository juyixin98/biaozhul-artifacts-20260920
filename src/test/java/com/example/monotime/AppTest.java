package com.example.monotime;

import com.example.monotime.domain.ScheduledTimeout;
import com.example.monotime.persistence.PersistedTimeout;
import com.example.monotime.scenario.ScenarioReport;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class AppTest {

    @TempDir
    Path tempDir;

    @Test
    void infoReportsTzdbVersionAndEnvironment() throws Exception {
        // Act
        Object result = App.execute(new String[]{"info"});

        // Assert
        Map<?, ?> info = assertInstanceOf(Map.class, result);
        com.example.monotime.tzdb.TzdbInfo tzdb =
                assertInstanceOf(com.example.monotime.tzdb.TzdbInfo.class, info.get("tzdb"));
        assertTrue(tzdb.tzdbVersion().matches("\\d{4}[a-z]?"));
        assertTrue(tzdb.tzdbDataFile().endsWith("tzdb.dat"));
    }

    @Test
    void catalogListsFixedRules() throws Exception {
        // Act
        Object result = App.execute(new String[]{"catalog"});

        // Assert
        List<?> rules = assertInstanceOf(List.class, result);
        assertTrue(rules.size() >= 5);
    }

    @Test
    void scheduleThenListThenStatusRoundTripAcrossProcesses() throws Exception {
        // Act：schedule（新 JVM 语义：每次命令独立）
        String store = tempDir.toString();
        Object scheduled = App.execute(new String[]{"schedule", "--id", "cli-1", "--rule", "grace-two-minutes", "--store", store});

        // Assert：返回的超时 2 分钟后到期
        ScheduledTimeout timeout = assertInstanceOf(ScheduledTimeout.class, scheduled);
        assertEquals("cli-1", timeout.timeoutId());
        assertFalse(timeout.alreadyExpiredAtSchedule());

        // Act：list（只读持久化的墙钟域记录）
        Object listed = App.execute(new String[]{"list", "--store", store});
        List<?> records = assertInstanceOf(List.class, listed);
        assertEquals(1, records.size());
        PersistedTimeout persisted = assertInstanceOf(PersistedTimeout.class, records.get(0));
        assertEquals(timeout.deadlineInstant(), persisted.deadlineInstant());

        // Act：status（模拟另一个 JVM 恢复并立即查询）
        Object status = App.execute(new String[]{"status", "--id", "cli-1", "--store", store});
        com.example.monotime.domain.TimeoutStatus timeoutStatus =
                assertInstanceOf(com.example.monotime.domain.TimeoutStatus.class, status);
        assertFalse(timeoutStatus.expired());
    }

    @Test
    void scenarioCommandRunsDeterministicScriptFromFile() throws Exception {
        // Arrange
        Path scenarioFile = tempDir.resolve("scenario.json");
        Files.writeString(scenarioFile, """
                {
                  "name": "cli-scenario",
                  "description": "CLI 文件驱动验收",
                  "initialWallClock": "2026-09-25T09:00:00Z",
                  "initialMonotonicNanos": 1000000000,
                  "steps": [
                    {"op": "schedule", "timeoutId": "t1", "ruleId": "grace-two-minutes", "version": "1"},
                    {"op": "adjustWallClock", "backwardIso": "PT5H", "note": "向后校时 5 小时"},
                    {"op": "status", "timeoutId": "t1"},
                    {"op": "advance", "durationIso": "PT2M", "note": "单调流逝 2 分钟"},
                    {"op": "status", "timeoutId": "t1"}
                  ]
                }
                """);

        // Act
        Object result = App.execute(new String[]{
                "scenario", "--file", scenarioFile.toString(), "--store", tempDir.resolve("s").toString()});

        // Assert
        ScenarioReport report = assertInstanceOf(ScenarioReport.class, result);
        assertFalse(report.aborted());
        assertEquals(5, report.steps().size());
        com.example.monotime.domain.TimeoutStatus afterBackward =
                (com.example.monotime.domain.TimeoutStatus) report.steps().get(2).detail();
        com.example.monotime.domain.TimeoutStatus afterElapse =
                (com.example.monotime.domain.TimeoutStatus) report.steps().get(4).detail();
        assertEquals(java.time.Duration.ofMinutes(2).toNanos(), afterBackward.remainingNanos());
        assertTrue(afterElapse.expired());
    }

    @Test
    void unknownCommandIsRejected() {
        // Act + Assert
        assertThrows(IllegalArgumentException.class, () -> App.execute(new String[]{"frobnicate"}));
    }

    @Test
    void missingRequiredOptionIsRejected() {
        // Act + Assert
        assertThrows(IllegalArgumentException.class,
                () -> App.execute(new String[]{"schedule", "--rule", "grace-two-minutes", "--store", tempDir.toString()}));
    }

    @Test
    void malformedScenarioFileFails() throws Exception {
        // Arrange
        Path bad = tempDir.resolve("bad.json");
        Files.writeString(bad, "{ this is not json");

        // Act + Assert
        assertThrows(Exception.class, () -> App.execute(new String[]{"scenario", "--file", bad.toString()}));
    }

    @Test
    void helpReturnsCommandDocumentation() throws Exception {
        // Act
        Object result = App.execute(new String[]{"help"});

        // Assert
        Map<?, ?> help = assertInstanceOf(Map.class, result);
        assertInstanceOf(List.class, help.get("commands"));
    }

    @Test
    void requestEndpointAcceptsScenarioEnvelopeFromFile() throws Exception {
        // Arrange
        Path requestFile = tempDir.resolve("req.json");
        Files.writeString(requestFile, """
                {
                  "command": "scenario",
                  "store": "%s",
                  "scenario": {
                    "name": "json-request",
                    "description": "通用 JSON 请求",
                    "initialWallClock": "2026-09-25T09:00:00Z",
                    "initialMonotonicNanos": 0,
                    "steps": [
                      {"op": "schedule", "timeoutId": "j1", "ruleId": "grace-two-minutes", "version": "1"},
                      {"op": "adjustWallClock", "instant": "2026-09-20T00:00:00Z", "note": "直接设置墙钟到过去"},
                      {"op": "status", "timeoutId": "j1"}
                    ]
                  }
                }
                """.formatted(tempDir.resolve("reqstore")));

        // Act
        Object result = App.execute(new String[]{"request", "--file", requestFile.toString()});

        // Assert
        com.example.monotime.scenario.ScenarioReport report =
                assertInstanceOf(com.example.monotime.scenario.ScenarioReport.class, result);
        assertFalse(report.aborted());
        com.example.monotime.domain.TimeoutStatus status =
                (com.example.monotime.domain.TimeoutStatus) report.steps().get(2).detail();
        assertEquals(java.time.Duration.ofMinutes(2).toNanos(), status.remainingNanos());
    }

    @Test
    void requestEndpointRejectsMissingCommand() throws Exception {
        // Arrange
        Path requestFile = tempDir.resolve("badreq.json");
        Files.writeString(requestFile, "{\"foo\":1}");

        // Act + Assert
        assertThrows(IllegalArgumentException.class,
                () -> App.execute(new String[]{"request", "--file", requestFile.toString()}));
    }

    @Test
    void requestEndpointRejectsScenarioWithoutScenarioField() throws Exception {
        // Arrange
        Path requestFile = tempDir.resolve("noscenario.json");
        Files.writeString(requestFile, "{\"command\":\"scenario\"}");

        // Act + Assert
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class,
                () -> App.execute(new String[]{"request", "--file", requestFile.toString()}));
        assertTrue(ex.getMessage().contains("scenario"));
    }

    @Test
    void bareOptionWithoutValueIsRejectedRatherThanTreatedAsLiteral() {
        // Act + Assert：--id 后无值，不应被当成字面量 id
        assertThrows(IllegalArgumentException.class,
                () -> App.execute(new String[]{"schedule", "--id", "--rule", "grace-two-minutes"}));
    }

    @Test
    void requestEndpointSupportsCatalogAndInfo() throws Exception {
        // Arrange
        Path requestFile = tempDir.resolve("info.json");
        Files.writeString(requestFile, "{\"command\":\"info\"}");

        // Act + Assert
        Object result = App.execute(new String[]{"request", "--file", requestFile.toString()});
        assertInstanceOf(Map.class, result);
    }
}
