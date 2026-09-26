package com.example.timeout.core;

import com.example.timeout.clock.TimeoutClock;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * 超时注册表：本项目的核心——墙钟截止时间 → 单调计时器的转换服务。
 *
 * <p>转换只在两个时刻发生：
 * <ol>
 *   <li>{@link #schedule}：新安排时，{@code remaining = deadlineWall - clock.wall()}，
 *       {@code fireMono = clock.monoNanos() + remaining}；</li>
 *   <li>{@link #recover}：进程重启后，用当前新时钟重新执行同样的换算。</li>
 * </ol>
 *
 * <p>转换完成后，触发判断与剩余时长只看单调读数；之后墙钟如何被调整都与已安排的超时无关。
 * {@link #status} 直接按当前单调读数派生结果——即使尚未调用 {@link #pollExpired} 收集事件，
 * “单调时刻是否已到”这一事实也立即可见。
 */
public final class TimeoutRegistry {

    private final TimeoutClock clock;
    private final Map<String, TimeoutEntry> entries = new LinkedHashMap<>();

    private TimeoutRegistry(TimeoutClock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    public static TimeoutRegistry create(TimeoutClock clock) {
        return new TimeoutRegistry(clock);
    }

    /** 重启入口：从持久化的墙钟截止时间逐条重新换算单调触发点。 */
    public static TimeoutRegistry recover(TimeoutClock clock, List<TimeoutEntry> persisted) {
        TimeoutRegistry registry = new TimeoutRegistry(clock);
        for (TimeoutEntry stored : persisted) {
            TimeoutEntry recomputed = registry.convert(stored.id(), stored.label(),
                    stored.deadlineWall(), stored.ruleSource());
            if (stored.status() == TimeoutStatus.EXPIRED) {
                // 上一个进程已确认过期：沿用过期结论，触发墙钟记为截止时刻本身
                registry.entries.put(stored.id(),
                        recomputed.toExpired(stored.deadlineWall(),
                                Math.max(clock.monoNanos(), recomputed.fireMonoNanos())));
            } else {
                // 仅以“待触发”形态放回；若停机期间已到点，status() 会立刻派生为 EXPIRED，
                // 由首次 pollExpired() 在本进程投递过期事件。
                registry.entries.put(stored.id(), recomputed);
            }
        }
        return registry;
    }

    /**
     * 安排一个新超时。
     * 截止时间已在过去时：{@code fireMonoNanos <= nowMono}，
     * {@link #get}/{@link #list} 立即以过期视图呈现，{@code remainingAtConversionNanos} 保留负值
     * （“迟到多久”）；过期事件仍由首次 {@link #pollExpired} 投递一次。
     */
    public TimeoutEntry schedule(String id, Instant deadlineWall, String label) {
        return schedule(id, deadlineWall, label, null);
    }

    public TimeoutEntry schedule(String id, Instant deadlineWall, String label, String ruleSource) {
        Objects.requireNonNull(id, "id");
        Objects.requireNonNull(deadlineWall, "deadlineWall");
        if (entries.containsKey(id)) {
            throw new IllegalArgumentException("duplicate timeout id: " + id);
        }
        TimeoutEntry entry = convert(id, label, deadlineWall, ruleSource);
        entries.put(id, entry);
        return entry;
    }

    private TimeoutEntry convert(String id, String label, Instant deadlineWall, String ruleSource) {
        Instant nowWall = clock.wall();
        long nowMono = clock.monoNanos();
        // 唯一允许的“跨时钟”运算：在转换瞬间用墙钟差求时长，立刻锚定到单调读数上。
        long remainingNanos = Duration.between(nowWall, deadlineWall).toNanos();
        long fireMono = Math.addExact(nowMono, remainingNanos);
        // 始终以 SCHEDULED 入册；是否到期由单调读数动态决定，过期事件由 pollExpired 投递一次。
        return new TimeoutEntry(id, label, deadlineWall, nowWall, nowMono,
                remainingNanos, fireMono, TimeoutStatus.SCHEDULED, null, null, ruleSource);
    }

    /** 当前状态：已确认过期，或单调截止时刻已到（含安排时/重启重算时已过期）。 */
    public TimeoutStatus status(String id) {
        return effectiveStatus(require(id));
    }

    private TimeoutStatus effectiveStatus(TimeoutEntry e) {
        if (e.status() == TimeoutStatus.EXPIRED) {
            return TimeoutStatus.EXPIRED;
        }
        return clock.monoNanos() >= e.fireMonoNanos() ? TimeoutStatus.EXPIRED : TimeoutStatus.SCHEDULED;
    }

    /**
     * 只读视图：到期但尚未被 {@link #pollExpired()} 确认的条目，也立即呈现为 EXPIRED，
     * 但不改动存储态、不消费过期事件。视图里的 firedAt* 是“此刻观察到”的读数。
     */
    private TimeoutEntry viewOf(TimeoutEntry e) {
        if (e.status() == TimeoutStatus.SCHEDULED && clock.monoNanos() >= e.fireMonoNanos()) {
            return e.toExpired(clock.wall(), clock.monoNanos());
        }
        return e;
    }

    /** 距触发的剩余时长，只由单调读数推算；已过期返回 0。 */
    public Duration remainingMonotonic(String id) {
        TimeoutEntry e = require(id);
        if (effectiveStatus(e) == TimeoutStatus.EXPIRED) {
            return Duration.ZERO;
        }
        return Duration.ofNanos(e.fireMonoNanos() - clock.monoNanos());
    }

    public TimeoutEntry get(String id) {
        return viewOf(require(id));
    }

    /**
     * 推进判断：把所有单调截止时刻已到的待触发条目标记为过期，并返回本次新过期的条目。
     * 判据是 {@code clock.monoNanos() >= fireMonoNanos}，全程不读墙钟差值；
     * 墙钟只用于记录“过期瞬间日历上显示几点”，不参与判定。
     */
    public List<TimeoutEntry> pollExpired() {
        List<TimeoutEntry> justExpired = new ArrayList<>();
        long nowMono = clock.monoNanos();
        Instant nowWall = clock.wall();
        for (Map.Entry<String, TimeoutEntry> mapEntry : entries.entrySet()) {
            TimeoutEntry e = mapEntry.getValue();
            if (e.status() == TimeoutStatus.SCHEDULED && nowMono >= e.fireMonoNanos()) {
                TimeoutEntry expired = e.toExpired(nowWall, nowMono);
                mapEntry.setValue(expired);
                justExpired.add(expired);
            }
        }
        return List.copyOf(justExpired);
    }

    public List<TimeoutEntry> list() {
        return entries.values().stream().map(this::viewOf).toList();
    }

    /** 供持久化使用的不可变快照；持久化层只应写出墙钟字段。 */
    public RegistrySnapshot snapshot() {
        return new RegistrySnapshot(List.copyOf(entries.values()));
    }

    private TimeoutEntry require(String id) {
        TimeoutEntry e = entries.get(id);
        if (e == null) {
            throw new IllegalArgumentException("unknown timeout id: " + id);
        }
        return e;
    }
}
