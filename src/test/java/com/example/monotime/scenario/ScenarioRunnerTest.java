package com.example.monotime.scenario;

import com.example.monotime.api.JsonMappers;
import com.example.monotime.data.RuleCatalog;
import com.example.monotime.domain.ScheduledTimeout;
import com.example.monotime.domain.TimeoutStatus;
import com.example.monotime.persistence.JsonTimeoutStore;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 端到端验收：用确定性脚本覆盖
 * 向前/向后校时、单调流逝、已过期截止、重启后重新计算（含跨校时重启）。
 */
class ScenarioRunnerTest {

    private static final Instant T0 = Instant.parse("2026-09-25T09:00:00Z");
    private static final long TICK0 = 1_000_000_000L;
    private static final long TWO_MIN_NANOS = Duration.ofMinutes(2).toNanos();

    private final ObjectMapper mapper = JsonMappers.create();

    @TempDir
    Path tempDir;

    @Test
    void wallAdjustmentsDoNotChangeTimeoutButMonotonicElapseDoes() throws Exception {
        // Arrange
        ScenarioDefinition definition = new ScenarioDefinition(
                "clock-adjust", "校时不影响已安排超时",
                T0, TICK0,
                List.of(
                        schedule("t1", "安排 2 分钟宽限"),
                        status("t1", "初始剩余 120s"),
                        adjustBackward("PT3H", "墙钟向后拨 3 小时"),
                        status("t1", null),
                        adjustForward("P2D", "墙钟向前跳 2 天"),
                        status("t1", null),
                        advance("PT90S", "单调流逝 90s"),
                        status("t1", "剩余应为 30s")));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertFalse(report.aborted());
        TimeoutStatus initial = (TimeoutStatus) report.steps().get(1).detail();
        TimeoutStatus afterBackward = (TimeoutStatus) report.steps().get(3).detail();
        TimeoutStatus afterForward = (TimeoutStatus) report.steps().get(5).detail();
        TimeoutStatus afterElapse = (TimeoutStatus) report.steps().get(7).detail();

        assertEquals(TWO_MIN_NANOS, initial.remainingNanos());
        assertEquals(TWO_MIN_NANOS, afterBackward.remainingNanos(), "向后校时不得推迟超时");
        assertEquals(TWO_MIN_NANOS, afterForward.remainingNanos(), "向前校时不得提前超时");
        assertEquals(Duration.ofSeconds(30).toNanos(), afterElapse.remainingNanos(), "只有单调流逝才消耗剩余时间");
        assertFalse(afterElapse.expired());
    }

    @Test
    void restartRecomputesMonotonicDeadlineFromPersistedWallInstant() throws Exception {
        // Arrange：安排后流逝 90s 再重启；重启纪元与墙钟显式给出
        ScenarioDefinition definition = new ScenarioDefinition(
                "restart", "重启后重新计算",
                T0, TICK0,
                List.of(
                        schedule("t1", null),
                        advance("PT90S", "崩溃前流逝 90s"),
                        restart(8_000_000_000L, T0.plus(Duration.ofSeconds(90)), "JVM 重启，nanoTime 纪元重置"),
                        status("t1", "恢复后剩余 30s"),
                        advance("PT30S", "再流逝 30s"),
                        status("t1", "应当过期")));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertFalse(report.aborted());
        @SuppressWarnings("unchecked")
        List<ScheduledTimeout> recovered = (List<ScheduledTimeout>) report.steps().get(2).detail();
        TimeoutStatus rightAfterRecovery = (TimeoutStatus) report.steps().get(3).detail();
        TimeoutStatus finalStatus = (TimeoutStatus) report.steps().get(5).detail();

        assertEquals(1, recovered.size());
        assertTrue(recovered.get(0).recoveredAfterRestart());
        // 墙钟截止瞬间仍是安排时算出的 T0+2m
        assertEquals(T0.plus(Duration.ofMinutes(2)), recovered.get(0).deadlineInstant());
        // 单调死线按新纪元 8_000_000_000 重算：+30s
        assertEquals(8_000_000_000L + Duration.ofSeconds(30).toNanos(),
                recovered.get(0).monotonicDeadlineTickNanos());
        assertEquals(Duration.ofSeconds(30).toNanos(), rightAfterRecovery.remainingNanos());
        assertTrue(finalStatus.expired());
    }

    @Test
    void restartAfterWallDeadlinePassedYieldsAlreadyExpired() throws Exception {
        // Arrange：重启时墙钟已超过截止瞬间 15s
        ScenarioDefinition definition = new ScenarioDefinition(
                "restart-late", "重启时截止已过",
                T0, TICK0,
                List.of(
                        schedule("t1", null),
                        restart(42L, T0.plus(Duration.ofSeconds(135)), "停机很久后才重启，墙钟已过截止 15s"),
                        status("t1", null)));

        // Act
        ScenarioReport report = run(definition);

        // Assert：立即判定过期，剩余为负
        assertFalse(report.aborted());
        @SuppressWarnings("unchecked")
        List<ScheduledTimeout> recovered = (List<ScheduledTimeout>) report.steps().get(1).detail();
        TimeoutStatus status = (TimeoutStatus) report.steps().get(2).detail();
        assertTrue(recovered.get(0).alreadyExpiredAtSchedule());
        assertTrue(status.expired());
        assertTrue(status.remainingNanos() < 0);
    }

