package drvb.time;

/**
 * 由调用者完全掌控时间的时钟，用于确定性测试。
 *
 * <p>时间只有显式调用 {@link #advanceTo(long)} / {@link #advanceBy(long)} 才会前进，
 * 从不自己走动。
 */
public final class ManualClock implements Clock {
    private long current;

    public ManualClock() {
        this(0L);
    }

    public ManualClock(long startMillis) {
        this.current = startMillis;
    }

    @Override
    public long nowMillis() {
        return current;
    }

    /** 将时间推进到 {@code target}（不允许倒退）。 */
    public void advanceTo(long target) {
        if (target < current) {
            throw new IllegalArgumentException(
                    "ManualClock 不允许时间倒退: 当前=" + current + " 目标=" + target);
        }
        current = target;
    }

    /** 将时间前进 {@code deltaMillis} 毫秒。 */
    public void advanceBy(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta 不能为负");
        }
        current += deltaMillis;
    }
}
