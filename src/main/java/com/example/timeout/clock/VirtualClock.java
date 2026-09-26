package com.example.timeout.clock;

import java.time.Duration;
import java.time.Instant;
import java.util.Objects;

/**
 * 可控虚拟时钟，仅用于测试与演示。
 *
 * <ul>
 *   <li>{@link #tick(Duration)}：时间真实流逝——墙钟与单调钟<b>同步</b>前进；</li>
 *   <li>{@link #setWall(Instant)} / {@link #advanceWall(Duration)}：校时——只改墙钟，
 *       单调读数保持不变（向前、向后校时均可模拟）。</li>
 * </ul>
 */
public final class VirtualClock implements TimeoutClock {

    private Instant wall;
    private long monoNanos;

    public VirtualClock(Instant startWall) {
        this.wall = Objects.requireNonNull(startWall, "startWall");
        this.monoNanos = 0L;
    }

    @Override
    public Instant wall() {
        return wall;
    }

    @Override
    public long monoNanos() {
        return monoNanos;
    }

    @Override
    public String type() {
        return "virtual";
    }

    /** 让真实时间流逝：墙钟与单调钟一起前进 {@code elapsed}（允许为负吗？不允许，单调钟不能倒退）。 */
    public void tick(Duration elapsed) {
        Objects.requireNonNull(elapsed, "elapsed");
        if (elapsed.isNegative()) {
            throw new IllegalArgumentException("monotonic time cannot move backwards: " + elapsed);
        }
        monoNanos = Math.addExact(monoNanos, elapsed.toNanos());
        wall = wall.plus(elapsed);
    }

    /** 把墙钟直接设置到某个瞬时（NTP 校时），单调读数不变。 */
    public void setWall(Instant newWall) {
        this.wall = Objects.requireNonNull(newWall, "newWall");
    }

    /** 墙钟相对调整（可正可负），单调读数不变。 */
    public void advanceWall(Duration delta) {
        Objects.requireNonNull(delta, "delta");
        this.wall = wall.plus(delta);
    }
}
