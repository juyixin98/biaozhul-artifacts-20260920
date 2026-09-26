package com.example.timeout.service;

import com.example.timeout.clock.SystemTimeoutClock;
import com.example.timeout.clock.TimeoutClock;
import com.example.timeout.clock.VirtualClock;
import com.example.timeout.core.RegistrySnapshot;
import com.example.timeout.core.TimeoutEntry;
import com.example.timeout.core.TimeoutRegistry;
import com.example.timeout.core.TimeoutStatus;
import com.example.timeout.rule.DeadlineRule;
import com.example.timeout.rule.DeadlineRuleEngine;
import com.example.timeout.store.JsonTimeoutStore;
import com.example.timeout.store.TimeoutStore;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Objects;

/**
 * 应用服务：编排“规则 → 墙钟截止时间 → 单调计时器”的完整流程，并负责持久化。
 *
 * <p>支持两种运行模式：
 * <ul>
 *   <li>virtual：虚拟时钟（默认，用于演示与固定测试数据），可通过接口向前/向后校时、推进时间；</li>
 *   <li>system：真实系统时钟（墙钟 UTC + {@code nanoTime}）。</li>
 * </ul>
 */
public final class TimeoutService {

    private final TimeoutClock clock;
    private final TimeoutStore store;
    private final DeadlineRuleEngine ruleEngine;
    private final TimeoutRegistry registry;
    private final String mode;

    private TimeoutService(TimeoutClock clock, TimeoutStore store, String mode) {
        this.clock = clock;
        this.store = store;
        this.mode = mode;
        this.ruleEngine = new DeadlineRuleEngine();
        this.registry = TimeoutRegistry.recover(clock, store.load());
    }

    /** 虚拟时钟模式：从给定墙钟瞬时起步，单调读数归零（如同一次全新启动）。 */
    public static TimeoutService virtual(Instant startWall, Path storeFile) {
        var clock = new VirtualClock(Objects.requireNonNull(startWall, "startWall"));
        return new TimeoutService(clock, new JsonTimeoutStore(storeFile), "virtual");
    }

    /** 真实时钟模式：启动时必须用当前时钟重新换算持久化的墙钟截止时间。 */
    public static TimeoutService system(Path storeFile) {
        var clock = new SystemTimeoutClock();
        return new TimeoutService(clock, new JsonTimeoutStore(storeFile), "system");
    }

    public TimeoutEntry scheduleFromRule(String id, String label, DeadlineRule rule) {
        Objects.requireNonNull(id, "id");
        Objects.requireNonNull(rule, "rule");
        Instant deadline = ruleEngine.resolve(rule, clock);
        registry.schedule(id, deadline, label, rule.getClass().getSimpleName());
        // persist() 是确认点；即使截止时间已在过去，返回的只读视图也立即呈现为 EXPIRED。
        persist();
        return registry.get(id);
    }

    public TimeoutEntry scheduleAbsolute(String id, String label, Instant deadlineWall) {
        Objects.requireNonNull(deadlineWall, "deadlineWall");
        registry.schedule(id, deadlineWall, label, "absolute");
        persist();
        return registry.get(id);
    }

    public List<TimeoutEntry> list() {
        return registry.list();
    }

    public TimeoutEntry get(String id) {
        return registry.get(id);
    }

    public Duration remainingMonotonic(String id) {
        return registry.remainingMonotonic(id);
    }

    /** 收集并落盘新过期的条目。 */
    public List<TimeoutEntry> pollExpired() {
        List<TimeoutEntry> expired = registry.pollExpired();
        if (!expired.isEmpty()) {
            persist();
        }
        return expired;
    }

    // —— 仅虚拟模式可用的时钟控制（演示向前/向后校时）——

    public void tick(Duration elapsed) {
        requireVirtual("tick");
        ((VirtualClock) clock).tick(elapsed);
        pollExpired();
    }

    public void setWall(Instant newWall) {
        requireVirtual("setWall");
        ((VirtualClock) clock).setWall(newWall);
        pollExpired();
    }

    public void advanceWall(Duration delta) {
        requireVirtual("advanceWall");
        ((VirtualClock) clock).advanceWall(delta);
        pollExpired();
    }

    public Instant wallNow() {
        return clock.wall();
    }

    public long monoNow() {
        return clock.monoNanos();
    }

    public String mode() {
        return mode;
    }

    public RegistrySnapshot snapshot() {
        return registry.snapshot();
    }

    public void persist() {
        // 落盘是显式确认点：先把单调时刻已到的条目确认为 EXPIRED，再只持久化墙钟字段。
        registry.pollExpired();
        store.persist(registry.snapshot());
    }

    /** 供启动时灌入固定测试数据。 */
    public void seedIfEmpty(List<SeedSpec> seeds) {
        if (!registry.list().isEmpty()) {
            return;
        }
        for (SeedSpec seed : seeds) {
            scheduleFromRule(seed.id(), seed.label(), seed.rule());
        }
    }

    private void requireVirtual(String op) {
        if (!(clock instanceof VirtualClock)) {
            throw new IllegalStateException("clock operation '" + op
                    + "' is only allowed in virtual mode; the system clock cannot be steered by the API");
        }
    }
}
