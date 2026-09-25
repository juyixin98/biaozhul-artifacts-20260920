package dev.example.cp.engine;

/**
 * 按经过的墙钟时间周期性触发（时间间隔同样可注入：配合虚拟时钟即可在测试中加速）。
 */
public final class IntervalScheduler implements CheckpointScheduler {

    private final long intervalMillis;
    private long lastCheckpointMillis = Long.MIN_VALUE;

    public IntervalScheduler(long intervalMillis) {
        if (intervalMillis <= 0) {
            throw new IllegalArgumentException("interval must be positive");
        }
        this.intervalMillis = intervalMillis;
    }

    @Override
    public boolean shouldCheckpointAfterEvent(long processedInRun, Clock clock) {
        long now = clock.nowMillis();
        if (lastCheckpointMillis == Long.MIN_VALUE) {
            lastCheckpointMillis = now;
        }
        if (now - lastCheckpointMillis >= intervalMillis) {
            lastCheckpointMillis = now;
            return true;
        }
        return false;
    }
}
