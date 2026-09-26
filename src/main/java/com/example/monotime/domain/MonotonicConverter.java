package com.example.monotime.domain;

import com.example.monotime.ClockPort;
import com.example.monotime.SaturatingMath;
import com.example.monotime.tzdb.TzdbInfo;

import java.time.Duration;
import java.time.Instant;

/**
 * 墙钟截止时间 → 单调计时器的转换服务。
 *
 * <p>三条铁律：</p>
 * <ol>
 *   <li><b>只在安排（或重启恢复）瞬间做一次转换</b>：
 *       {@code duration = deadlineWall - nowWall}（两个墙钟瞬间之差），
 *       {@code monotonicDeadlineTick = nowTick + duration}。</li>
 *   <li><b>运行期间只认单调刻度</b>：剩余时间/到期判断只用 {@code System.nanoTime()} 风格刻度，
 *       因此墙钟向前或向后调整都不会改变已安排的超时。</li>
 *   <li><b>重启后必须重新计算</b>：单调刻度纪元随 JVM 死亡而失效，持久化只保存墙钟截止瞬间；
 *       恢复时用新时钟重新执行一次转换（见 {@link #recoverAfterRestart}）。</li>
 * </ol>
 */
public final class MonotonicConverter {

    private final ClockPort clock;
    private final DeadlineCalculator deadlineCalculator;
    private final String tzdbVersion;

    public MonotonicConverter(ClockPort clock, DeadlineCalculator deadlineCalculator, String tzdbVersion) {
        this.clock = clock;
        this.deadlineCalculator = deadlineCalculator;
        this.tzdbVersion = tzdbVersion;
    }

    public static MonotonicConverter create(ClockPort clock) {
        return new MonotonicConverter(clock, new DeadlineCalculator(), TzdbInfo.detect().tzdbVersion());
    }

    /** 在当前时刻按规则安排一个超时，完成墙钟→单调转换。 */
    public ScheduledTimeout schedule(String timeoutId, TimeRule rule) {
        Instant nowWall = clock.wallClockInstant();
        long nowTick = clock.monotonicNanos();
        Instant deadline = deadlineCalculator.deadline(rule, nowWall);
        return build(timeoutId, rule.ruleId(), rule.version(), nowWall, nowTick, deadline, false);
    }

    /**
     * 重启恢复：持久化记录里的单调刻度已失效（nanoTime 纪元重置），
     * 只复用墙钟截止瞬间，用新时钟重新计算时长与单调死线刻度。
     *
     * <p>若重启时墙钟曾被校时，重算结果以新墙钟为准——这正是“重启后必须重新计算”的语义，
     * 单调计时无法跨重启保持，恢复点是新的转换边界。</p>
     */
    public ScheduledTimeout recoverAfterRestart(ScheduledTimeout persisted, long newEpochTickNanos, Instant newWallNow) {
        return build(
                persisted.timeoutId(),
                persisted.ruleId(),
                persisted.ruleVersion(),
                newWallNow,
                newEpochTickNanos,
                persisted.deadlineInstant(),
                true);
    }

    /** 查询状态：只读单调刻度，墙钟当前值完全不参与。 */
    public TimeoutStatus statusOf(ScheduledTimeout timeout) {
        long nowTick = clock.monotonicNanos();
        return statusAt(timeout, nowTick);
    }

    /**
     * 便捷方法：在同一时钟快照上完成“重启恢复锚定 + 立即查询”，
     * 避免锚定与查询分两次读取时钟造成的量级偏差。
     * 入参只需携带墙钟域字段（截止瞬间），单调字段会被忽略并重算。
     */
    public TimeoutStatus recoverAndStatus(ScheduledTimeout persistedWallFields) {
        Instant nowWall = clock.wallClockInstant();
        long nowTick = clock.monotonicNanos();
        ScheduledTimeout recovered = build(
                persistedWallFields.timeoutId(),
                persistedWallFields.ruleId(),
                persistedWallFields.ruleVersion(),
                nowWall,
                nowTick,
                persistedWallFields.deadlineInstant(),
                true);
        return statusAt(recovered, nowTick);
    }

    /** 用指定单调刻度查询（测试/模拟用），语义与 {@link #statusOf} 完全一致。 */
    public TimeoutStatus statusAt(ScheduledTimeout timeout, long nowTick) {
        long deadlineTick = timeout.monotonicDeadlineTickNanos();
        long remaining = SaturatingMath.subtractClamped(deadlineTick, nowTick);
        boolean expired = nowTick >= deadlineTick;
        return new TimeoutStatus(timeout.timeoutId(), nowTick, deadlineTick, remaining, expired);
    }

    private ScheduledTimeout build(
            String timeoutId, String ruleId, String ruleVersion,
            Instant nowWall, long nowTick, Instant deadline, boolean recovered) {
        long durationNanos = SaturatingMath.toNanosSaturated(Duration.between(nowWall, deadline));
        long deadlineTick = SaturatingMath.addClamped(nowTick, durationNanos);
        boolean alreadyExpired = !deadline.isAfter(nowWall);
        return new ScheduledTimeout(
                timeoutId, ruleId, ruleVersion,
                nowWall, deadline,
                durationNanos, nowTick, deadlineTick,
                alreadyExpired, recovered, tzdbVersion);
    }
}
