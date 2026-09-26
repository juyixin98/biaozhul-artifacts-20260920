package com.example.monotime.scenario;

import com.example.monotime.SimulatedClock;
import com.example.monotime.data.RuleCatalog;
import com.example.monotime.domain.DeadlineCalculator;
import com.example.monotime.domain.MonotonicConverter;
import com.example.monotime.domain.ScheduledTimeout;
import com.example.monotime.domain.TimeRule;
import com.example.monotime.domain.TimeoutStatus;
import com.example.monotime.persistence.JsonTimeoutStore;
import com.example.monotime.persistence.PersistedTimeout;
import com.example.monotime.tzdb.TzdbInfo;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 确定性执行场景脚本：用一个 {@link SimulatedClock} 同时驱动墙钟与单调时钟，
 * 用真实的 {@link JsonTimeoutStore} 落盘以验证“持久化恢复”。
 *
 * <p>每一步记录操作前后的双时钟快照，使验收点可直接从报告读出：
 * 校时只改墙钟、流逝只改单调刻度、重启后单调刻度按新纪元重算。</p>
 */
public final class ScenarioRunner {

    private final RuleCatalog catalog;
    private final JsonTimeoutStore store;
    private final TzdbInfo tzdb;

    private SimulatedClock clock;
    private MonotonicConverter converter;
    private final Map<String, ScheduledTimeout> live = new HashMap<>();

    public ScenarioRunner(RuleCatalog catalog, JsonTimeoutStore store) {
        this.catalog = catalog;
        this.store = store;
        this.tzdb = TzdbInfo.detect();
    }

    public ScenarioReport run(ScenarioDefinition def) throws IOException {
        resetOwnedRecords(def);
        long startTick = def.initialMonotonicNanos() == null ? 0L : def.initialMonotonicNanos();
        this.clock = new SimulatedClock(def.initialWallClock(), startTick);
        this.converter = new MonotonicConverter(clock, new DeadlineCalculator(), tzdb.tzdbVersion());
        this.live.clear();

        List<StepReport> reports = new ArrayList<>();
        boolean aborted = false;
        for (int i = 0; i < def.steps().size(); i++) {
            ScenarioStep step = def.steps().get(i);
            StepReport report;
            try {
                report = execute(i, step);
            } catch (RuntimeException e) {
                report = snapshot(i, step, null, e.getMessage());
                reports.add(report);
                aborted = true;
                break;
            }
            reports.add(report);
        }
        return new ScenarioReport(def.name(), def.description(), tzdb, List.copyOf(reports), aborted);
    }

    /**
     * 仅删除“本场景脚本自己涉及”的超时记录（schedule/delete/status 引用到的 id），
     * 绝不清空整个目录，从而保证场景可重复执行，又不会破坏同一存储中不相关的记录。
     */
    private void resetOwnedRecords(ScenarioDefinition def) throws IOException {
        Files.createDirectories(store.directory());
        def.steps().stream()
                .map(ScenarioStep::timeoutId)
                .filter(id -> id != null && !id.isBlank())
                .distinct()
                .forEach(id -> {
                    try {
                        store.delete(id);
                    } catch (IOException e) {
                        throw new UncheckedIOException("清理场景历史记录失败: " + id, e);
                    }
                });
    }

    private StepReport execute(int index, ScenarioStep step) throws IOException {
        Instant wallBefore = clock.wallClockInstant();
        long tickBefore = clock.monotonicNanos();
        Object detail = switch (step.op() == null ? "" : step.op()) {
            case "schedule" -> doSchedule(step);
            case "status" -> doStatus(step);
            case "delete" -> doDelete(step);
            case "advance" -> doAdvance(step);
            case "adjustWallClock" -> doAdjust(step);
            case "restart" -> doRestart(step);
            default -> throw new IllegalArgumentException("未知 op: " + step.op());
        };
        return new StepReport(
                index, step.op(), step.note(),
                wallBefore, clock.wallClockInstant(),
                tickBefore, clock.monotonicNanos(),
                detail, null);
    }

