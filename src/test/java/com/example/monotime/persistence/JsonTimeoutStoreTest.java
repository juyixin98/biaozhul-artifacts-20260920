package com.example.monotime.persistence;

import com.example.monotime.api.JsonMappers;
import com.example.monotime.domain.ScheduledTimeout;
import com.example.monotime.tzdb.TzdbInfo;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 持久化关键约束测试：落盘内容只有墙钟域字段，单调刻度在结构上不可能被保存。
 */
class JsonTimeoutStoreTest {

    private final ObjectMapper mapper = JsonMappers.create();
    private final String tzdbVersion = TzdbInfo.detect().tzdbVersion();

    @TempDir
    Path tempDir;

    @Test
    void persistsOnlyWallClockFieldsNeverMonotonicTicks() throws Exception {
        // Arrange
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);
        store.initialize();
        ScheduledTimeout timeout = sampleTimeout("t1");

        // Act
        store.save(timeout);
        Path file = tempDir.resolve("t1.json");
        JsonNode json = mapper.readTree(Files.readString(file));

        // Assert：墙钟域与审计字段在
        assertEquals("t1", json.path("timeoutId").asText());
        assertEquals("2026-09-25T09:00:00Z", json.path("scheduledAtInstant").asText());
        assertEquals("2026-09-25T09:10:00Z", json.path("deadlineInstant").asText());
        assertEquals(tzdbVersion, json.path("scheduledTzdbVersion").asText());
        // 任何单调刻度字段都不应出现
        assertFalse(json.has("monotonicDeadlineTickNanos"), "单调死线刻度绝不能被持久化");
        assertFalse(json.has("scheduledAtTickNanos"), "单调起始刻度绝不能被持久化");
        assertFalse(json.has("durationAtScheduleNanos"), "转换产物（时长）不应跨重启复用");
        assertFalse(json.has("alreadyExpiredAtSchedule"));
        assertFalse(json.has("recoveredAfterRestart"));
    }

    @Test
    void readsBackPersistedRecord() throws Exception {
        // Arrange
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);
        store.save(sampleTimeout("t2"));

        // Act
        Optional<PersistedTimeout> loaded = store.find("t2");

        // Assert
        assertTrue(loaded.isPresent());
        assertEquals(Instant.parse("2026-09-25T09:10:00Z"), loaded.get().deadlineInstant());
    }

    @Test
    void findAllReturnsSortedRecords() throws Exception {
        // Arrange
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);
        store.save(sampleTimeout("b"));
        store.save(sampleTimeout("a"));
        store.save(sampleTimeout("c"));

        // Act
        List<PersistedTimeout> all = store.findAll();

        // Assert
        assertEquals(List.of("a", "b", "c"), all.stream().map(PersistedTimeout::timeoutId).toList());
    }

    @Test
    void deleteRemovesRecord() throws Exception {
        // Arrange
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);
        store.save(sampleTimeout("gone"));

        // Act
        store.delete("gone");

        // Assert
        assertTrue(store.find("gone").isEmpty());
    }

    @Test
    void rejectsUnsafeTimeoutIdsToPreventPathTraversalAndCollision() {
        // Act + Assert：分隔符、点号段、空、超长 id 全部在边界被拒绝
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId("../etc/passwd"));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId("a/b"));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId("a\\b"));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId(""));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId(null));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId(".."));
        assertThrows(IllegalArgumentException.class, () -> JsonTimeoutStore.validateId("a".repeat(129)));
        // 安全 id 原样返回，保证不同 id 不会映射到同一文件
        assertEquals("a_b-1.2", JsonTimeoutStore.validateId("a_b-1.2"));
    }

    @Test
    void distinctIdsNeverCollideToSameFile() throws Exception {
        // Arrange：下划线是合法字符；若旧实现把斜杠替换成下划线就会覆盖
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);

        // Act + Assert：含斜杠的 id 被拒绝而不是静默落到 a_b.json
        assertThrows(IllegalArgumentException.class, () ->
                store.save(sampleTimeout("a/b")));
        store.save(sampleTimeout("a_b"));
        assertEquals(1, store.findAll().size());
    }

    @Test
    void oneCorruptFileDoesNotBreakRecoveryOfOthers() throws Exception {
        // Arrange：一条正常记录 + 一个损坏的 json
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);
        store.save(sampleTimeout("good"));
        Files.writeString(tempDir.resolve("broken.json"), "{ 不是合法 JSON ");

        // Act
        List<PersistedTimeout> all = store.findAll();

        // Assert：好记录仍可恢复，坏文件被跳过
        assertEquals(List.of("good"), all.stream().map(PersistedTimeout::timeoutId).toList());
    }

    @Test
    void saveIsAtomicAndLeavesNoTempFiles() throws Exception {
        // Arrange
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir, mapper);

        // Act
        store.save(sampleTimeout("atomic"));

        // Assert：正式文件在，无残留临时文件
        assertTrue(Files.exists(tempDir.resolve("atomic.json")));
        try (var list = Files.list(tempDir)) {
            assertTrue(list.noneMatch(p -> p.getFileName().toString().startsWith(".")));
        }
    }

    private ScheduledTimeout sampleTimeout(String id) {
        return new ScheduledTimeout(
                id, "rule-x", "3",
                Instant.parse("2026-09-25T09:00:00Z"),
                Instant.parse("2026-09-25T09:10:00Z"),
                600_000_000_000L,
                111L,
                600_000_000_111L,
                false, false,
                tzdbVersion);
    }
}