    @Test
    void alreadyExpiredAbsoluteDeadlineIsDetectedAtSchedule() throws Exception {
        // Arrange：固定数据 brief-absolute v1 截止 2026-09-25T10:05:00Z；在该时刻之后安排
        ScenarioDefinition definition = new ScenarioDefinition(
                "past-deadline", "安排时已过期",
                Instant.parse("2026-09-25T11:00:00Z"), 0L,
                List.of(
                        new ScenarioStep("schedule", "late", "brief-absolute", "1",
                                null, null, null, null, null, null, null, "截止 10:05Z，当前 11:00Z"),
                        status("late", null)));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertFalse(report.aborted());
        ScheduledTimeout scheduled = (ScheduledTimeout) report.steps().get(0).detail();
        TimeoutStatus status = (TimeoutStatus) report.steps().get(1).detail();
        assertTrue(scheduled.alreadyExpiredAtSchedule());
        assertTrue(status.expired());
    }

    @Test
    void unknownRuleAbortsScenarioWithError() throws Exception {
        // Arrange
        ScenarioDefinition definition = new ScenarioDefinition(
                "bad-rule", "不存在的规则", T0, 0L,
                List.of(new ScenarioStep("schedule", "x", "missing-rule", null,
                        null, null, null, null, null, null, null, null)));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertTrue(report.aborted());
        assertTrue(report.steps().get(0).error().contains("规则不存在"));
    }

    @Test
    void deleteRemovesTimeoutFromLiveAndStore() throws Exception {
        // Arrange
        ScenarioDefinition definition = new ScenarioDefinition(
                "delete", "删除超时", T0, TICK0,
                List.of(
                        schedule("t1", null),
                        new ScenarioStep("delete", "t1", null, null,
                                null, null, null, null, null, null, null, null),
                        // 重启后持久化中也应没有该记录
                        restart(0L, null, "重启")));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertFalse(report.aborted());
        @SuppressWarnings("unchecked")
        List<ScheduledTimeout> recovered = (List<ScheduledTimeout>) report.steps().get(2).detail();
        assertTrue(recovered.isEmpty(), "已删除的超时不应在重启后恢复");
    }

    @Test
    void restartWithoutExplicitEpochDefaultsTickToZeroAndKeepsWallClock() throws Exception {
        // Arrange：重启步骤不提供 newMonotonicNanos / newWallClockInstant
        ScenarioDefinition definition = new ScenarioDefinition(
                "restart-defaults", "默认重启参数", T0, TICK0,
                List.of(
                        schedule("t1", null),
                        new ScenarioStep("advance", null, null, null,
                                null, "PT30S", null, null, null, null, null, null),
                        new ScenarioStep("restart", null, null, null,
                                null, null, null, null, null, null, null, null),
                        status("t1", "新纪元从 0 起，墙钟停在崩溃瞬间")));

        // Act
        ScenarioReport report = run(definition);

        // Assert
        assertFalse(report.aborted());
        @SuppressWarnings("unchecked")
        List<ScheduledTimeout> recovered = (List<ScheduledTimeout>) report.steps().get(2).detail();
        TimeoutStatus status = (TimeoutStatus) report.steps().get(3).detail();
        // 墙钟截止 T0+2m，恢复时墙钟 = T0+30s（advance 只推单调，但场景里墙钟未变）→ 剩余 2m
        assertEquals(0L, recovered.get(0).scheduledAtTickNanos(), "默认新纪元刻度为 0");
        assertEquals(TWO_MIN_NANOS, status.remainingNanos());
    }

    @Test
    void runDoesNotDeleteUnrelatedRecordsInSharedStore() throws Exception {
        // Arrange：场景使用的目录里预先存在一条与脚本无关的持久化记录
        JsonTimeoutStore shared = new JsonTimeoutStore(tempDir.resolve("shared"), mapper);
        ScheduledTimeout unrelated = new ScheduledTimeout(
                "unrelated", "grace-two-minutes", "1",
                T0, T0.plus(Duration.ofMinutes(2)),
                0L, 0L, 0L, false, false, "2026b");
        shared.save(unrelated);

        ScenarioDefinition definition = new ScenarioDefinition(
                "shared-store", "共享存储不被清空", T0, TICK0,
                List.of(schedule("t1", null), status("t1", null)));

        // Act
        ScenarioReport report = new ScenarioRunner(new RuleCatalog(), shared).run(definition);

        // Assert：场景成功，且无关记录仍在
        assertFalse(report.aborted());
        assertTrue(shared.find("unrelated").isPresent(), "无关记录不得被场景运行删除");
        assertTrue(shared.find("t1").isPresent());
    }

    private ScenarioReport run(ScenarioDefinition definition) throws Exception {
        JsonTimeoutStore store = new JsonTimeoutStore(tempDir.resolve("store"), mapper);
        return new ScenarioRunner(new RuleCatalog(), store).run(definition);
    }

    private static ScenarioStep schedule(String timeoutId, String note) {
        return new ScenarioStep("schedule", timeoutId, "grace-two-minutes", "1",
                null, null, null, null, null, null, null, note);
    }

    private static ScenarioStep status(String timeoutId, String note) {
        return new ScenarioStep("status", timeoutId, null, null,
                null, null, null, null, null, null, null, note);
    }

    private static ScenarioStep advance(String iso, String note) {
        return new ScenarioStep("advance", null, null, null,
                null, iso, null, null, null, null, null, note);
    }

    private static ScenarioStep adjustBackward(String iso, String note) {
        return new ScenarioStep("adjustWallClock", null, null, null,
                null, null, null, iso, null, null, null, note);
    }

    private static ScenarioStep adjustForward(String iso, String note) {
        return new ScenarioStep("adjustWallClock", null, null, null,
                null, null, iso, null, null, null, null, note);
    }

    private static ScenarioStep restart(long newTick, Instant newWall, String note) {
        return new ScenarioStep("restart", null, null, null,
                null, null, null, null, null, newTick, newWall, note);
    }
}