    private ScheduledTimeout doSchedule(ScenarioStep step) throws IOException {
        TimeRule rule = resolveRule(step);
        ScheduledTimeout scheduled = converter.schedule(step.timeoutId(), rule);
        live.put(scheduled.timeoutId(), scheduled);
        store.save(scheduled);
        return scheduled;
    }

    private TimeoutStatus doStatus(ScenarioStep step) {
        ScheduledTimeout scheduled = requireLive(step.timeoutId());
        return converter.statusOf(scheduled);
    }

    private String doDelete(ScenarioStep step) throws IOException {
        requireLive(step.timeoutId());
        live.remove(step.timeoutId());
        store.delete(step.timeoutId());
        return "deleted";
    }

    private String doAdvance(ScenarioStep step) {
        Duration elapsed = parseDuration(step);
        clock.advanceMonotonic(elapsed);
        return "单调时钟推进 " + elapsed;
    }

    private String doAdjust(ScenarioStep step) {
        if (step.instant() != null) {
            clock.setWallClock(step.instant());
            return "墙钟设置为 " + step.instant();
        }
        if (step.forwardIso() != null) {
            Duration d = Duration.parse(step.forwardIso());
            clock.jumpWallClockForward(d);
            return "墙钟向前校时 " + d;
        }
        if (step.backwardIso() != null) {
            Duration d = Duration.parse(step.backwardIso());
            clock.jumpWallClockBackward(d);
            return "墙钟向后校时 " + d;
        }
        throw new IllegalArgumentException("adjustWallClock 需要 forwardIso/backwardIso/instant 之一");
    }

    /**
     * 模拟重启：丢弃全部内存状态；nanoTime 纪元重置为新读数（默认 0）；
     * 从持久化读回墙钟截止瞬间，用新时钟重新锚定单调死线。
     */
    private List<ScheduledTimeout> doRestart(ScenarioStep step) throws IOException {
        long newTick = step.newMonotonicNanos() == null ? 0L : step.newMonotonicNanos();
        Instant newWall = step.newWallClockInstant() == null ? clock.wallClockInstant() : step.newWallClockInstant();
        clock.setMonotonicNanos(newTick);
        clock.setWallClock(newWall);
        live.clear();

        List<PersistedTimeout> persisted = store.findAll();
        List<ScheduledTimeout> recovered = new ArrayList<>();
        for (PersistedTimeout p : persisted) {
            ScheduledTimeout r = converter.recoverAfterRestart(
                    new ScheduledTimeout(
                            p.timeoutId(), p.ruleId(), p.ruleVersion(),
                            p.scheduledAtInstant(), p.deadlineInstant(),
                            0L, 0L, 0L, false, false, p.scheduledTzdbVersion()),
                    newTick, newWall);
            live.put(r.timeoutId(), r);
            recovered.add(r);
        }
        return recovered;
    }

    private Duration parseDuration(ScenarioStep step) {
        if (step.durationIso() != null) {
            return Duration.parse(step.durationIso());
        }
        if (step.nanos() != null) {
            return Duration.ofNanos(step.nanos());
        }
        throw new IllegalArgumentException("advance 需要 durationIso 或 nanos");
    }

    private TimeRule resolveRule(ScenarioStep step) {
        Optional<TimeRule> rule = step.version() == null
                ? catalog.findLatest(step.ruleId())
                : catalog.find(step.ruleId(), step.version());
        return rule.orElseThrow(() -> new IllegalArgumentException(
                "规则不存在: ruleId=" + step.ruleId() + ", version=" + step.version()));
    }

    private ScheduledTimeout requireLive(String timeoutId) {
        ScheduledTimeout scheduled = live.get(timeoutId);
        if (scheduled == null) {
            throw new IllegalArgumentException("活动超时不存在（可能尚未安排或重启后未恢复）: " + timeoutId);
        }
        return scheduled;
    }

    private StepReport snapshot(int index, ScenarioStep step, Object detail, String error) {
        return new StepReport(
                index, step.op(), step.note(),
                clock.wallClockInstant(), clock.wallClockInstant(),
                clock.monotonicNanos(), clock.monotonicNanos(),
                detail, error);
    }
}
