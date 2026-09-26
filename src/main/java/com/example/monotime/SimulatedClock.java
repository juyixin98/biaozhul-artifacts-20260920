package com.example.monotime;

import java.time.Duration;
import java.time.Instant;
import java.util.Objects;

/**
 * 用于测试与样例演示的虚拟时钟。墙钟与单调刻度分别由独立的方法推进——
 * 这刻意保留了“两种时钟互不相关”的事实：校时只跳墙钟，时间流逝只增单调刻度。
 *
 * <p>真实系统里二者本就独立（NTP 调整墙钟不会改变 nanoTime 的速率/读数），
 * 虚拟时钟让验收用例可以确定性地向前/向后校时。</p>
 *
 * <p><b>非线程安全</b>：可变状态无同步，仅供单线程场景脚本使用。</p>
 */
public final class SimulatedClock implements ClockPort {

    private Instant wall;
    private long tickNanos;

    public SimulatedClock(Instant wall, long tickNanos) {
        this.wall = Objects.requireNonNull(wall, "wall");
        this.tickNanos = tickNanos;
    }

    @Override
    public long monotonicNanos() {
        return tickNanos;
    }

    @Override
    public Instant wallClockInstant() {
        return wall;
    }

    /** 仅推进单调计时器（真实时间流逝），墙钟不动。 */
    public void advanceMonotonic(Duration elapsed) {
        tickNanos = SaturatingMath.addClamped(tickNanos, SaturatingMath.toNanosSaturated(elapsed));
    }

    /** 仅向前校时（例如 NTP 跳变），单调刻度不变。 */
    public void jumpWallClockForward(Duration delta) {
        wall = wall.plus(delta);
    }

    /** 仅向后校时（例如管理员把时钟拨回），单调刻度不变。 */
    public void jumpWallClockBackward(Duration delta) {
        wall = wall.minus(delta);
    }

    /** 直接把墙钟设置到指定瞬间，单调刻度不变。 */
    public void setWallClock(Instant instant) {
        this.wall = Objects.requireNonNull(instant, "instant");
    }

    /** 直接把单调刻度设置到指定读数（用于模拟重启后 nanoTime 纪元重置）。 */
    public void setMonotonicNanos(long tickNanos) {
        this.tickNanos = tickNanos;
    }
}
