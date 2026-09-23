package com.example.wm.time;

/**
 * 手动驱动的虚拟时钟。时间只能向前推进，不能倒退，
 * 以便确定性地重放事件、验证空闲超时与水位线单调性。
 */
public final class VirtualClock implements Clock {
    private long now;

    public VirtualClock(long startTimeMs) {
        this.now = startTimeMs;
    }

    @Override
    public long currentTimeMillis() {
        return now;
    }

    /** 推进到某个绝对时间；早于当前时间会抛出异常（时钟不可倒退）。 */
    public void advanceTo(long newTimeMs) {
        if (newTimeMs < now) {
            throw new IllegalArgumentException(
                    "virtual clock cannot move backwards: " + newTimeMs + " < " + now);
        }
        this.now = newTimeMs;
    }

    /** 向前推进一段非负时长。 */
    public void advanceBy(long deltaMs) {
        if (deltaMs < 0) {
            throw new IllegalArgumentException("delta must be non-negative: " + deltaMs);
        }
        this.now = saturatedAdd(now, deltaMs);
    }

    static long saturatedAdd(long a, long b) {
        long r = a + b;
        if (((a ^ r) & (b ^ r)) < 0) {
            return Long.MAX_VALUE;
        }
        return r;
    }
}
